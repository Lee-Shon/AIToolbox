from __future__ import annotations

import ctypes
import hashlib
import hmac
import secrets
import sqlite3
import sys
import uuid
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


_CST = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
  provider TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS callers (
  id TEXT PRIMARY KEY, token_sha256 TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS usage_events (
  event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, day_cst TEXT NOT NULL,
  caller TEXT NOT NULL, provider TEXT NOT NULL, model TEXT,
  method TEXT NOT NULL, path TEXT NOT NULL, status_code INTEGER,
  usage_status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
  total_tokens INTEGER, upstream_request_id TEXT, client_request_id TEXT,
  outcome TEXT NOT NULL DEFAULT 'legacy_unknown'
);
CREATE INDEX IF NOT EXISTS usage_day_caller ON usage_events(day_cst, caller, provider, model);
CREATE TABLE IF NOT EXISTS usage_metrics (
  event_id TEXT NOT NULL REFERENCES usage_events(event_id) ON DELETE RESTRICT,
  metric_name TEXT NOT NULL, unit TEXT NOT NULL,
  quantity_decimal TEXT NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (event_id, metric_name, unit)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fault_events (
  fault_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
  phase TEXT NOT NULL, code TEXT NOT NULL, event_id TEXT,
  FOREIGN KEY (event_id) REFERENCES usage_events(event_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS faults_occurred_at ON fault_events(occurred_at);
"""


if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    _crypt = ctypes.windll.crypt32
    _kernel = ctypes.windll.kernel32
    _crypt.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR,
                                        ctypes.POINTER(_Blob), ctypes.c_void_p,
                                        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt.CryptProtectData.restype = wintypes.BOOL
    _crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob),
                                          ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob),
                                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(_Blob)]
    _crypt.CryptUnprotectData.restype = wintypes.BOOL
    _kernel.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel.LocalFree.restype = ctypes.c_void_p

    def _blob(value: bytes) -> tuple[_Blob, Any]:
        buffer = ctypes.create_string_buffer(value)
        return _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer

    def _dpapi(value: bytes, decrypt: bool = False) -> bytes:
        source, keepalive = _blob(value)
        target = _Blob()
        if decrypt:
            description = wintypes.LPWSTR()
            ok = _crypt.CryptUnprotectData(ctypes.byref(source), ctypes.byref(description),
                                            None, None, None, 1, ctypes.byref(target))
        else:
            ok = _crypt.CryptProtectData(ctypes.byref(source), None, None, None,
                                          None, 1, ctypes.byref(target))
        if not ok:
            raise OSError(ctypes.get_last_error(), "Windows DPAPI failed")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            _kernel.LocalFree(target.data)
else:
    def _dpapi(value: bytes, decrypt: bool = False) -> bytes:
        raise RuntimeError("provider_secrets_require_windows_dpapi")


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.parent.is_dir():
            raise FileNotFoundError(f"state_directory_missing: {self.path.parent}")
        with closing(self._connect()) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            existing = db.execute("""SELECT name FROM sqlite_master WHERE type='table'
                                     AND name IN ('credentials','callers','usage_events')
                                     LIMIT 1""").fetchone()
            if version == 0 and existing:
                raise RuntimeError("state_migration_required")
            if version not in (0, SCHEMA_VERSION):
                raise RuntimeError(f"unsupported_state_version: {version}")
            if version == 0:
                db.executescript(_SCHEMA)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.commit()
            else:
                required = {"credentials", "callers", "usage_events", "usage_metrics", "fault_events"}
                present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not required <= present:
                    raise RuntimeError("state_schema_incomplete")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def set_provider_key(self, provider: str, key: str) -> None:
        if not key.strip():
            raise ValueError("empty_provider_key")
        encrypted = _dpapi(key.strip().encode("utf-8"))
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO credentials(provider,ciphertext,updated_at) VALUES (?,?,?) "
                       "ON CONFLICT(provider) DO UPDATE SET ciphertext=excluded.ciphertext,updated_at=excluded.updated_at",
                       (provider, encrypted, datetime.now(timezone.utc).isoformat()))

    def delete_provider_key(self, provider: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM credentials WHERE provider=?", (provider,))

    def provider_key(self, provider: str) -> str | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT ciphertext FROM credentials WHERE provider=?", (provider,)).fetchone()
        return _dpapi(row["ciphertext"], decrypt=True).decode("utf-8") if row else None

    def has_provider_key(self, provider: str) -> bool:
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM credentials WHERE provider=?", (provider,)).fetchone() is not None

    def add_caller(self, name: str) -> str:
        if not name or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in name):
            raise ValueError("caller_id_must_be_lowercase_ascii")
        token = "v9_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO callers(id,token_sha256,created_at) VALUES (?,?,?)",
                       (name, digest, datetime.now(timezone.utc).isoformat()))
        return token

    def caller_for(self, token: str) -> str | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(self._connect()) as db:
            rows = db.execute("SELECT id,token_sha256 FROM callers WHERE revoked_at IS NULL").fetchall()
        for row in rows:
            if hmac.compare_digest(digest, row["token_sha256"]):
                return row["id"]
        return None

    def revoke_caller(self, name: str) -> bool:
        with closing(self._connect()) as db, db:
            result = db.execute("UPDATE callers SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                (datetime.now(timezone.utc).isoformat(), name))
            return result.rowcount == 1

    def callers(self) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("SELECT id,created_at,revoked_at FROM callers ORDER BY id")]

    def record(self, *, caller: str, provider: str, model: str | None, method: str,
               path: str, status_code: int | None, usage: dict | None,
               upstream_request_id: str | None, client_request_id: str | None,
               outcome: str = "completed") -> None:
        now = datetime.now(timezone.utc)
        status = usage.get("source", "provider") if usage is not None else "unknown"
        event_id = str(uuid.uuid4())
        with closing(self._connect()) as db, db:
            db.execute("""INSERT INTO usage_events
                       (event_id,occurred_at,day_cst,caller,provider,model,method,path,status_code,
                        usage_status,input_tokens,output_tokens,total_tokens,upstream_request_id,
                        client_request_id,outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (event_id, now.isoformat(), now.astimezone(_CST).date().isoformat(),
                        caller, provider, model, method, path, status_code, status,
                        usage.get("input_tokens") if usage else None,
                        usage.get("output_tokens") if usage else None,
                        usage.get("total_tokens") if usage else None,
                        upstream_request_id, client_request_id, outcome))
            for metric in (usage or {}).get("metrics", ()):
                db.execute("""INSERT INTO usage_metrics
                           (event_id,metric_name,unit,quantity_decimal,source)
                           VALUES (?,?,?,?,?)""",
                           (event_id, metric["name"], metric["unit"],
                            metric["value"], "provider"))

    def record_fault(self, phase: str, code: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO fault_events(fault_id,occurred_at,phase,code) VALUES (?,?,?,?)",
                       (str(uuid.uuid4()), datetime.now(timezone.utc).isoformat(), phase, code))

    def daily(self, day: str) -> list[dict]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("invalid_day")
        datetime.strptime(day, "%Y-%m-%d")
        with closing(self._connect()) as db:
            rows = db.execute("""
              SELECT caller,provider,model,COUNT(*) AS calls,
                     SUM(CASE WHEN usage_status='unknown' THEN 1 ELSE 0 END) AS unknown_calls,
                     SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
                     SUM(total_tokens) AS total_tokens
                FROM usage_events WHERE day_cst=?
               GROUP BY caller,provider,model ORDER BY calls DESC,caller,provider,model
            """, (day,)).fetchall()
            metric_rows = db.execute("""
              SELECT e.caller,e.provider,e.model,m.metric_name,m.unit,m.quantity_decimal
                FROM usage_metrics m JOIN usage_events e ON e.event_id=m.event_id
               WHERE e.day_cst=?
            """, (day,)).fetchall()
        result = [dict(row) for row in rows]
        positions = {(row["caller"], row["provider"], row["model"]): row for row in result}
        totals: dict[tuple[str, str, str | None, str, str], Decimal] = {}
        for row in metric_rows:
            key = (row["caller"], row["provider"], row["model"],
                   row["metric_name"], row["unit"])
            totals[key] = totals.get(key, Decimal(0)) + Decimal(row["quantity_decimal"])
        for row in result:
            row["native_usage"] = []
        for (caller, provider, model, name, unit), quantity in sorted(totals.items(), key=lambda x: str(x[0])):
            positions[(caller, provider, model)]["native_usage"].append(
                {"metric_name": name, "unit": unit, "quantity_decimal": str(quantity)})
        return result
