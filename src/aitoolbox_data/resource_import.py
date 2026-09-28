"""Commit bounded V9 resource metadata pages without assuming resource authority."""

from __future__ import annotations

import json
import re
from typing import Any

from .core import Catalog, CatalogConflict, _now


SOURCE = "v9-local-physical-resources"
KINDS = {"events", "leases", "resource_predictions", "policy"}
HEX = re.compile(r"[0-9a-f]{64}\Z")
SEMANTICS = {
    "events": "append_only_sequence_watermark",
    "leases": "mutable_rows_observed_under_rowid_watermark",
    "resource_predictions": "mutable_rows_observed_under_rowid_watermark",
    "policy": "physical_policy_content_hash",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _natural(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid_resource_" + name)
    return value


def _text(value: Any, name: str, *, limit: int = 2048) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_resource_" + name)
    return value


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or HEX.fullmatch(value) is None:
        raise ValueError("invalid_resource_" + name)
    return value


def _identity(page: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    if not isinstance(page, dict) or page.get("format") != 1 or page.get("source_id") != SOURCE:
        raise ValueError("invalid_resource_page_identity")
    kind = page.get("kind")
    records = page.get("records")
    if kind not in KINDS or not isinstance(records, list) or len(records) > 64:
        raise ValueError("invalid_resource_page_bounds")
    if page.get("snapshot_semantics") != SEMANTICS[kind] or type(page.get("complete")) is not bool:
        raise ValueError("invalid_resource_page_semantics")
    if not isinstance(page.get("watermark"), dict) or not isinstance(page.get("reconciliation"), dict):
        raise ValueError("invalid_resource_page_reconciliation")
    if page["complete"] != (page.get("cursor") is None):
        raise ValueError("invalid_resource_page_cursor")
    if not page["complete"] and not isinstance(page["cursor"], dict):
        raise ValueError("invalid_resource_page_cursor")
    return SOURCE, kind, records


def _record(record: dict[str, Any], kind: str) -> tuple[Any, ...]:
    if not isinstance(record, dict) or record.get("source_id") != SOURCE or record.get("source_kind") != kind:
        raise ValueError("invalid_resource_record_identity")
    source_key = _text(record.get("source_key"), "source_key", limit=256)
    if kind == "policy":
        sha = _sha(record.get("policy_sha256"), "policy_sha256")
        size = _natural(record.get("policy_bytes"), "policy_bytes")
        order_key = None
        rowid = None
        if not source_key.startswith(sha + ":"):
            raise ValueError("invalid_resource_policy_key")
        projection_keys = ("policy_revision", "profile_sha256", "model_id", "budgets_bytes")
    else:
        sha = _sha(record.get("document_sha256"), "document_sha256")
        size = _natural(record.get("document_bytes"), "document_bytes")
        order_key = _natural(record.get("source_order_key"), "source_order_key")
        rowid = _natural(record.get("source_rowid"), "source_rowid")
        if order_key == 0 or rowid == 0:
            raise ValueError("invalid_resource_source_order")
        if kind == "events" and source_key != str(order_key):
            raise ValueError("invalid_resource_event_key")
        projection_keys = ("facts", "event", "token_sha256", "replica_sha256",
                           "identity_sha256", "profile_sha256", "profile_value_is_sha256")
    source_uri = _text(record.get("source_uri"), "source_uri", limit=4096)
    authority = _text(record.get("authority"), "authority", limit=128)
    if not source_uri.startswith("file:///"):
        raise ValueError("invalid_resource_source_uri")
    unknown = record.get("unknown", [])
    if not isinstance(unknown, list) or len(unknown) > 32 or any(not isinstance(x, str) or len(x) > 128 for x in unknown):
        raise ValueError("invalid_resource_unknown")
    project = record.get("project_id")
    request = record.get("request_id")
    if (project is None) != (request is None):
        raise ValueError("invalid_resource_request_link")
    if project is not None:
        _text(project, "project_id", limit=256)
        _text(request, "request_id", limit=256)
        link = record.get("request_link")
        if not isinstance(link, dict) or link.get("project_id") != project or link.get("request_id") != request:
            raise ValueError("invalid_resource_request_link")
    observation = {key: record[key] for key in projection_keys if key in record}
    if kind == "policy" and not isinstance(observation.get("budgets_bytes"), dict):
        raise ValueError("invalid_resource_policy_budgets")
    if kind != "policy" and not isinstance(observation.get("facts"), dict):
        raise ValueError("invalid_resource_facts")
    if len(_json(observation)) > 16384:
        raise ValueError("resource_projection_too_large")
    return (SOURCE, kind, source_key, sha, size, source_uri, order_key, rowid,
            project, request, authority, _json(observation), _json(unknown), _now())


def import_resource_page(catalog: Catalog, page: dict[str, Any]) -> dict[str, Any]:
    source, kind, records = _identity(page)
    watermark = page["watermark"]
    recon = page["reconciliation"]
    expected = _natural(recon.get("expected_rows"), "expected_rows")
    emitted = _natural(recon.get("emitted_rows"), "emitted_rows")
    if emitted < len(records):
        raise ValueError("invalid_resource_emitted_rows")
    if page["complete"]:
        if recon.get("count_matches") is not True or emitted != expected:
            raise ValueError("resource_source_count_mismatch")
    elif recon.get("count_matches") is not None:
        raise ValueError("invalid_resource_partial_reconciliation")
    if kind == "policy":
        _sha(watermark.get("policy_sha256"), "policy_watermark")
        _natural(watermark.get("policy_bytes"), "policy_watermark_bytes")
        if expected != _natural(watermark.get("profile_count"), "policy_profile_count"):
            raise ValueError("resource_policy_count_mismatch")
    else:
        _sha(watermark.get("source_instance"), "source_instance")
        _natural(watermark.get("key"), "watermark_key")
        _natural(watermark.get("event_sequence"), "event_sequence")
        if page["complete"] and recon.get("current_rows_up_to_watermark") != expected:
            raise ValueError("resource_current_count_mismatch")
    rows = [_record(record, kind) for record in records]
    order_keys = [row[6] for row in rows if row[6] is not None]
    if order_keys != sorted(set(order_keys)):
        raise ValueError("resource_page_order_invalid")
    if order_keys and order_keys[-1] > watermark["key"]:
        raise ValueError("resource_page_past_watermark")
    next_cursor = page.get("cursor")
    if next_cursor is not None:
        if next_cursor.get("source_id") != source or next_cursor.get("kind") != kind:
            raise ValueError("invalid_resource_next_cursor")
        if kind != "policy" and (next_cursor.get("source_instance") != watermark["source_instance"] or
                                 next_cursor.get("watermark_key") != watermark["key"] or
                                 next_cursor.get("last_key") != order_keys[-1]):
            raise ValueError("invalid_resource_next_cursor")
        if kind == "policy" and next_cursor.get("policy_sha256") != watermark["policy_sha256"]:
            raise ValueError("invalid_resource_next_cursor")
    # A repeated page may be delivered after a lost acknowledgement. It must
    # carry exactly the already committed checkpoint, never advance twice.
    with catalog._tx() as db:
        old = db.execute("SELECT * FROM resource_import_cursors WHERE source_id=? AND source_kind=?",
                         (source, kind)).fetchone()
        same_checkpoint = old is not None and old["cursor_json"] == (_json(next_cursor) if next_cursor else None) and \
            old["watermark_json"] == _json(watermark) and old["reconciliation_json"] == _json(recon)
        if old is not None and not same_checkpoint and not old["complete"]:
            previous = json.loads(old["cursor_json"])
            if kind == "policy":
                if watermark["policy_sha256"] != previous["policy_sha256"] or emitted <= previous["next_index"]:
                    raise CatalogConflict("resource_page_cursor_conflict")
            else:
                if (watermark["source_instance"] != previous["source_instance"] or
                    watermark["key"] != previous["watermark_key"] or
                    not order_keys or order_keys[0] <= previous["last_key"] or
                    emitted != previous["emitted_rows"] + len(records)):
                    raise CatalogConflict("resource_page_cursor_conflict")
        elif old is not None and not same_checkpoint and old["complete"] and kind == "events":
            previous = json.loads(old["watermark_json"])
            if order_keys and order_keys[0] <= previous["key"]:
                raise CatalogConflict("resource_event_watermark_conflict")
        inserted = 0
        for row in rows:
            if kind == "events":
                prior = db.execute("SELECT content_sha256 FROM resource_source_facts WHERE source_id=? AND source_kind=? AND source_key=?",
                                   (source, kind, row[2])).fetchone()
                if prior is not None and prior[0] != row[3]:
                    raise CatalogConflict("resource_event_content_conflict")
            before = db.total_changes
            db.execute("INSERT INTO resource_source_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", row)
            inserted += db.total_changes - before
        if not same_checkpoint:
            db.execute("""INSERT INTO resource_import_cursors VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id,source_kind) DO UPDATE SET
                cursor_json=excluded.cursor_json,watermark_json=excluded.watermark_json,
                reconciliation_json=excluded.reconciliation_json,
                snapshot_semantics=excluded.snapshot_semantics,last_order_key=excluded.last_order_key,
                complete=excluded.complete,updated_at_ns=excluded.updated_at_ns""",
                       (source, kind, _json(next_cursor) if next_cursor else None,
                        _json(watermark), _json(recon), page["snapshot_semantics"],
                        order_keys[-1] if order_keys else None, int(page["complete"]), _now()))
    return {"inserted": inserted, "records": len(records), "complete": page["complete"],
            "watermark": watermark}


def get_resource_cursor(catalog: Catalog, kind: str) -> dict[str, Any] | None:
    if kind not in KINDS:
        raise ValueError("invalid_resource_kind")
    row = catalog.db.execute("SELECT * FROM resource_import_cursors WHERE source_id=? AND source_kind=?",
                             (SOURCE, kind)).fetchone()
    return dict(row) if row else None


def list_resource_facts(catalog: Catalog, *, source_kind: str, project_id: str | None = None,
                        request_id: str | None = None, after: int = 0, limit: int = 100) -> dict[str, Any]:
    if source_kind not in KINDS or type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("invalid_resource_query")
    if request_id is not None and project_id is None:
        raise ValueError("resource_query_project_required")
    if project_id is not None:
        _text(project_id, "project_id", limit=256)
    if request_id is not None:
        _text(request_id, "request_id", limit=256)
    where = ["source_id=?", "source_kind=?", "rowid>?"]
    params: list[Any] = [SOURCE, source_kind, after]
    if project_id is not None:
        where.append("project_id=?")
        params.append(project_id)
    if request_id is not None:
        where.append("request_id=?")
        params.append(request_id)
    order = "rowid"
    rows = catalog.db.execute("SELECT rowid,* FROM resource_source_facts WHERE " + " AND ".join(where) +
                              " ORDER BY " + order + " LIMIT ?", (*params, limit + 1)).fetchall()
    selected = rows[:limit]
    result = []
    for row in selected:
        item = dict(row)
        item["observation"] = json.loads(item.pop("observation_json"))
        item["unknown"] = json.loads(item.pop("unknown_json"))
        result.append(item)
    return {"records": result, "next_after": selected[-1]["rowid"]
            if len(rows) > limit else None}
