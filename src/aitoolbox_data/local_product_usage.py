"""Hash checked read projection of V10 local product receipts for usage charts."""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .core import Catalog, CatalogConflict, _now


SOURCE = "v10-local-product-receipts"
MAX_RECEIPT_BYTES = 256 * 1024 * 1024
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
STATES = {"PENDING", "COMPLETED", "FAILED", "UNKNOWN"}


def _text(value: Any, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_product_" + name)
    return value


def _timestamp(value: Any, name: str) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("invalid_product_" + name)
    return int(Decimal(str(value)) * 1_000_000_000)


def _usage(record: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    native = record.get("usage")
    if native is None:
        response = record.get("response")
        if isinstance(response, dict) and isinstance(response.get("body_base64"), str):
            try:
                body = base64.b64decode(response["body_base64"], validate=True)
                payload = json.loads(body)
                native = payload.get("usage") if isinstance(payload, dict) else None
            except (ValueError, TypeError):
                native = None
    if not isinstance(native, dict):
        return None, None, "native_usage_missing"
    values = []
    for name in ("prompt_tokens", "completion_tokens"):
        value = native.get(name)
        values.append(value if type(value) is int and value >= 0 else None)
    reason = None if all(value is not None for value in values) else "native_usage_partial_or_invalid"
    return values[0], values[1], reason


def _parse(path: Path, root: Path) -> tuple[Any, ...]:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or resolved.parent != root / "requests" or not REQUEST_ID.fullmatch(path.stem):
        raise ValueError("unsafe_product_receipt_path")
    before = path.stat()
    if before.st_size > MAX_RECEIPT_BYTES:
        raise ValueError("product_receipt_too_large")
    raw = path.read_bytes()
    after = path.stat()
    if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size) or len(raw) != before.st_size:
        raise ValueError("product_receipt_changed_during_read")
    record = json.loads(raw)
    if not isinstance(record, dict) or record.get("request_id") != path.stem:
        raise ValueError("invalid_product_receipt_identity")
    model = _text(record.get("model"), "model")
    raw_revision = record.get("revision")
    revision = (str(raw_revision) if type(raw_revision) is int and raw_revision >= 0
                else _text(raw_revision, "revision"))
    state = record.get("state")
    if state not in STATES:
        raise ValueError("invalid_product_state")
    created = _timestamp(record.get("created_at"), "created_at")
    updated = _timestamp(record.get("updated_at", record["created_at"]), "updated_at")
    if updated < created:
        raise ValueError("invalid_product_time_order")
    input_tokens, output_tokens, unknown = _usage(record)
    return (SOURCE, path.stem, model, revision, state, created, updated,
            input_tokens, output_tokens, unknown, resolved.as_uri(),
            hashlib.sha256(raw).hexdigest(), len(raw), _now())


def import_receipt(catalog: Catalog, path: Path, root: Path) -> bool:
    row = _parse(path, root.resolve(strict=True))
    with catalog._tx() as db:
        prior = db.execute("SELECT model,model_revision,created_at_ns,updated_at_ns,source_uri,source_sha256 FROM local_product_calls WHERE source_id=? AND request_id=?",
                           row[:2]).fetchone()
        if prior is not None:
            if (prior["model"], prior["model_revision"], prior["created_at_ns"], prior["source_uri"]) != \
                    (row[2], row[3], row[5], row[10]):
                raise CatalogConflict("product_receipt_identity_conflict")
            if prior["source_sha256"] == row[11]:
                return False
            if row[6] < prior["updated_at_ns"]:
                raise CatalogConflict("product_receipt_time_regression")
            db.execute("""UPDATE local_product_calls SET state=?,updated_at_ns=?,input_tokens=?,output_tokens=?,
                usage_unknown_reason=?,source_sha256=?,source_bytes=?,imported_at_ns=?
                WHERE source_id=? AND request_id=?""",
                       (row[4], row[6], row[7], row[8], row[9], row[11], row[12], row[13], row[0], row[1]))
        else:
            db.execute("INSERT INTO local_product_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        db.execute("INSERT INTO local_product_call_versions VALUES (?,?,?,?,?,?)",
                   (row[0], row[1], row[11], row[4], row[6], _now()))
    return True


def scan_receipts(catalog: Catalog, root: Path) -> dict[str, int]:
    root = Path(root).resolve()
    request_dir = root / "requests"
    if not request_dir.exists():
        return {"source_files": 0, "imported": 0, "unchanged": 0}
    if not request_dir.is_dir() or request_dir.is_symlink():
        raise ValueError("unsafe_product_receipt_directory")
    imported = unchanged = files = 0
    for path in sorted(request_dir.glob("*.json")):
        files += 1
        if import_receipt(catalog, path, root):
            imported += 1
        else:
            unchanged += 1
    return {"source_files": files, "imported": imported, "unchanged": unchanged}


def daily_product_usage(catalog: Catalog, day_cst: str) -> list[dict[str, Any]]:
    if not isinstance(day_cst, str) or len(day_cst) != 10:
        raise ValueError("invalid_usage_day")
    try:
        parsed = date.fromisoformat(day_cst)
    except ValueError as exc:
        raise ValueError("invalid_usage_day") from exc
    if parsed.isoformat() != day_cst:
        raise ValueError("invalid_usage_day")
    zone = timezone(timedelta(hours=8))
    start = int(datetime(parsed.year, parsed.month, parsed.day, tzinfo=zone).timestamp() * 1_000_000_000)
    end = int((datetime(parsed.year, parsed.month, parsed.day, tzinfo=zone) + timedelta(days=1)).timestamp() * 1_000_000_000)
    rows = catalog.db.execute("""SELECT model,state,input_tokens,output_tokens FROM local_product_calls
        WHERE created_at_ns>=? AND created_at_ns<? ORDER BY model,request_id""", (start, end)).fetchall()
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        model = row["model"]
        item = totals.setdefault(model, {"model": model, "calls": 0, "completed": 0,
                                         "failed": 0, "unknown": 0, "usage_known_calls": 0,
                                         "input_tokens": None, "output_tokens": None})
        item["calls"] += 1
        state = row["state"]
        item["completed" if state == "COMPLETED" else "failed" if state == "FAILED" else "unknown"] += 1
        if row["input_tokens"] is not None and row["output_tokens"] is not None:
            item["usage_known_calls"] += 1
        for field in ("input_tokens", "output_tokens"):
            if row[field] is not None:
                item[field] = (item[field] or 0) + row[field]
    return sorted(totals.values(), key=lambda item: (-item["calls"], item["model"]))
