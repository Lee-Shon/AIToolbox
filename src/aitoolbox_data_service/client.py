"""Synchronous client for the single-writer service; uncertain writes are never retried."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from typing import Any

from aitoolbox_data import CatalogConflict, CatalogUnavailable


class DataServiceClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path not in {"", "/"}:
            raise ValueError("data_service_client_requires_loopback")
        if not token or timeout <= 0:
            raise ValueError("invalid_data_service_client_config")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path: str, args: dict[str, Any]) -> Any:
        body = json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(self.base_url + path, data=body, method="POST",
                          headers={"Authorization": "Bearer " + self.token,
                                   "Content-Type": "application/json", "Cache-Control": "no-store"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                value = json.load(response)
        except HTTPError as exc:
            try:
                value = json.load(exc)
                reason = str(value.get("error", "data_service_error"))
            except (ValueError, AttributeError):
                reason = "data_service_error"
            if exc.code == 409:
                raise CatalogConflict(reason) from exc
            if exc.code == 400:
                raise ValueError(reason) from exc
            raise CatalogUnavailable(reason) from exc
        except (URLError, TimeoutError, OSError) as exc:
            # The server may have committed before this connection failed.
            raise CatalogUnavailable("data_service_outcome_unknown") from exc
        if not isinstance(value, dict) or "result" not in value:
            raise CatalogUnavailable("invalid_data_service_response")
        return value["result"]

    @staticmethod
    def _ref(value: Any) -> dict[str, Any]:
        if is_dataclass(value):
            return asdict(value)
        return {name: getattr(value, name) for name in ("project", "sha256", "size", "path")}

    def admit_request(self, *, caller: Any, request_id: str, provider: str,
                      method: str, path: str, model: str | None, input_ref: Any,
                      config_revision: str, request_snapshot_ref: Any,
                      request_wire_ref: Any | None) -> str:
        return self._post("/v1/cloud/admit", {
            "caller": {"caller_id": caller.caller_id, "project_id": caller.project_id},
            "request_id": request_id, "provider": provider, "method": method, "path": path,
            "model": model, "input_ref": self._ref(input_ref), "config_revision": config_revision,
            "request_snapshot_ref": self._ref(request_snapshot_ref),
            "request_wire_ref": self._ref(request_wire_ref) if request_wire_ref is not None else None,
        })

    def begin_attempt(self, *, project: str, request_id: str, attempt_id: str) -> None:
        self._post("/v1/cloud/begin", {"project": project, "request_id": request_id, "attempt_id": attempt_id})

    def finalize_attempt(self, *, project: str, request_id: str, attempt_id: str,
                         outcome: str, status_code: int, result_ref: Any,
                         upstream_request_id: str | None, usage: dict | None) -> None:
        self._post("/v1/cloud/finalize", {
            "project": project, "request_id": request_id, "attempt_id": attempt_id,
            "outcome": outcome, "status_code": status_code,
            "result_ref": self._ref(result_ref), "upstream_request_id": upstream_request_id,
            "usage": usage,
        })

    def mark_unknown(self, *, project: str, request_id: str, attempt_id: str,
                     reason: str, partial_ref: Any | None) -> str:
        return self._post("/v1/cloud/unknown", {
            "project": project, "request_id": request_id, "attempt_id": attempt_id,
            "reason": reason, "partial_ref": self._ref(partial_ref) if partial_ref is not None else None,
        })

    def get_attempt_state(self, project: str, request_id: str, attempt_id: str) -> str | None:
        return self._post("/v1/cloud/state", {"project": project, "request_id": request_id, "attempt_id": attempt_id})

    def get_chain(self, project_id: str, request_id: str, *, caller_id: str | None) -> dict | None:
        return self._post("/v1/data/get-chain", {"project_id": project_id, "request_id": request_id,
                                                  "caller_id": caller_id})

    def get_chain_by_execution(self, project_id: str, execution_id: str, *, caller_id: str | None) -> dict | None:
        return self._post("/v1/data/get-chain-by-execution", {
            "project_id": project_id, "execution_id": execution_id, "caller_id": caller_id})

    def list_chains(self, project_id: str, *, caller_id: str | None, **filters: Any) -> dict:
        return self._post("/v1/data/list-chains", {"project_id": project_id,
                                                   "caller_id": caller_id, **filters})

    def daily_usage(self, day_cst: str) -> list[dict]:
        return self._post("/v1/data/daily-usage", {"day_cst": day_cst})

    def local_product_usage(self, day_cst: str) -> list[dict]:
        return self._post("/v1/data/local-product-usage", {"day_cst": day_cst})

    def register_project(self, project_id: str, display_name: str) -> None:
        self._post("/v1/admin/project", {"project_id": project_id, "display_name": display_name})

    def register_caller(self, caller_id: str, project_id: str, credential_ref: str) -> None:
        self._post("/v1/admin/caller", {"caller_id": caller_id, "project_id": project_id,
                                        "credential_ref": credential_ref})

    def register_source(self, source_id: str, kind: str, authority: str,
                        location: str, access_contract: str) -> None:
        self._post("/v1/admin/source", {"source_id": source_id, "kind": kind,
                                        "authority": authority, "location": location,
                                        "access_contract": access_contract})

    def register_config(self, project_id: str, config_id: str, *, model: str,
                        software_version: str, revision: str, sha256: str | None,
                        snapshot_artifact_id: str | None = None, source_id: str | None = None,
                        unknown_reason: str | None = None) -> None:
        self._post("/v1/admin/config", {"project_id": project_id, "config_id": config_id,
                                        "model": model, "software_version": software_version,
                                        "revision": revision, "sha256": sha256,
                                        "snapshot_artifact_id": snapshot_artifact_id,
                                        "source_id": source_id, "unknown_reason": unknown_reason})

    def register_artifact(self, project_id: str, artifact_id: str, *, role: str,
                          media_type: str, uri: str, sha256: str, bytes: int,
                          source_id: str | None, recovery_state: str) -> None:
        self._post("/v1/admin/artifact", {"project_id": project_id,
                                          "artifact_id": artifact_id,"role":role,
                                          "media_type":media_type,"uri":uri,
                                          "sha256":sha256,"bytes":bytes,
                                          "source_id":source_id,
                                          "recovery_state":recovery_state})

    def import_legacy_cloud_event(self, event: dict, metrics: list[dict]) -> bool:
        return self._post("/v1/admin/legacy-cloud-event", {"event": event, "metrics": metrics})

    def import_local_page(self, page: dict) -> bool:
        return self._post("/v1/admin/local-page", {"page": page})

    def get_local_cursor(self, source_id: str, project_id: str) -> dict | None:
        return self._post("/v1/admin/local-cursor", {
            "source_id": source_id, "project_id": project_id})

    def import_resource_page(self, page: dict) -> dict:
        return self._post("/v1/admin/resource-page", {"page": page})

    def get_resource_cursor(self, kind: str) -> dict | None:
        return self._post("/v1/admin/resource-cursor", {"kind": kind})

    def list_resource_facts(self, source_kind: str, *, caller_id: str | None = None,
                            project_id: str | None = None, request_id: str | None = None,
                            after: int = 0, limit: int = 100) -> dict:
        return self._post("/v1/data/resource-facts", {
            "source_kind": source_kind, "caller_id": caller_id,
            "project_id": project_id, "request_id": request_id,
            "after": after, "limit": limit})
