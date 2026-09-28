"""Authenticated loopback API. One process owns the catalog SQLite writer lock."""

from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from typing import Any

from aitoolbox_data import Catalog, CatalogConflict, CatalogUnavailable, LocalHistoryMapper
from aitoolbox_data.cloud_adapter import CloudCatalogAdapter
from aitoolbox_data.resource_import import (get_resource_cursor, import_resource_page,
                                            list_resource_facts)
from aitoolbox_data.local_product_usage import (SOURCE as PRODUCT_SOURCE,
                                                daily_product_usage, scan_receipts)
from .legacy_sync import import_history


_MAX_METADATA_BYTES = 1024 * 1024
_CLOUD = {
    "/v1/cloud/admit", "/v1/cloud/begin", "/v1/cloud/finalize",
    "/v1/cloud/unknown", "/v1/cloud/state",
}
_READ = {"/v1/data/get-chain", "/v1/data/get-chain-by-execution",
         "/v1/data/list-chains", "/v1/data/daily-usage", "/v1/data/resource-facts",
         "/v1/data/local-product-usage"}
_ADMIN = {"/v1/admin/project", "/v1/admin/caller", "/v1/admin/source",
          "/v1/admin/config", "/v1/admin/artifact", "/v1/admin/legacy-cloud-event",
          "/v1/admin/local-page", "/v1/admin/local-cursor",
          "/v1/admin/resource-page", "/v1/admin/resource-cursor"}


def _safe_error(exc: Exception) -> str:
    value = str(exc)
    return value if value and len(value) <= 96 and all(c.isascii() and (c.islower() or c.isdigit() or c == "_") for c in value) else type(exc).__name__


def _record(value: Any, keys: set[str]) -> SimpleNamespace:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid_record_fields")
    return SimpleNamespace(**value)


class DataServiceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], *, catalog: Catalog, file_root: str | Path,
                 runtime_token: str, admin_token: str,
                 legacy_cloud_source: str | Path | None = None,
                 local_product_source: str | Path | None = None,
                 sync_interval_seconds: int = 60):
        if address[0] not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("data_service_must_bind_loopback")
        if not runtime_token or not admin_token or runtime_token == admin_token:
            raise ValueError("distinct_service_tokens_required")
        self.catalog = catalog
        self.adapter = CloudCatalogAdapter(catalog, file_root=file_root)
        self.runtime_hash = hashlib.sha256(runtime_token.encode()).digest()
        self.admin_hash = hashlib.sha256(admin_token.encode()).digest()
        if sync_interval_seconds < 5:
            raise ValueError("legacy_sync_interval_too_short")
        self.legacy_cloud_source = Path(legacy_cloud_source) if legacy_cloud_source else None
        self.local_product_source = Path(local_product_source).resolve() if local_product_source else None
        if self.local_product_source is not None:
            catalog.add_source(PRODUCT_SOURCE, "local_product", "V10 local product receipt JSON",
                               str(self.local_product_source), "read-only hash checked usage projection")
        self.sync_interval_seconds = sync_interval_seconds
        self._sync_stop = Event()
        self._sync_mutex = Lock()
        self._sync_status: dict[str, Any] = {"enabled": self.legacy_cloud_source is not None,
                                             "last_success_utc": None, "source_events": None,
                                             "imported_last_run": None, "error": None}
        self._sync_thread: Thread | None = None
        self._product_status: dict[str, Any] = {"enabled": self.local_product_source is not None,
                                                "last_success_utc": None,
                                                "source_files": None, "imported_last_run": None,
                                                "error": None}
        self._product_thread: Thread | None = None
        super().__init__(address, _Handler)

    def start_legacy_sync(self) -> None:
        if self.legacy_cloud_source is None or self._sync_thread is not None:
            return
        self._sync_thread = Thread(target=self._sync_loop, name="v9-cloud-ledger-sync", daemon=True)
        self._sync_thread.start()

    def _sync_loop(self) -> None:
        while not self._sync_stop.is_set():
            try:
                result = import_history(self.legacy_cloud_source, self.catalog)
                with self._sync_mutex:
                    self._sync_status.update(last_success_utc=result["finished_at_utc"],
                                             source_events=result["source_events"],
                                             imported_last_run=result["imported"], error=None)
            except Exception as exc:
                with self._sync_mutex:
                    self._sync_status["error"] = _safe_error(exc)
            if self._sync_stop.wait(self.sync_interval_seconds):
                break

    def start_product_sync(self) -> None:
        if self.local_product_source is None or self._product_thread is not None:
            return
        self._product_thread = Thread(target=self._product_loop, name="v10-local-product-usage-sync", daemon=True)
        self._product_thread.start()

    def _product_loop(self) -> None:
        from datetime import datetime, timezone
        while not self._sync_stop.is_set():
            try:
                result = scan_receipts(self.catalog, self.local_product_source)
                with self._sync_mutex:
                    self._product_status.update(last_success_utc=datetime.now(timezone.utc).isoformat(),
                                                source_files=result["source_files"],
                                                imported_last_run=result["imported"], error=None)
            except Exception as exc:
                with self._sync_mutex:
                    self._product_status["error"] = _safe_error(exc)
            if self._sync_stop.wait(self.sync_interval_seconds):
                break

    def server_close(self) -> None:
        self._sync_stop.set()
        if self._sync_thread is not None:
            self._sync_thread.join(timeout=30)
        if self._product_thread is not None:
            self._product_thread.join(timeout=30)
        super().server_close()

    def sync_status(self) -> dict[str, Any]:
        with self._sync_mutex:
            return dict(self._sync_status)

    def product_status(self) -> dict[str, Any]:
        with self._sync_mutex:
            return dict(self._product_status)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: DataServiceServer

    def log_message(self, *_: Any) -> None:
        # Routes, IDs and error details may identify private business data.
        return

    def _send(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def _authorized(self, *, admin: bool = False) -> bool:
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        digest = hashlib.sha256(token.encode()).digest()
        expected = self.server.admin_hash if admin else self.server.runtime_hash
        return bool(token) and hmac.compare_digest(digest, expected)

    def _payload(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("metadata_chunking_unsupported")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("content_length_required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid_content_length") from exc
        if length < 0 or length > _MAX_METADATA_BYTES:
            raise ValueError("metadata_too_large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete_metadata")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("metadata_object_required")
        return value

    def do_GET(self) -> None:
        if self.path == "/health":
            try:
                with self.server.catalog._mutex:
                    version = self.server.catalog.verify_schema()
                sync = self.server.sync_status()
                product = self.server.product_status()
                self._send(200, {"status": "ok", "schema_version": version,
                                 "legacy_sync_degraded": bool(sync["error"]),
                                 "legacy_sync": sync,
                                 "local_product_sync_degraded": bool(product["error"]),
                                 "local_product_sync": product})
            except Exception:
                self._send(503, {"status": "unavailable"})
            return
        self._send(404, {"error": "unknown_route"})

    def do_POST(self) -> None:
        path = self.path
        if path not in _CLOUD | _READ | _ADMIN:
            self._send(404, {"error": "unknown_route"})
            return
        if path in _ADMIN:
            authorized = self._authorized(admin=True)
        elif path in _READ:
            authorized = self._authorized() or self._authorized(admin=True)
        else:
            authorized = self._authorized()
        if not authorized:
            self._send(401, {"error": "unauthorized"})
            return
        try:
            args = self._payload()
            with self.server.catalog._mutex:
                result = self._dispatch(path, args)
            self._send(200, {"result": result})
        except CatalogConflict as exc:
            self._send(409, {"error": _safe_error(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            self._send(400, {"error": _safe_error(exc)})
        except (CatalogUnavailable, OSError) as exc:
            self._send(503, {"error": _safe_error(exc)})
        except Exception:
            self._send(503, {"error": "data_service_unavailable"})

    def _dispatch(self, path: str, args: dict[str, Any]) -> Any:
        adapter = self.server.adapter
        catalog = self.server.catalog
        if path == "/v1/cloud/admit":
            args["caller"] = _record(args["caller"], {"caller_id", "project_id"})
            args["input_ref"] = _record(args["input_ref"], {"project", "sha256", "size", "path"})
            args["request_snapshot_ref"] = _record(args["request_snapshot_ref"],
                                                   {"project", "sha256", "size", "path"})
            if args.get("request_wire_ref") is not None:
                args["request_wire_ref"] = _record(args["request_wire_ref"],
                                                   {"project", "sha256", "size", "path"})
            return adapter.admit_request(**args)
        if path == "/v1/cloud/begin":
            return adapter.begin_attempt(**args)
        if path == "/v1/cloud/finalize":
            args["result_ref"] = _record(args["result_ref"], {"project", "sha256", "size", "path"})
            return adapter.finalize_attempt(**args)
        if path == "/v1/cloud/unknown":
            if args.get("partial_ref") is not None:
                args["partial_ref"] = _record(args["partial_ref"], {"project", "sha256", "size", "path"})
            return adapter.mark_unknown(**args)
        if path == "/v1/cloud/state":
            return adapter.get_attempt_state(**args)
        if path == "/v1/data/get-chain":
            if not self._authorized(admin=True) and (not isinstance(args.get("caller_id"), str) or not args["caller_id"]):
                raise ValueError("caller_id_required")
            return catalog.get_chain(**args)
        if path == "/v1/data/get-chain-by-execution":
            if not self._authorized(admin=True) and (not isinstance(args.get("caller_id"), str) or not args["caller_id"]):
                raise ValueError("caller_id_required")
            return catalog.get_chain_by_execution(**args)
        if path == "/v1/data/list-chains":
            if not self._authorized(admin=True) and (not isinstance(args.get("caller_id"), str) or not args["caller_id"]):
                raise ValueError("caller_id_required")
            if args.get("after") is not None:
                after = args["after"]
                if not isinstance(after, list) or len(after) != 2 or type(after[0]) is not int or not isinstance(after[1], str):
                    raise ValueError("invalid_cursor")
                args["after"] = tuple(after)
            return catalog.list_chains(**args)
        if path == "/v1/data/daily-usage":
            return adapter.daily_usage(**args)
        if path == "/v1/data/local-product-usage":
            if not self._authorized(admin=True) or set(args) != {"day_cst"}:
                raise ValueError("local_product_admin_query_required")
            return daily_product_usage(catalog, args["day_cst"])
        if path == "/v1/data/resource-facts":
            if not self._authorized(admin=True):
                caller_id = args.pop("caller_id", None)
                project_id = args.get("project_id")
                if not isinstance(caller_id, str) or not isinstance(project_id, str):
                    raise ValueError("resource_query_scope_required")
                catalog._check_reader(project_id, caller_id)
            else:
                args.pop("caller_id", None)
            return list_resource_facts(catalog, **args)
        if path == "/v1/admin/project":
            return catalog.add_project(**args)
        if path == "/v1/admin/caller":
            return catalog.add_caller(**args)
        if path == "/v1/admin/source":
            return catalog.add_source(**args)
        if path == "/v1/admin/config":
            return catalog.add_config(**args)
        if path == "/v1/admin/artifact":
            return catalog.register_artifact(**args)
        if path == "/v1/admin/legacy-cloud-event":
            return catalog.import_legacy_cloud_event(**args)
        if path == "/v1/admin/local-page":
            if set(args) != {"page"}:
                raise ValueError("invalid_local_page_request")
            return catalog.commit_import_page(args["page"], LocalHistoryMapper())
        if path == "/v1/admin/resource-page":
            if set(args) != {"page"}:
                raise ValueError("invalid_resource_page_request")
            return import_resource_page(catalog, args["page"])
        if path == "/v1/admin/resource-cursor":
            if set(args) != {"kind"}:
                raise ValueError("invalid_resource_cursor_request")
            return get_resource_cursor(catalog, args["kind"])
        if path == "/v1/admin/local-cursor":
            if set(args) != {"source_id", "project_id"}:
                raise ValueError("invalid_local_cursor_request")
            source_id = args["source_id"]
            project_id = args["project_id"]
            if source_id not in {"v9-local-intake", "v9-local-intake-events"} or not isinstance(project_id, str) or not project_id:
                raise ValueError("invalid_local_cursor_key")
            stream = "events" if source_id == "v9-local-intake-events" else "requests"
            row = catalog.db.execute(
                "SELECT * FROM import_cursors WHERE source_id=? AND project_id=? AND stream=?",
                (source_id, project_id, stream)).fetchone()
            return dict(row) if row else None
        raise ValueError("unknown_route")
