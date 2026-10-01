"""Runtime-independent registration, authenticated HTTP management and receipts.

LocalAI implementation and generation forwarding live in v11.py.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlsplit
import zlib


CAPABILITIES = {"text_input", "image_input", "audio_input", "text_output"}
MAX_BODY = 32 * 1024 * 1024
RETRYABLE_REGISTRATION_ERRORS = {
    "localai_unavailable",
    "physical_resource_observation_unknown", "physical_resource_observation_stale",
    "validation_interrupted_by_restart", "service_stopping", "validation_worker_start_failed",
    "audio_validation_requires_windows_english_speech_voice",
}


class ProductError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


def red_png() -> bytes:
    width = height = 32
    raw = b"".join(b"\0" + b"\xff\0\0" * width for _ in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        import struct
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    import struct
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class SharedContext:
    """Use V9's shared-KV rule: caller-declared context reservations, not equal slices."""
    def __init__(self, total: int):
        self.total = total
        self.tokens = 0
        self.condition = threading.Condition()
        self.waiters = []
        self.sequence = 0

    def _drain(self) -> None:
        for item in sorted(self.waiters, key=lambda item: (item['order'], item['sequence'])):
            if not item['granted'] and item['tokens'] + self.tokens <= self.total:
                item['granted'] = True
                self.tokens += item['tokens']
        self.condition.notify_all()

    def enqueue(self, tokens: int, order: int | None = None):
        if not 0 < tokens <= self.total:
            raise ProductError("request_exceeds_instance_capacity", 422)
        with self.condition:
            self.sequence += 1
            item = dict(tokens=tokens, order=self.sequence if order is None else order,
                        sequence=self.sequence, granted=False)
            self.waiters.append(item)
            return item

    def cancel(self, item):
        with self.condition:
            if item in self.waiters:
                self.waiters.remove(item)
                if item['granted']:
                    self.tokens -= item['tokens']
                self._drain()

    def acquire(self, tokens: int, order: int | None = None, cancelled=lambda: False, ticket=None) -> None:
        item = ticket or self.enqueue(tokens, order)
        with self.condition:
            try:
                while True:
                    if cancelled():
                        raise ProductError('request_cancelled', 409)
                    self._drain()
                    if item['granted']:
                        return
                    self.condition.wait(.1)
            except BaseException:
                if item['granted']:
                    self.tokens -= tokens
                raise
            finally:
                self.waiters.remove(item)
                self._drain()

    def release(self, tokens: int) -> None:
        with self.condition:
            if not 0 < tokens <= self.tokens:
                raise RuntimeError('shared_context_release_mismatch')
            self.tokens -= tokens
            self._drain()


