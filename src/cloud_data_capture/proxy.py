"""Candidate cloud relay with durable business capture and a catalog boundary.

This module is deliberately isolated from the deployed V9 relay. A catalog
implementation must acknowledge each method only after its own durable commit.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import socket
import sys
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlsplit
import uuid
import zlib

from .storage import ArtifactRef, ManagedFiles


_ROUTE = re.compile(r"^/p/([a-z][a-z0-9_-]*)/v1(?:/(.*))?$")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade"}
_AUTH = {"authorization", "x-api-key", "api-key", "x-api-app-key", "x-api-access-key"}
_PRIVATE_HEADER = re.compile(r"auth|cookie|token|secret|key|credential|password|session", re.I)
_MAX_TRAILER_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Caller:
    caller_id: str
    project_id: str
    is_admin: bool = False


@dataclass(frozen=True)
class Provider:
    provider_id: str
    base_url: str
    credential_ref: str
    config_revision: str
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    models: tuple[str, ...] = ()
    static_headers: tuple[tuple[str, str], ...] = ()


class Catalog(Protocol):
    """A single writer enforces uniqueness, project access and atomic commits.

    `admit_request` returns accepted, existing or conflict. An existing request
    is never sent to the provider a second time by this candidate.
    `finalize_attempt` commits result metadata and provider usage in one step.
    """

    def admit_request(self, *, caller: Caller, request_id: str, provider: str,
                      method: str, path: str, model: str | None, input_ref: ArtifactRef,
                      request_snapshot_ref: ArtifactRef,
                      request_wire_ref: ArtifactRef | None,
                      config_revision: str) -> str: ...

    def begin_attempt(self, *, project: str, request_id: str, attempt_id: str) -> None: ...

    def finalize_attempt(self, *, project: str, request_id: str, attempt_id: str,
                         outcome: str, status_code: int, result_ref: ArtifactRef,
                         upstream_request_id: str | None, usage: dict | None) -> None: ...

    def mark_unknown(self, *, project: str, request_id: str, attempt_id: str,
                     reason: str, partial_ref: ArtifactRef | None) -> str: ...

    def get_attempt_state(self, project: str, request_id: str, attempt_id: str) -> str | None: ...

    def daily_usage(self, day_cst: str) -> list[dict]: ...


def recover_incomplete(catalog: Catalog, files: ManagedFiles) -> dict[str, int]:
    """Reconcile crash receipts before accepting new requests; never resubmit."""
    counts = {"checked": 0, "marked_unknown": 0, "already_terminal": 0,
              "attempt_not_started": 0}
    for path in sorted((files.root / "recovery").glob("*.json")):
        with path.open("r", encoding="utf-8") as stream:
            record = json.load(stream)
        if record.get("state") not in {"attempt_begin_pending", "before_upstream", "upstream_may_have_started",
                                       "upstream_started", "unknown"}:
            continue
        attempt_id = record.get("attempt_id")
        project = record.get("project")
        request_id = record.get("request_id")
        if not isinstance(attempt_id, str) or path.stem != attempt_id or not isinstance(project, str) or not isinstance(request_id, str):
            raise RuntimeError("invalid_attempt_recovery_receipt")
        counts["checked"] += 1
        state = catalog.get_attempt_state(project, request_id, attempt_id)
        if state is None and record["state"] == "attempt_begin_pending":
            # The pre-begin receipt exists but the catalog transaction never
            # started. Keep it for admission repair; there is no upstream call.
            counts["attempt_not_started"] += 1
            continue
        if state in {"completed", "upstream_error"}:
            files.recovery(attempt_id, {**record, "state": "catalog_committed", "catalog_state": state})
            counts["already_terminal"] += 1
            continue
        partial = record.get("partial")
        partial_ref = ArtifactRef(**partial) if isinstance(partial, dict) else None
        reason = record.get("reason") or ("restart_after_" + record["state"])
        actual = catalog.mark_unknown(project=project, request_id=request_id,
                                      attempt_id=attempt_id, reason=reason, partial_ref=partial_ref)
        if actual in {"completed", "upstream_error"}:
            files.recovery(attempt_id, {**record, "state": "catalog_committed", "catalog_state": actual})
            counts["already_terminal"] += 1
        elif actual == "unknown":
            files.recovery(attempt_id, {**record, "state": "unknown", "reason": reason})
            counts["marked_unknown"] += 1
        else:
            raise RuntimeError("unrecognized_catalog_attempt_state")
    return counts


def _counter(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for key in ("response", "message"):
        if isinstance(payload.get(key), dict):
            candidates.insert(0, payload[key])
    usage = next((item.get(key) for item in candidates
                  for key in ("usage", "usageMetadata", "usage_metadata")
                  if isinstance(item.get(key), dict)), None)
    if usage is None:
        return None
    counts = {
        "input_tokens": next((_counter(usage[k]) for k in ("input_tokens", "prompt_tokens", "promptTokenCount")
                              if k in usage and _counter(usage[k]) is not None), None),
        "output_tokens": next((_counter(usage[k]) for k in ("output_tokens", "completion_tokens", "candidatesTokenCount")
                               if k in usage and _counter(usage[k]) is not None), None),
        "total_tokens": next((_counter(usage[k]) for k in ("total_tokens", "totalTokenCount")
                              if k in usage and _counter(usage[k]) is not None), None),
    }
    metrics = []

    def visit(value: dict, prefix: str = "") -> None:
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            name = prefix + "." + key if prefix else key
            if isinstance(item, dict):
                visit(item, name)
            elif isinstance(item, (int, float, Decimal)) and not isinstance(item, bool):
                if not prefix and key in {"input_tokens", "prompt_tokens", "promptTokenCount",
                                          "output_tokens", "completion_tokens", "candidatesTokenCount",
                                          "total_tokens", "totalTokenCount"}:
                    continue
                leaf = key.lower()
                unit = ("token" if "token" in leaf else "second" if "second" in leaf or "duration" in leaf
                        else "byte" if "byte" in leaf else "character" if "character" in leaf
                        else "count" if "count" in leaf or "image" in leaf else None)
                if unit is None:
                    continue
                try:
                    number = Decimal(str(item))
                except InvalidOperation:
                    continue
                if number.is_finite() and number >= 0:
                    metrics.append({"name": name, "unit": unit, "value": str(number)})

    visit(usage)
    if all(value is None for value in counts.values()) and not metrics:
        return None
    return {**counts, "metrics": metrics, "source": "provider"}


class ResponseObservation:
    def __init__(self, content_type: str, content_encoding: str):
        self.sse = "text/event-stream" in content_type.lower()
        encoding = content_encoding.lower().strip()
        self.opaque = bool(encoding and encoding not in {"identity", "gzip"})
        self.gzip = encoding == "gzip"
        self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if self.gzip else None
        self.decoded_size = 0
        self.buffer = bytearray()
        self.usage: dict | None = None
        self.terminal = False
        self.overflow = False

    def feed(self, chunk: bytes) -> None:
        if self.opaque or self.overflow:
            return
        if self.decoder is not None:
            try:
                chunk = self.decoder.decompress(chunk, 16 * 1024 * 1024 - self.decoded_size + 1)
            except zlib.error:
                self.overflow = True
                return
            self.decoded_size += len(chunk)
            if self.decoder.unconsumed_tail or self.decoded_size > 16 * 1024 * 1024:
                self.overflow = True
                return
        self.buffer.extend(chunk)
        if len(self.buffer) > 16 * 1024 * 1024:
            self.buffer.clear()
            self.overflow = True
            return
        if not self.sse:
            return
        self.buffer = bytearray(self.buffer.replace(b"\r\n", b"\n"))
        while b"\n\n" in self.buffer:
            raw, _, rest = self.buffer.partition(b"\n\n")
            self.buffer = bytearray(rest)
            lines = raw.split(b"\n")
            data = b"\n".join(line[5:].lstrip() for line in lines if line.startswith(b"data:"))
            if data == b"[DONE]":
                self.terminal = True
                continue
            if not data:
                continue
            try:
                event = json.loads(data, parse_float=Decimal)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "message_stop":
                self.terminal = True
            found = _usage(event)
            if found:
                if self.usage is None:
                    self.usage = found
                else:
                    for field in ("input_tokens", "output_tokens", "total_tokens"):
                        if found[field] is not None:
                            self.usage[field] = found[field]
                    by_name = {(m["name"], m["unit"]): m for m in self.usage["metrics"]}
                    by_name.update({(m["name"], m["unit"]): m for m in found["metrics"]})
                    self.usage["metrics"] = list(by_name.values())

    def result(self) -> dict | None:
        if self.sse or self.opaque or self.overflow:
            return self.usage
        try:
            return _usage(json.loads(self.buffer, parse_float=Decimal))
        except (ValueError, UnicodeError):
            return None

    def framing_complete(self) -> bool:
        return not self.overflow and not self.opaque and (self.decoder is None or self.decoder.eof)


def _top_level_model(path: str) -> str | None:
    """Scan a JSON file with bounded memory, including a model after large media."""
    depth = 0
    in_string = False
    escaped = False
    role: str | None = None
    captured = bytearray()
    key: str | None = None
    expect_key = False
    expect_value = False
    model = None
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            for char in block:
                if in_string:
                    if role and len(captured) <= 512:
                        captured.append(char)
                    if escaped:
                        escaped = False
                    elif char == 92:
                        escaped = True
                    elif char == 34:
                        in_string = False
                        if role and len(captured) <= 512:
                            try:
                                value = json.loads(b'"' + captured)
                            except (ValueError, UnicodeError):
                                value = None
                            if role == "key":
                                key = value if isinstance(value, str) else None
                            elif role == "model" and isinstance(value, str):
                                model = value
                        role = None
                    continue
                if char == 34:
                    in_string = True
                    escaped = False
                    captured = bytearray()
                    role = "key" if depth == 1 and expect_key else "model" if depth == 1 and expect_value and key == "model" else None
                    if role == "key":
                        expect_key = False
                    if expect_value:
                        expect_value = False
                elif char in (123, 91):
                    depth += 1
                    if depth == 1:
                        expect_key = True
                    if expect_value:
                        expect_value = False
                elif char in (125, 93):
                    depth -= 1
                elif depth == 1 and char == 44:
                    key = None
                    expect_key = True
                    expect_value = False
                elif depth == 1 and char == 58 and key is not None:
                    expect_value = True
                elif expect_value and char not in (9, 10, 13, 32):
                    expect_value = False
    return model


class CandidateServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, providers: dict[str, Provider],
                 authenticate: Callable[[str], Caller | None],
                 resolve_credential: Callable[[str], str], catalog: Catalog,
                 files: ManagedFiles, max_request_bytes: int | None = None):
        if address[0] not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("candidate_must_bind_loopback")
        for name, provider in providers.items():
            base = urlsplit(provider.base_url)
            if name != provider.provider_id or not _ROUTE.fullmatch(f"/p/{name}/v1"):
                raise ValueError("invalid_provider_id")
            if not base.hostname or (base.scheme != "https" and not (base.scheme == "http" and base.hostname in {"127.0.0.1", "localhost", "::1"})):
                raise ValueError("provider_requires_https_or_loopback")
            if base.username or base.password or base.query or base.fragment:
                raise ValueError("provider_url_contains_secret_or_query")
            if provider.auth_header.lower() not in {"authorization", "x-api-key", "api-key", "doubao-speech"}:
                raise ValueError("unsupported_provider_auth")
            if not provider.config_revision or provider.config_revision == "unversioned":
                raise ValueError("config_revision_required")
            if any(not isinstance(model, str) or not model for model in provider.models) or len(set(provider.models)) != len(provider.models):
                raise ValueError("invalid_provider_models")
            for key, value in provider.static_headers:
                if (not re.fullmatch(r"[A-Za-z0-9-]+", key) or
                    key.lower() in _AUTH | {"host"} or not isinstance(value, str) or
                    "\r" in value or "\n" in value):
                    raise ValueError("invalid_static_header")
        self.providers = providers
        self.authenticate = authenticate
        self.resolve_credential = resolve_credential
        self.catalog = catalog
        self.files = files
        if max_request_bytes is not None and max_request_bytes <= 0:
            raise ValueError("invalid_max_request_bytes")
        self.max_request_bytes = max_request_bytes
        self._owner = (files.root / "gateway-owner.lock").open("a+b")
        try:
            self._owner.seek(0, os.SEEK_END)
            if self._owner.tell() == 0:
                self._owner.write(b"0")
                self._owner.flush()
            self._owner.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._owner.close()
            raise RuntimeError("candidate_store_already_owned") from exc
        try:
            super().__init__(address, CandidateHandler)
            recover_incomplete(catalog, files)
        except BaseException:
            if hasattr(self, "socket"):
                self.server_close()
            else:
                self._release_owner()
            raise

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self._release_owner()

    def _release_owner(self) -> None:
        if getattr(self, "_owner", None) is not None and not self._owner.closed:
            self._owner.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._owner.fileno(), fcntl.LOCK_UN)
            self._owner.close()


class CandidateHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: CandidateServer

    def log_message(self, fmt: str, *args: object) -> None:
        # URLs and headers may contain sensitive provider or caller material.
        return

    def do_GET(self) -> None: self._dispatch()
    def do_POST(self) -> None: self._dispatch()
    def do_PUT(self) -> None: self._dispatch()
    def do_PATCH(self) -> None: self._dispatch()
    def do_DELETE(self) -> None: self._dispatch()
    def do_HEAD(self) -> None: self._dispatch()

    def _write_json(self, code: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response_only(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _json(self, code: int, message: str, request_id: str | None = None) -> None:
        self._write_json(code, {"error": {"message": message}, "request_id": request_id})

    def _token(self) -> str:
        bearer = self.headers.get("Authorization", "")
        bearer = bearer[7:] if bearer.lower().startswith("bearer ") else ""
        values = [v for v in (bearer, self.headers.get("x-api-key"), self.headers.get("api-key")) if v]
        return values[0] if values and all(v == values[0] for v in values) else ""

    def _provider_key(self, provider: Provider) -> str | dict[str, str]:
        if provider.auth_header.lower() == "doubao-speech":
            key = {field: self.server.resolve_credential(provider.credential_ref + ":" + field)
                   for field in ("app-id", "access-token")}
            if not all(key.values()):
                raise RuntimeError("provider_credential_unavailable")
            return key
        value = self.server.resolve_credential(provider.credential_ref)
        if not value:
            raise RuntimeError("provider_credential_unavailable")
        return value

    def _capture_request(self, project: str) -> tuple[ArtifactRef, ArtifactRef | None]:
        writer = self.server.files.begin(project)
        wire_writer = None
        try:
            transfer = self.headers.get("Transfer-Encoding", "").lower()
            if transfer and transfer != "chunked":
                raise ValueError("unsupported_transfer_encoding")
            if transfer and self.headers.get("Content-Length") is not None:
                raise ValueError("conflicting_body_framing")
            if transfer == "chunked":
                wire_writer = self.server.files.begin(project)

                def wire_write(data: bytes) -> None:
                    wire_writer.write(data)

                while True:
                    line = self.rfile.readline(128)
                    if not line.endswith(b"\r\n") or len(line) >= 128:
                        raise ValueError("invalid_chunk_header")
                    wire_write(line)
                    size = int(line.split(b";", 1)[0].strip(), 16)
                    if size < 0 or (self.server.max_request_bytes is not None and
                                    writer.size + size > self.server.max_request_bytes):
                        raise ValueError("request_too_large")
                    if size == 0:
                        break
                    remaining = size
                    while remaining:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            raise ConnectionError("incomplete_request")
                        writer.write(chunk)
                        wire_write(chunk)
                        remaining -= len(chunk)
                    ending = self.rfile.read(2)
                    if ending != b"\r\n":
                        raise ValueError("invalid_chunk_ending")
                    wire_write(ending)
                trailer_bytes = 0
                trailer_line_bytes = 0
                preceding = None
                while True:
                    trailer = self.rfile.readline(8192)
                    if not trailer:
                        raise ConnectionError("incomplete_request_trailer")
                    trailer_bytes += len(trailer)
                    if trailer_bytes > _MAX_TRAILER_BYTES:
                        raise ValueError("request_trailers_too_large")
                    wire_write(trailer)
                    trailer_line_bytes += len(trailer)
                    if trailer.endswith(b"\n"):
                        before_lf = trailer[-2] if len(trailer) > 1 else preceding
                        if before_lf != 13:
                            raise ValueError("invalid_request_trailer_line")
                        if trailer_line_bytes == 2:
                            break
                        trailer_line_bytes = 0
                    preceding = trailer[-1]
            else:
                remaining = int(self.headers.get("Content-Length", "0"))
                if remaining < 0 or (self.server.max_request_bytes is not None and
                                     remaining > self.server.max_request_bytes):
                    raise ValueError("request_too_large")
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise ConnectionError("incomplete_request")
                    writer.write(chunk)
                    remaining -= len(chunk)
            body_ref = writer.publish()
            return body_ref, wire_writer.publish() if wire_writer is not None else None
        except BaseException:
            writer.abort()
            if wire_writer is not None:
                wire_writer.abort()
            raise

    def _request_snapshot(self, caller: Caller, provider: Provider, request_id: str,
                          body_ref: ArtifactRef, wire_ref: ArtifactRef | None) -> ArtifactRef:
        connection_tokens = {x.strip().lower() for x in self.headers.get("Connection", "").split(",")}
        static_names = {name.lower() for name, _ in provider.static_headers}
        headers = []
        redacted = []
        for index, (name, value) in enumerate(self.headers.raw_items()):
            lower = name.lower()
            forwarded = lower not in _HOP | _AUTH | connection_tokens | static_names | {"host", "content-length"}
            if _PRIVATE_HEADER.search(lower):
                headers.append({"index": index, "name": name, "value_state": "unknown",
                                "reason": "credential_value_not_retained", "forwarded": forwarded})
                redacted.append(name)
            else:
                headers.append({"index": index, "name": name, "value_state": "known",
                                "value": value, "forwarded": forwarded})
        snapshot = {
            "schema": "aitoolbox.v10.cloud-request-snapshot/1",
            "project_id": caller.project_id,
            "caller_id": caller.caller_id,
            "request_id": request_id,
            "provider_id": provider.provider_id,
            "config_revision": provider.config_revision,
            "method": self.command,
            "target": self.path,
            "raw_request_line_base64": base64.b64encode(self.raw_requestline).decode("ascii"),
            "headers": headers,
            "body_ref": asdict(body_ref),
            "framing": {"kind": ("chunked" if wire_ref is not None else
                                 "content_length" if self.headers.get("Content-Length") is not None
                                 else "none"),
                        "declared_content_length": self.headers.get("Content-Length"),
                        "wire_ref": asdict(wire_ref) if wire_ref is not None else None},
            "recovery": {"method_target_body": "known",
                         "forwarded_header_values": ("unknown" if any(
                             item["value_state"] == "unknown" and item["forwarded"] for item in headers)
                             else "known"),
                         "exact_original_http_wire": "unknown",
                         "automatic_resubmit": False},
            "unknown_fields": ([{"field": "raw_header_line_bytes",
                                 "reason": "http_parser_did_not_retain_wire_header_lines"},
                                {"field": "provider_credential_value",
                                 "reason": "resolved_from_secret_store_not_retained"}] +
                               [{"field": "header_value", "name": name,
                                 "reason": "credential_value_not_retained"} for name in redacted]),
        }
        writer = self.server.files.begin(caller.project_id)
        try:
            writer.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            return writer.publish()
        except BaseException:
            writer.abort()
            raise

    def _dispatch(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health" and self.command == "GET":
            degraded = bool(getattr(self.server.catalog, "logging_degraded", False))
            self._write_json(200, {"status": "ok", "component": "v10-cloud-data-candidate",
                                   "scope": "relay_liveness", "core_storage": "unchecked",
                                   "logging_degraded": degraded})
            return
        try:
            caller = self.server.authenticate(self._token())
        except Exception:
            self._json(503, "caller_authentication_unavailable")
            return
        if caller is None:
            self._json(401, "invalid_gateway_key")
            return
        if parsed.path == "/admin/usage" and self.command == "GET":
            if not caller.is_admin:
                self._json(403, "admin_required")
                return
            day = parse_qs(parsed.query).get("day", [""])[0]
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    raise ValueError("invalid_day")
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                self._json(400, "invalid_day")
                return
            try:
                rows = self.server.catalog.daily_usage(day)
                if not isinstance(rows, list):
                    raise RuntimeError("v9_usage_shape_unavailable")
                self._write_json(200, {"day_cst": day, "rows": rows})
            except Exception:
                self._json(503, "core_storage_unavailable")
            return
        match = _ROUTE.fullmatch(parsed.path)
        if match is None or ".." in parsed.path or "%" in parsed.path:
            self._json(404, "unknown_route")
            return
        provider = self.server.providers.get(match.group(1))
        if provider is None:
            self._json(404, "unknown_provider")
            return
        suffix = match.group(2) or ""
        if suffix == "models" and self.command == "GET" and not parsed.query:
            try:
                self._provider_key(provider)
            except Exception:
                self._json(503, "provider_credential_unavailable")
                return
            self._write_json(200, {"object": "list", "data": [
                {"id": model, "object": "model", "owned_by": provider.provider_id}
                for model in provider.models]})
            return
        try:
            credential = self._provider_key(provider)
        except Exception:
            self._json(503, "provider_credential_unavailable")
            return
        request_id = self.headers.get("X-Request-Id") or uuid.uuid4().hex
        if not _REQUEST_ID.fullmatch(request_id):
            self._json(400, "invalid_request_id")
            return
        try:
            request_ref, wire_ref = self._capture_request(caller.project_id)
        except (OSError, ValueError, ConnectionError, RuntimeError):
            self._json(400, "request_capture_failed", request_id)
            return
        self._after_capture(caller, provider, credential, request_id, request_ref,
                            wire_ref, parsed.query, suffix)

    def _after_capture(self, caller: Caller, provider: Provider, credential: str | dict[str, str],
                       request_id: str,
                       request_ref: ArtifactRef, wire_ref: ArtifactRef | None,
                       query: str, suffix: str) -> None:
        model = (_top_level_model(request_ref.path)
                 if "json" in self.headers.get("Content-Type", "").lower() else None)
        path = "/" + suffix + (("?" + query) if query else "")
        admission_receipt = uuid.uuid4().hex
        receipt = {"project": caller.project_id, "request_id": request_id,
                   "state": "pre_admission", "input": asdict(request_ref),
                   "wire": asdict(wire_ref) if wire_ref is not None else None,
                   "snapshot_state": "unknown", "snapshot_gap_reason": "snapshot_not_yet_published"}
        try:
            self.server.files.recovery(admission_receipt, receipt)
        except OSError:
            self._json(503, "admission_recovery_unavailable", request_id)
            return
        try:
            snapshot_ref = self._request_snapshot(caller, provider, request_id, request_ref, wire_ref)
            receipt = {"project": caller.project_id, "request_id": request_id,
                       "state": "pre_admission", "input": asdict(request_ref),
                       "wire": asdict(wire_ref) if wire_ref is not None else None,
                       "snapshot_state": "known", "snapshot_ref": asdict(snapshot_ref)}
            self.server.files.recovery(admission_receipt, receipt)
        except Exception:
            self._json(503, "request_snapshot_unavailable", request_id)
            return
        try:
            admitted = self.server.catalog.admit_request(
                caller=caller, request_id=request_id, provider=provider.provider_id,
                method=self.command, path=path, model=model, input_ref=request_ref,
                request_snapshot_ref=snapshot_ref, request_wire_ref=wire_ref,
                config_revision=provider.config_revision)
        except Exception:
            self._json(503, "core_storage_unavailable", request_id)
            return
        try:
            self.server.files.recovery(admission_receipt, {**receipt, "state": admitted})
        except OSError:
            # The pre-admission receipt still identifies the retained input.
            pass
        if admitted != "accepted":
            self._json(409, "request_identity_conflict" if admitted == "conflict" else "request_already_admitted", request_id)
            return
        attempt_id = uuid.uuid4().hex
        try:
            self.server.files.recovery(attempt_id, {"project": caller.project_id, "request_id": request_id,
                                                    "attempt_id": attempt_id, "state": "attempt_begin_pending"})
            self.server.catalog.begin_attempt(project=caller.project_id, request_id=request_id, attempt_id=attempt_id)
            self.server.files.recovery(attempt_id, {"project": caller.project_id, "request_id": request_id,
                                                    "attempt_id": attempt_id, "state": "before_upstream"})
        except Exception:
            self._json(503, "attempt_storage_unavailable", request_id)
            return
        self._forward(caller, provider, credential, request_id, attempt_id,
                      request_ref, wire_ref, suffix, query)

    def _unknown(self, caller: Caller, request_id: str, attempt_id: str,
                 reason: str, partial: ArtifactRef | None) -> None:
        value = {"project": caller.project_id, "request_id": request_id, "attempt_id": attempt_id,
                 "state": "unknown", "reason": reason, "partial": asdict(partial) if partial else None}
        try:
            self.server.files.recovery(attempt_id, value)
        except OSError:
            pass
        try:
            actual = self.server.catalog.mark_unknown(project=caller.project_id, request_id=request_id,
                                                      attempt_id=attempt_id, reason=reason, partial_ref=partial)
            if actual in {"completed", "upstream_error"}:
                self.server.files.recovery(attempt_id, {**value, "state": "catalog_committed",
                                                        "catalog_state": actual})
        except Exception:
            pass

    def _response_headers(self, response: http.client.HTTPResponse, request_id: str,
                          framed_length: int | None, no_body: bool, chunked: bool) -> None:
        self.send_response_only(response.status, response.reason)
        response_tokens = {x.strip().lower() for x in (response.getheader("connection") or "").split(",")}
        for name, value in response.getheaders():
            if name.lower() in _HOP | response_tokens | {"content-length"}:
                continue
            self.send_header(name, value)
        self.send_header("X-AIToolbox-Request-Id", request_id)
        if not no_body:
            if framed_length is not None:
                self.send_header("Content-Length", str(framed_length))
            elif chunked:
                self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_chunk(self, chunk: bytes, *, chunked: bool) -> None:
        if not chunked:
            self.wfile.write(chunk)
        else:
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
        self.wfile.flush()

    def _send_saved_tail(self, result: ArtifactRef, offset: int, *, chunked: bool) -> None:
        with open(result.path, "rb") as saved:
            saved.seek(offset)
            for block in iter(lambda: saved.read(8192), b""):
                self._send_chunk(block, chunked=chunked)

    def _forward(self, caller: Caller, provider: Provider, credential: str | dict[str, str],
                 request_id: str,
                 attempt_id: str, request_ref: ArtifactRef, wire_ref: ArtifactRef | None,
                 suffix: str, query: str) -> None:
        base = urlsplit(provider.base_url)
        upstream_path = base.path.rstrip("/") + ("/" + suffix if suffix else "")
        if query:
            upstream_path += "?" + query
        conn_type = http.client.HTTPSConnection if base.scheme == "https" else http.client.HTTPConnection
        conn = conn_type(base.hostname, base.port or (443 if base.scheme == "https" else 80), timeout=7200)
        response_started = False
        writer = None
        upstream_id = None
        partial = None
        catalog_committed = False
        try:
            self.server.files.recovery(attempt_id, {"project": caller.project_id, "request_id": request_id,
                                                    "attempt_id": attempt_id, "state": "upstream_may_have_started"})
            conn.putrequest(self.command, upstream_path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", base.netloc)
            connection_tokens = {x.strip().lower() for x in self.headers.get("Connection", "").split(",")}
            static_names = {name.lower() for name, _ in provider.static_headers}
            for name, value in self.headers.items():
                if name.lower() in _HOP | _AUTH | connection_tokens | static_names | {"host", "content-length"}:
                    continue
                conn.putheader(name, value)
            if provider.auth_header.lower() == "doubao-speech":
                conn.putheader("X-Api-App-Key", credential["app-id"])
                conn.putheader("X-Api-Access-Key", credential["access-token"])
            else:
                conn.putheader(provider.auth_header, provider.auth_prefix + credential)
            for name, value in provider.static_headers:
                conn.putheader(name, value)
            if wire_ref is None:
                conn.putheader("Content-Length", str(request_ref.size))
            else:
                conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            with open(wire_ref.path if wire_ref is not None else request_ref.path, "rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    conn.send(chunk)
            response = conn.getresponse()
            upstream_id = response.getheader("x-request-id") or response.getheader("request-id")
            length_header = response.getheader("content-length")
            try:
                framed_length = int(length_header) if length_header is not None else None
            except ValueError:
                framed_length = None
            if framed_length is not None and framed_length < 0:
                framed_length = None
            if response.chunked:
                framed_length = None
            downstream_chunked = response.chunked
            no_body = self.command == "HEAD" or response.status in {204, 304} or 100 <= response.status < 200
            observation = ResponseObservation(response.getheader("content-type", ""),
                                              response.getheader("content-encoding", ""))
            writer = self.server.files.begin(caller.project_id)
            self.server.files.recovery(attempt_id, {"project": caller.project_id, "request_id": request_id,
                                                    "attempt_id": attempt_id, "state": "upstream_started",
                                                    "response_stage": str(writer.stage)})
            observed_bytes = 0
            pending = b""
            sent_bytes = 0
            terminal_seen = False
            if not no_body:
                while True:
                    chunk = response.read1(8192)
                    if not chunk:
                        break
                    writer.write(chunk)  # fsync before any client sees these bytes.
                    observed_bytes += len(chunk)
                    observation.feed(chunk)
                    if observation.sse and observation.terminal:
                        terminal_seen = True
                    if terminal_seen:
                        # The whole chunk containing [DONE]/message_stop stays
                        # private until the catalog commits, even with trailing
                        # provider whitespace after the terminal event.
                        continue
                    available = pending + chunk
                    # Keep one trailing byte until the catalog has committed.
                    # This preserves streaming without allowing a complete
                    # Content-Length body or the final SSE blank line through.
                    to_client, pending = available[:-1], available[-1:]
                    if to_client:
                        if not response_started:
                            self._response_headers(response, request_id, framed_length, no_body, downstream_chunked)
                            response_started = True
                        self._send_chunk(to_client, chunked=downstream_chunked)
                        sent_bytes += len(to_client)
            partial = writer.publish()
            writer = None
            close_delimited = framed_length is None and not response.chunked and not no_body
            complete = (no_body or
                        ((framed_length is not None and observed_bytes == framed_length) or
                         response.chunked or (close_delimited and observation.sse and observation.terminal)) and
                        (not observation.sse or (observation.terminal and observation.framing_complete())))
            if not complete:
                reason = "close_delimited_unverified" if close_delimited else "incomplete_upstream_response"
                self._unknown(caller, request_id, attempt_id, reason, partial)
                if close_delimited:
                    if not response_started:
                        self._response_headers(response, request_id, framed_length, no_body, False)
                        response_started = True
                    self._send_saved_tail(partial, sent_bytes, chunked=False)
                elif not response_started:
                    self._json(502, "incomplete_upstream_response", request_id)
                return
            try:
                self.server.catalog.finalize_attempt(
                    project=caller.project_id, request_id=request_id, attempt_id=attempt_id,
                    outcome="upstream_error" if response.status >= 400 else "completed",
                    status_code=response.status, result_ref=partial,
                    upstream_request_id=upstream_id, usage=observation.result())
            except Exception as exc:
                raise RuntimeError("core_storage_unavailable") from exc
            catalog_committed = True
            try:
                self.server.files.recovery(attempt_id, {"project": caller.project_id, "request_id": request_id,
                                                        "attempt_id": attempt_id, "state": "catalog_committed",
                                                        "response": asdict(partial)})
            except OSError:
                pass
            if not response_started:
                self._response_headers(response, request_id, framed_length, no_body, downstream_chunked)
                response_started = True
            self._send_saved_tail(partial, sent_bytes, chunked=downstream_chunked)
            if not no_body and downstream_chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except Exception as exc:
            if writer is not None:
                try:
                    partial = writer.publish()
                except OSError:
                    # The recovery file points at the staging path for repair.
                    pass
            if not catalog_committed:
                self._unknown(caller, request_id, attempt_id, type(exc).__name__, partial)
            if not response_started:
                try:
                    self._json(503 if isinstance(exc, RuntimeError) else 502,
                               "upstream_or_core_unavailable", request_id)
                except OSError:
                    pass
        finally:
            self.close_connection = True
            conn.close()
