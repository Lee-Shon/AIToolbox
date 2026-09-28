"""Idempotently mirror the live V9 cloud usage ledger into the V10 catalog."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Protocol

from aitoolbox_data import Catalog


class _Target(Protocol):
    def register_source(self, source_id: str, kind: str, authority: str,
                        location: str, access_contract: str) -> None: ...
    def import_legacy_cloud_event(self, event: dict, metrics: list[dict]) -> bool: ...


def import_history(source: Path, target: Catalog | _Target) -> dict:
    """Read one consistent V9 SQLite snapshot, preserving row values and unknowns."""
    source = source.resolve(strict=True)
    source_fields = ("v9-cloud-relay", "cloud_ledger", "V9 Cloud Relay state",
                     str(source), "read-only-snapshot")
    if isinstance(target, Catalog):
        target.add_source(*source_fields)
    else:
        target.register_source(*source_fields)
    digest = hashlib.sha256()
    totals = {"source_events": 0, "source_metrics": 0, "imported": 0,
              "already_present": 0, "source": str(source),
              "started_at_utc": datetime.now(timezone.utc).isoformat()}
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("v9_cloud_source_integrity_failed")
        for event_row in db.execute("SELECT * FROM usage_events ORDER BY event_id"):
            event = dict(event_row)
            metrics = [dict(row) for row in db.execute(
                "SELECT metric_name,unit,quantity_decimal,source FROM usage_metrics "
                "WHERE event_id=? ORDER BY metric_name,unit", (event["event_id"],))]
            canonical = json.dumps({"event": event, "metrics": metrics}, sort_keys=True,
                                   ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            digest.update(len(canonical).to_bytes(8, "big"))
            digest.update(canonical)
            created = target.import_legacy_cloud_event(event, metrics)
            totals["source_events"] += 1
            totals["source_metrics"] += len(metrics)
            totals["imported" if created else "already_present"] += 1
        db.rollback()  # End the read snapshot without touching V9.
    totals["canonical_source_sha256"] = digest.hexdigest()
    totals["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return totals