class Product:
    def __init__(self, root: Path, runtime: Any, data_service_url: str,
                 data_admin_token_file: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        parsed = urlsplit(data_service_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.path not in {"", "/"} or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("data_service_requires_loopback")
        self.data_service_url = data_service_url.rstrip("/")
        self.data_admin_token_file = data_admin_token_file.resolve(strict=True)
        self.registry_path = self.root / "registry.json"
        self.token_path = self.root / "api-token.txt"
        if not self.token_path.exists():
            self.token_path.write_text(secrets.token_urlsafe(40), encoding="ascii")
        self.token = self.token_path.read_text(encoding="ascii").strip()
        if len(self.token) < 32:
            raise ValueError("invalid_product_token")
        self.lock = threading.RLock()
        self.idle = threading.Condition(self.lock)
        self.request_counts: dict[tuple[str, int], int] = {}
        self.closing = False
        self.workers: set[threading.Thread] = set()
        self.runtime = runtime
        self.rows = json.loads(self.registry_path.read_text(encoding="utf-8")) if self.registry_path.exists() else {}
        # A restarted relay cannot prove what an interrupted native call did.
        # Keep its identity and input, and never replay it automatically.
        for receipt in (self.root / "requests").glob("*.json"):
            record = json.loads(receipt.read_text(encoding="utf-8"))
            if record.get("state") == "PENDING":
                record.update(state="UNKNOWN", error="service_restarted_during_request",
                              updated_at=time.time())
                save_json(receipt, record)
        for row in self.rows.values():
            if row["state"] == "UPDATING":
                pending = row.pop("pending_update")
                row.update(state=pending["previous_state"], update_error="update_interrupted_by_restart",
                           last_update={"state": "REJECTED", "revision": pending["revision"],
                                        "error": "update_interrupted_by_restart"})
            # READY records capability evidence, not a resident GPU process.
            # Restarting the API must not launch native work by itself.
            if row["state"] == "VALIDATING":
                row.update(state="REJECTED", error="validation_interrupted_by_restart",
                           updated_at=time.time())
        self._save()

    def _launch(self, target, args) -> None:
        # Caller holds the registry lock, making acceptance and worker tracking
        # atomic with close(). Validation itself must not hold that lock.
        def work():
            try:
                target(*args)
            finally:
                with self.idle:
                    self.workers.discard(worker)
                    self.idle.notify_all()
        worker = threading.Thread(target=work, daemon=True)
        self.workers.add(worker)
        try:
            worker.start()
        except BaseException:
            self.workers.discard(worker)
            raise

    def close(self) -> None:
        with self.idle:
            self.closing = True
            while self.request_counts or self.workers:
                self.idle.wait()
        self.runtime.stop()

    def _save(self) -> None:
        save_json(self.registry_path, self.rows)

    @staticmethod
    def settings(context: Any) -> int:
        if type(context) is not int or not 512 <= context <= 2147483647:
            raise ProductError("invalid_max_context_tokens")
        return context


    def register(self, body: dict) -> tuple[int, dict]:
        spec = self.registration_spec(body)
        model_id = spec["id"]
        with self.lock:
            if self.closing:
                raise ProductError("service_stopping", 503)
            prior = self.rows.get(model_id)
            if prior and prior["state"] != "REMOVED":
                if prior["state"] in {"UPDATING", "DRAINING"}:
                    raise ProductError("model_busy", 409)
                if prior["spec"] != spec:
                    raise ProductError("model_id_conflict", 409)
                if (prior["state"] != "REJECTED"
                        or prior.get("error") not in RETRYABLE_REGISTRATION_ERRORS):
                    return 200, prior.copy()
                # An explicit retry after a resource failure keeps the binding.
                revision = prior["revision"]
            else:
                revision = prior["revision"] + 1 if prior else 1
            row = {**spec, "spec": spec, "revision": revision, "state": "VALIDATING",
                   "assets": {}, "checked": {}, "error": None, "updated_at": time.time()}
            self.rows[model_id] = row
            self._save()
            try:
                self._launch(self._validate, (model_id,))
            except Exception as exc:
                row.update(state="REJECTED", error="validation_worker_start_failed")
                self._save()
                raise ProductError("validation_worker_start_failed", 503) from exc
        return 202, row.copy()

    def _validate(self, model_id: str) -> None:
        with self.lock:
            row = self.rows[model_id].copy()
        result = self._validate_row(row)
        with self.lock:
            current = self.rows.get(model_id)
            if current and current["revision"] == row["revision"] and current["state"] == "VALIDATING":
                current.update(result)
                self._save()


    def update(self, model_id: str, body: dict) -> tuple[int, dict]:
        fields = {"max_context_tokens", "model_path", "mmproj_path", "capabilities"}
        if not isinstance(body, dict) or not fields.intersection(body) or set(body) - fields - {"expected_revision"}:
            raise ProductError("invalid_model_update")
        with self.lock:
            if self.closing:
                raise ProductError("service_stopping", 503)
            row = self.rows.get(model_id)
            if row is None or row["state"] == "REMOVED":
                raise ProductError("model_not_found", 404)
            expected = body.get("expected_revision", row["revision"])
            if type(expected) is not int or expected != row["revision"]:
                raise ProductError("model_revision_conflict", 409)
            if row["state"] not in {"READY", "REJECTED", "UPDATING"}:
                raise ProductError("model_busy", 409)
            spec = self.registration_spec({**row["spec"], **{key: body[key] for key in fields if key in body}})
            if row["state"] == "UPDATING":
                if row["pending_update"]["spec"] == spec:
                    return 202, row.copy()
                raise ProductError("model_update_in_progress", 409)
            if row["spec"] == spec:
                if row.get("update_error"):
                    row["update_error"] = None
                    self._save()
                return 200, row.copy()
            revision = max(row["revision"], row.get("last_update", {}).get("revision", 0)) + 1
            row["pending_update"] = {"spec": spec, "revision": revision, "previous_state": row["state"]}
            row.update(state="UPDATING", update_error=None, updated_at=time.time())
            self._save()
            try:
                self._launch(self._apply_update, (model_id, revision))
            except Exception as exc:
                pending = row.pop("pending_update")
                row.update(state=pending["previous_state"], update_error="validation_worker_start_failed",
                           last_update={"state": "REJECTED", "revision": revision,
                                        "error": "validation_worker_start_failed"})
                self._save()
                raise ProductError("validation_worker_start_failed", 503) from exc
        return 202, self.get(model_id)


    def release_request(self, row: dict) -> None:
        with self.idle:
            key = (row["id"], row["revision"])
            self.request_counts[key] -= 1
            if not self.request_counts[key]:
                del self.request_counts[key]
            self.idle.notify_all()


    def get(self, model_id: str) -> dict:
        with self.lock:
            if model_id not in self.rows:
                raise ProductError("model_not_found", 404)
            return self.rows[model_id].copy()

    def list_ready(self) -> list[dict]:
        with self.lock:
            return [{"id": row["id"], "object": "model", "owned_by": "aitoolbox-v11-localai",
                     "capabilities": row["capabilities"], "max_context_tokens": row["max_context_tokens"],
                     "runtime_shape": row.get("runtime_shape"),
                     "request_context_required": True, "request_context_parameter": "context_tokens",
                     "revision": row["revision"]} for row in self.rows.values() if row["state"] == "READY"]

    def list_all(self) -> list[dict]:
        with self.lock:
            return [{"id": row["id"], "state": row["state"], "error": row.get("error"),
                     "model_path": row["model_path"], "mmproj_path": row.get("mmproj_path"),
                     "capabilities": row["capabilities"], "max_context_tokens": row["max_context_tokens"],
                     "pending_update": row.get("pending_update"),
                     "update_error": row.get("update_error"),
                     "revision": row["revision"]} for row in self.rows.values()]

    def usage_day(self, day: str) -> list[dict]:
        return self.database_usage_day(day)["local"]

    def database_usage_day(self, day: str) -> dict:
        try:
            if datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                raise ValueError
        except ValueError as exc:
            raise ProductError("invalid_usage_day") from exc
        try:
            token = self.data_admin_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProductError("usage_database_credential_unavailable", 503) from exc
        if len(token) < 32:
            raise ProductError("usage_database_credential_invalid", 503)
        payload = json.dumps({"day_cst": day}).encode("utf-8")
        result = {}
        for name, route in (("cloud", "/v1/data/daily-usage"),
                            ("local", "/v1/data/local-product-usage")):
            request = Request(self.data_service_url + route, data=payload, method="POST",
                              headers={"Authorization": "Bearer " + token,
                                       "Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=20) as response:
                    answer = json.load(response)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                raise ProductError("usage_database_unavailable", 503) from exc
            rows = answer.get("result") if isinstance(answer, dict) else None
            if not isinstance(rows, list):
                raise ProductError("usage_database_response_invalid", 503)
            result[name] = rows
        return {"day": day, **result}


class Handler(BaseHTTPRequestHandler):
    server: "Server"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, value: Any, headers: dict | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "request_id", None):
            self.send_header("X-AIToolbox-Request-ID", self.request_id)
        for key, val in (headers or {}).items():
            self.send_header(key, str(val))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> None:
        provided = self.headers.get("Authorization", "")
        if not secrets.compare_digest(provided, "Bearer " + self.server.product.token):
            raise ProductError("invalid_local_token", 401)

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ProductError("invalid_content_length") from exc
        if not 0 < length <= MAX_BODY:
            raise ProductError("invalid_body_size", 413)
        body = self.rfile.read(length)
        if len(body) != length:
            raise ProductError("incomplete_request_body")
        return body

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        self._auth()
        p = self.server.product
        if path == "/v1/models" and self.command == "GET":
            return self._send(200, {"object": "list", "data": p.list_ready()})
        if path == "/admin/models" and self.command == "GET":
            return self._send(200, {"object": "list", "data": p.list_all()})
        if path == "/admin/usage" and self.command == "GET":
            day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
            return self._send(200, {"day": day, "data": p.usage_day(day)})
        if path == "/admin/usage-dashboard" and self.command == "GET":
            day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
            return self._send(200, p.database_usage_day(day))
        if path == "/admin/models" and self.command == "POST":
            try:
                value = json.loads(self._body())
            except (ValueError, UnicodeDecodeError) as exc:
                raise ProductError("invalid_json") from exc
            if not isinstance(value, dict):
                raise ProductError("invalid_registration_body")
            status, result = p.register(value)
            return self._send(status, result)
        if path.startswith("/admin/models/"):
            model_id = path[len("/admin/models/"):]
            if self.command == "GET":
                return self._send(200, p.get(model_id))
            if self.command == "DELETE":
                return self._send(200, p.unregister(model_id))
            if self.command == "PATCH":
                try:
                    value = json.loads(self._body())
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ProductError("invalid_json") from exc
                status, result = p.update(model_id, value)
                return self._send(status, result)
        if path.startswith("/requests/") and self.command == "GET":
            request_id = path[len("/requests/"):]
            if not request_id or not all(c.isalnum() or c in "-_" for c in request_id):
                raise ProductError("invalid_request_id")
            file = p.root / "requests" / (request_id + ".json")
            if not file.is_file():
                raise ProductError("request_not_found", 404)
            with p.lock:
                record = json.loads(file.read_text(encoding="utf-8"))
            return self._send(200, record)
        if path in ("/v1/chat/completions", "/v1/audio/transcriptions") and self.command == "POST":
            return self._proxy(path)
        raise ProductError("unsupported_path", 404)


    def do_GET(self) -> None:
        self._safe()

    def do_POST(self) -> None:
        self._safe()

    def do_DELETE(self) -> None:
        self._safe()

    def do_PATCH(self) -> None:
        self._safe()

    def _safe(self) -> None:
        try:
            self._handle()
        except ProductError as exc:
            self._send(exc.status, {"error": {"code": exc.code}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            if getattr(self, "_proxy_headers_sent", False):
                self.close_connection = True
                return
            try:
                self._send(500, {"error": {"code": "internal_error", "detail": str(exc)}})
            except (BrokenPipeError, ConnectionResetError):
                pass


class Server(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], product: Product,
                 handler: type[Handler]):
        super().__init__(address, handler)
        self.product = product

    def server_close(self) -> None:
        try:
            self.product.close()
        finally:
            super().server_close()
