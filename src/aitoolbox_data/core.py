"""Single-writer business catalog. Native engines remain their own execution authorities."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from typing import Any, Callable, Iterator


class CatalogConflict(ValueError):
    pass


class CatalogUnavailable(RuntimeError):
    pass


_MIGRATIONS = Path(__file__).with_name("migrations")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
_STATES = {"ACCEPTED", "QUEUED", "RUNNING", "WAITING_RECOVERY", "UNKNOWN", "COMPLETED", "FAILED", "CANCELLED"}


def _now() -> int:
    return time.time_ns()


def _required(value: str, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"invalid_{name}")
    return value


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"invalid_{name}")
    return value


def _sql_statements(script: str) -> Iterator[str]:
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        raise ValueError("incomplete_migration")


def _lock_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise CatalogUnavailable("writer_already_running") from exc
    else:
        import fcntl
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CatalogUnavailable("writer_already_running") from exc


def _unlock_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)


class ManagedArtifacts:
    """Publish exact business bytes once; the catalog holds ownership references."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def publish(self, project_id: str, source: Any, *, suffix: str = ".bin") -> dict[str, Any]:
        _required(project_id, "project_id")
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            raise ValueError("invalid_suffix")
        project_key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:20]
        folder = self.root / project_key
        folder.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".publish-", dir=folder)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise ValueError("artifact_stream_must_be_bytes")
                    digest.update(chunk)
                    size += len(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            destination = folder / (digest.hexdigest() + suffix)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.stat().st_size != size or self.hash_file(destination) != digest.hexdigest():
                    raise CatalogConflict("artifact_hash_collision_or_corruption")
            self._sync_dir(folder)
            return {"uri": str(destination), "sha256": digest.hexdigest(), "bytes": size}
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _sync_dir(path: Path) -> None:
        if os.name != "nt":
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def verify(self, project_id: str, uri: str, sha256: str, bytes: int) -> bool:
        key = hashlib.sha256(_required(project_id, "project_id").encode()).hexdigest()[:20]
        path = Path(uri).resolve()
        if path.parent != self.root / key:
            raise CatalogConflict("artifact_project_boundary")
        return path.stat().st_size == bytes and self.hash_file(path) == _digest(sha256, "sha256")


class Catalog:
    """One process owns the write lock for its lifetime; all mutations are transactions."""

    def __init__(self, path: Path, *, migrate: bool = False):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = (self.path.parent / (self.path.name + ".writer.lock")).open("a+b")
        if self._lock.seek(0, os.SEEK_END) == 0:
            self._lock.write(b"0")
            self._lock.flush()
        self._owns_lock = False
        try:
            _lock_file(self._lock)
            self._owns_lock = True
            self._mutex = threading.RLock()
            self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA busy_timeout=10000")
            if migrate:
                self.migrate()
            else:
                self.verify_schema()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self, "db", None) is not None:
            self.db.close()
            self.db = None
        if getattr(self, "_lock", None) is not None:
            try:
                if self._owns_lock:
                    _unlock_file(self._lock)
            finally:
                self._lock.close()
                self._lock = None

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        if self.db is None:
            raise CatalogUnavailable("catalog_closed")
        with self._mutex:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def migrate(self) -> list[int]:
        current = self.db.execute("PRAGMA user_version").fetchone()[0]
        scripts = sorted(_MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        versions = [int(p.name[:4]) for p in scripts]
        if versions != list(range(1, len(scripts) + 1)):
            raise CatalogUnavailable("migration_sequence_invalid")
        if current == 0:
            existing = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            if existing:
                raise CatalogUnavailable("unversioned_schema_requires_manual_migration")
        if current > len(scripts):
            raise CatalogUnavailable("database_schema_newer_than_code")
        if current:
            self.verify_schema(expected=current)
        applied = []
        for path in scripts[current:]:
            version = int(path.name[:4])
            body = path.read_bytes()
            with self._tx() as db:
                for sql in _sql_statements(body.decode("utf-8")):
                    db.execute(sql)
                db.execute("INSERT INTO schema_migrations VALUES (?,?,?,?)",
                           (version, path.name, hashlib.sha256(body).hexdigest(), _now()))
                db.execute(f"PRAGMA user_version={version}")
            applied.append(version)
        self.verify_schema()
        return applied

    def verify_schema(self, *, expected: int | None = None) -> int:
        scripts = sorted(_MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version != (len(scripts) if expected is None else expected):
            raise CatalogUnavailable("schema_version_mismatch")
        try:
            rows = self.db.execute("SELECT version,name,sha256 FROM schema_migrations ORDER BY version").fetchall()
        except sqlite3.Error as exc:
            raise CatalogUnavailable("migration_ledger_missing") from exc
        if len(rows) != version:
            raise CatalogUnavailable("migration_ledger_incomplete")
        for row, path in zip(rows, scripts):
            if row["version"] > version:
                break
            if row["name"] != path.name or row["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
                raise CatalogUnavailable("migration_hash_mismatch")
        return version

    def add_project(self, project_id: str, display_name: str) -> None:
        _required(project_id, "project_id"); _required(display_name, "display_name")
        with self._tx() as db:
            db.execute("INSERT INTO projects VALUES (?,?,?) ON CONFLICT(project_id) DO NOTHING", (project_id,display_name,_now()))
            if db.execute("SELECT display_name FROM projects WHERE project_id=?",(project_id,)).fetchone()[0] != display_name:
                raise CatalogConflict("project_identity_conflict")

    def add_caller(self, caller_id: str, project_id: str, credential_ref: str) -> None:
        for name,value in (("caller_id",caller_id),("project_id",project_id),("credential_ref",credential_ref)):
            _required(value,name)
        if any(x in credential_ref.lower() for x in ("bearer ","sk-","api_key=","token=")):
            raise ValueError("credential_ref_must_not_contain_secret")
        with self._tx() as db:
            db.execute("INSERT INTO callers VALUES (?,?,?,?,?) ON CONFLICT(caller_id) DO NOTHING",(caller_id,project_id,credential_ref,1,_now()))
            row=db.execute("SELECT project_id,credential_ref,active FROM callers WHERE caller_id=?",(caller_id,)).fetchone()
            if tuple(row)!=(project_id,credential_ref,1):
                raise CatalogConflict("caller_identity_conflict")

    def add_source(self, source_id: str, kind: str, authority: str, location: str, access_contract: str) -> None:
        values=[_required(v,n) for n,v in (("source_id",source_id),("kind",kind),("authority",authority),("location",location),("access_contract",access_contract))]
        with self._tx() as db:
            db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?) ON CONFLICT(source_id) DO NOTHING",(*values,_now()))
            if tuple(db.execute("SELECT source_kind,authority,location,access_contract FROM sources WHERE source_id=?",(source_id,)).fetchone()) != tuple(values[1:]):
                raise CatalogConflict("source_identity_conflict")

    def register_artifact(self, project_id: str, artifact_id: str, *, role: str, media_type: str,
                          uri: str, sha256: str, bytes: int, source_id: str | None = None,
                          recovery_state: str = "source_only") -> None:
        for name,value in (("project_id",project_id),("artifact_id",artifact_id),("role",role),("media_type",media_type),("uri",uri),("recovery_state",recovery_state)):
            _required(value,name,4096 if name=="uri" else 256)
        if "?" in uri or "#" in uri or uri.lower().startswith(("http://","https://")):
            raise ValueError("artifact_uri_must_be_durable_private_location")
        _digest(sha256,"sha256")
        if type(bytes) is not int or bytes < 0: raise ValueError("invalid_bytes")
        if source_id is not None: _required(source_id,"source_id")
        with self._tx() as db:
            db.execute("""INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id,artifact_id) DO NOTHING""",
                (project_id,artifact_id,role,media_type,uri,sha256,bytes,source_id,recovery_state,_now()))
            row=db.execute("SELECT role,media_type,uri,sha256,bytes,source_id,recovery_state FROM artifacts WHERE project_id=? AND artifact_id=?",(project_id,artifact_id)).fetchone()
            if tuple(row)!=(role,media_type,uri,sha256,bytes,source_id,recovery_state):
                raise CatalogConflict("artifact_identity_conflict")

    def link_artifact(self, project_id: str, request_id: str, artifact_id: str, role: str) -> bool:
        for n,v in (("project_id",project_id),("request_id",request_id),("artifact_id",artifact_id),("role",role)):_required(v,n)
        with self._tx() as db:
            cursor=db.execute("INSERT INTO artifact_links VALUES (?,?,?,?,?) ON CONFLICT(project_id,request_id,artifact_id,role) DO NOTHING",
                              (project_id,request_id,artifact_id,role,_now()))
            return cursor.rowcount==1

    def register_source_reference(self, *, project_id: str, request_id: str, ref_id: str,
                                  source_id: str, role: str, uri: str,
                                  observed_sha256: str | None, observed_bytes: int | None,
                                  recovery_state: str, unknown_reason: str | None = None) -> bool:
        for n,v in (("project_id",project_id),("request_id",request_id),("ref_id",ref_id),
                    ("source_id",source_id),("role",role),("uri",uri),("recovery_state",recovery_state)):_required(v,n,4096 if n=="uri" else 256)
        if "?" in uri or uri.lower().startswith(("http://","https://")):
            raise ValueError("source_reference_must_be_private_location")
        if observed_sha256 is not None:_digest(observed_sha256,"observed_sha256")
        if observed_bytes is not None and (type(observed_bytes) is not int or observed_bytes<0):
            raise ValueError("invalid_observed_bytes")
        if observed_sha256 is None and unknown_reason is None:
            raise ValueError("unknown_reference_reason_required")
        with self._tx() as db:
            row=db.execute("SELECT source_id,role,uri,observed_sha256,observed_bytes,recovery_state,unknown_reason FROM source_references WHERE project_id=? AND request_id=? AND ref_id=?",
                           (project_id,request_id,ref_id)).fetchone()
            expected=(source_id,role,uri,observed_sha256,observed_bytes,recovery_state,unknown_reason)
            if row:
                if tuple(row)!=expected:raise CatalogConflict("source_reference_identity_conflict")
                return False
            db.execute("INSERT INTO source_references VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (project_id,request_id,ref_id,source_id,role,uri,observed_sha256,
                        observed_bytes,recovery_state,unknown_reason,_now()))
            return True

    def add_config(self, project_id: str, config_id: str, *, model: str, software_version: str,
                   revision: str, sha256: str | None, snapshot_artifact_id: str | None = None,
                   source_id: str | None = None, unknown_reason: str | None = None) -> None:
        for n,v in (("project_id",project_id),("config_id",config_id),("model",model),("software_version",software_version),("revision",revision)):_required(v,n)
        if sha256 is None:
            _required(unknown_reason,"unknown_reason")
            knowledge="unknown"
        else:
            _digest(sha256,"sha256")
            if unknown_reason is not None:raise ValueError("known_config_has_unknown_reason")
            knowledge="known"
        with self._tx() as db:
            db.execute("""INSERT INTO config_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id,config_id) DO NOTHING""",
                (project_id,config_id,model,software_version,revision,sha256,knowledge,unknown_reason,snapshot_artifact_id,source_id,_now()))
            row=db.execute("SELECT model,software_version,revision,sha256,knowledge,unknown_reason,snapshot_artifact_id,source_id FROM config_revisions WHERE project_id=? AND config_id=?",(project_id,config_id)).fetchone()
            if tuple(row)!=(model,software_version,revision,sha256,knowledge,unknown_reason,snapshot_artifact_id,source_id):
                raise CatalogConflict("config_identity_conflict")

    def admit_request(self, *, project_id: str, caller_id: str | None, object_id: str,
                      batch_id: str, manifest_sha256: str, request_id: str, model: str,
                      config_id: str, input_artifact_id: str | None, request_fingerprint: str,
                      source_id: str | None = None, source_key: str | None = None,
                      external_batch_id: str | None = None,
                      batch_source_key: str | None = None) -> dict[str, Any]:
        for n,v in (("project_id",project_id),("object_id",object_id),("batch_id",batch_id),("request_id",request_id),("model",model),("config_id",config_id)):_required(v,n)
        _digest(manifest_sha256,"manifest_sha256");_digest(request_fingerprint,"request_fingerprint")
        external_batch_id=external_batch_id or batch_id
        _required(external_batch_id,"external_batch_id")
        if (source_id is None)!=(source_key is None):raise ValueError("source_identity_pair_required")
        batch_source_key=(batch_source_key or batch_id) if source_id else None
        with self._tx() as db:
            if caller_id is not None:
                row=db.execute("SELECT active FROM callers WHERE project_id=? AND caller_id=?",(project_id,caller_id)).fetchone()
                if row is None or not row[0]:raise CatalogConflict("caller_not_authorized_for_project")
            stamp=_now()
            db.execute("INSERT INTO objects VALUES (?,?,?,?) ON CONFLICT(project_id,object_id) DO NOTHING",(project_id,object_id,source_id,stamp))
            object_source=db.execute("SELECT source_id FROM objects WHERE project_id=? AND object_id=?",
                                     (project_id,object_id)).fetchone()[0]
            if object_source!=source_id:raise CatalogConflict("object_source_conflict")
            db.execute("""INSERT INTO batches VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(project_id,batch_id) DO NOTHING""",
                       (project_id,batch_id,external_batch_id,object_id,manifest_sha256,source_id,batch_source_key,stamp))
            b=db.execute("SELECT external_batch_id,object_id,manifest_sha256,source_id,source_key FROM batches WHERE project_id=? AND batch_id=?",(project_id,batch_id)).fetchone()
            if tuple(b)!=(external_batch_id,object_id,manifest_sha256,source_id,batch_source_key):raise CatalogConflict("batch_identity_conflict")
            existing=db.execute("SELECT batch_id,caller_id,model,config_id,input_artifact_id,request_fingerprint,source_id,source_key,state FROM requests WHERE project_id=? AND request_id=?",(project_id,request_id)).fetchone()
            expected=(batch_id,caller_id,model,config_id,input_artifact_id,request_fingerprint,source_id,source_key)
            if existing:
                if tuple(existing)[:8]!=expected:raise CatalogConflict("request_identity_conflict")
                return {"created":False,"state":existing["state"],"request_id":request_id}
            db.execute("""INSERT INTO requests
                (project_id,request_id,batch_id,caller_id,model,config_id,input_artifact_id,
                 request_fingerprint,source_id,source_key,state,revision,created_at_ns,updated_at_ns)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (project_id,request_id,batch_id,caller_id,model,config_id,input_artifact_id,
                        request_fingerprint,source_id,source_key,"ACCEPTED",0,stamp,stamp))
            return {"created":True,"state":"ACCEPTED","request_id":request_id}

    def advance_attempt(self, *, project_id: str, request_id: str, attempt: int,
                        execution_id: str, event_key: str, state: str,
                        expected_revision: int | None = None, source_id: str | None = None,
                        occurred_at_ns: int | None = None, error_code: str | None = None,
                        receipt_artifact_id: str | None = None,
                        stop_proof_artifact_id: str | None = None) -> dict[str, Any]:
        for n,v in (("project_id",project_id),("request_id",request_id),("execution_id",execution_id),("event_key",event_key)):_required(v,n)
        if state not in _STATES or state=="COMPLETED":raise ValueError("invalid_attempt_state")
        if type(attempt) is not int or attempt<1:raise ValueError("invalid_attempt")
        if state in {"FAILED","CANCELLED"} and stop_proof_artifact_id is None:
            raise ValueError("terminal_stop_proof_required")
        with self._tx() as db:
            request=db.execute("SELECT state,revision FROM requests WHERE project_id=? AND request_id=?",(project_id,request_id)).fetchone()
            if request is None:raise CatalogConflict("request_not_found")
            prior=db.execute("SELECT state,error_code,source_id,occurred_at_ns FROM transitions WHERE project_id=? AND request_id=? AND attempt=? AND event_key=?",(project_id,request_id,attempt,event_key)).fetchone()
            if prior:
                identity=db.execute("SELECT execution_id FROM attempts WHERE project_id=? AND request_id=? AND attempt=?",(project_id,request_id,attempt)).fetchone()
                if identity is None or identity[0]!=execution_id:raise CatalogConflict("execution_identity_conflict")
                if tuple(prior)!=(state,error_code,source_id,occurred_at_ns):raise CatalogConflict("transition_identity_conflict")
                return {"created":False,"state":state,"revision":request["revision"]}
            if request["state"] in _TERMINAL:raise CatalogConflict("request_already_terminal")
            if expected_revision is not None and request["revision"]!=expected_revision:raise CatalogConflict("stale_request_revision")
            row=db.execute("SELECT execution_id,state,revision FROM attempts WHERE project_id=? AND request_id=? AND attempt=?",(project_id,request_id,attempt)).fetchone()
            newest=db.execute("SELECT MAX(attempt) FROM attempts WHERE project_id=? AND request_id=?",(project_id,request_id)).fetchone()[0]
            if newest is not None and attempt<newest:raise CatalogConflict("older_attempt_cannot_advance")
            stamp=_now()
            if row is None:
                if attempt!=1:
                    previous=db.execute("SELECT state,stop_proof_artifact_id FROM attempts WHERE project_id=? AND request_id=? AND attempt=?",(project_id,request_id,attempt-1)).fetchone()
                    if previous is None:raise CatalogConflict("attempt_gap")
                    if previous["state"]!="FAILED" or previous["stop_proof_artifact_id"] is None:
                        raise CatalogConflict("previous_attempt_not_safely_stopped")
                db.execute("INSERT INTO attempts(project_id,request_id,attempt,execution_id,state,created_at_ns,updated_at_ns) VALUES (?,?,?,?,?,?,?)",(project_id,request_id,attempt,execution_id,"ACCEPTED",stamp,stamp))
            elif row["execution_id"]!=execution_id:raise CatalogConflict("execution_identity_conflict")
            elif row["state"] in _TERMINAL:raise CatalogConflict("attempt_already_terminal")
            elif row["state"]=="RUNNING" and state in {"ACCEPTED","QUEUED"}:
                raise CatalogConflict("attempt_state_regression")
            db.execute("""UPDATE attempts SET state=?,revision=revision+1,receipt_artifact_id=COALESCE(?,receipt_artifact_id),
                stop_proof_artifact_id=COALESCE(?,stop_proof_artifact_id),error_code=?,updated_at_ns=?
                WHERE project_id=? AND request_id=? AND attempt=?""",
                (state,receipt_artifact_id,stop_proof_artifact_id,error_code,stamp,project_id,request_id,attempt))
            request_state="WAITING_RECOVERY" if state=="FAILED" else state
            db.execute("UPDATE requests SET state=?,revision=revision+1,updated_at_ns=? WHERE project_id=? AND request_id=?",(request_state,stamp,project_id,request_id))
            db.execute("INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?,?)",(project_id,request_id,attempt,event_key,state,error_code,source_id,occurred_at_ns,stamp))
            return {"created":True,"state":state,"revision":request["revision"]+1}

    def finalize_failure(self, *, project_id: str, request_id: str, attempt: int,
                         event_key: str, source_id: str | None = None) -> bool:
        for n,v in (("project_id",project_id),("request_id",request_id),("event_key",event_key)):_required(v,n)
        with self._tx() as db:
            row=db.execute("SELECT state,stop_proof_artifact_id,error_code FROM attempts WHERE project_id=? AND request_id=? AND attempt=?",(project_id,request_id,attempt)).fetchone()
            if row is None or row["state"]!="FAILED" or row["stop_proof_artifact_id"] is None:
                raise CatalogConflict("failed_attempt_with_stop_proof_required")
            newest=db.execute("SELECT MAX(attempt) FROM attempts WHERE project_id=? AND request_id=?",(project_id,request_id)).fetchone()[0]
            if newest!=attempt:raise CatalogConflict("older_attempt_cannot_finalize")
            prior=db.execute("SELECT state FROM transitions WHERE project_id=? AND request_id=? AND attempt=? AND event_key=?",(project_id,request_id,attempt,event_key)).fetchone()
            if prior:
                if prior[0]!="FAILED":raise CatalogConflict("transition_identity_conflict")
                current=db.execute("SELECT state FROM requests WHERE project_id=? AND request_id=?",(project_id,request_id)).fetchone()
                if current is None or current[0]!="FAILED":raise CatalogConflict("finalization_event_conflict")
                return False
            request=db.execute("SELECT state FROM requests WHERE project_id=? AND request_id=?",(project_id,request_id)).fetchone()
            if request is None or request[0] in _TERMINAL:raise CatalogConflict("request_already_terminal")
            stamp=_now()
            db.execute("UPDATE requests SET state='FAILED',revision=revision+1,updated_at_ns=? WHERE project_id=? AND request_id=?",(stamp,project_id,request_id))
            db.execute("INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?,?)",(project_id,request_id,attempt,event_key,"FAILED",row["error_code"],source_id,None,stamp))
            return True

    def publish_result(self, *, project_id: str, request_id: str, attempt: int,
                       execution_id: str, event_key: str, result_artifact_id: str,
                       receipt_artifact_id: str | None = None,
                       source_id: str | None = None) -> dict[str, Any]:
        for n,v in (("project_id",project_id),("request_id",request_id),("execution_id",execution_id),("event_key",event_key),("result_artifact_id",result_artifact_id)):_required(v,n)
        with self._tx() as db:
            prior=db.execute("SELECT result_artifact_id,execution_id,state FROM attempts WHERE project_id=? AND request_id=? AND attempt=?",(project_id,request_id,attempt)).fetchone()
            if prior is None or prior["execution_id"]!=execution_id:raise CatalogConflict("attempt_identity_missing")
            newest=db.execute("SELECT MAX(attempt) FROM attempts WHERE project_id=? AND request_id=?",(project_id,request_id)).fetchone()[0]
            if newest!=attempt:raise CatalogConflict("older_attempt_cannot_complete")
            exists=db.execute("SELECT 1 FROM transitions WHERE project_id=? AND request_id=? AND attempt=? AND event_key=?",(project_id,request_id,attempt,event_key)).fetchone()
            if prior["state"]=="COMPLETED" and exists and prior["result_artifact_id"]==result_artifact_id:
                return {"created":False,"state":"COMPLETED"}
            if prior["state"] in _TERMINAL:raise CatalogConflict("terminal_result_conflict")
            artifact=db.execute("SELECT role FROM artifacts WHERE project_id=? AND artifact_id=?",(project_id,result_artifact_id)).fetchone()
            if artifact is None or artifact["role"]!="result":raise CatalogConflict("result_artifact_missing")
            stamp=_now()
            db.execute("UPDATE attempts SET state='COMPLETED',revision=revision+1,result_artifact_id=?,receipt_artifact_id=COALESCE(?,receipt_artifact_id),updated_at_ns=? WHERE project_id=? AND request_id=? AND attempt=?",(result_artifact_id,receipt_artifact_id,stamp,project_id,request_id,attempt))
            db.execute("UPDATE requests SET state='COMPLETED',revision=revision+1,updated_at_ns=? WHERE project_id=? AND request_id=?",(stamp,project_id,request_id))
            db.execute("INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?,?)",(project_id,request_id,attempt,event_key,"COMPLETED",None,source_id,None,stamp))
            return {"created":True,"state":"COMPLETED"}

    def record_usage(self, *, project_id: str, request_id: str, attempt: int,
                     source_id: str, source_event_key: str, metric_name: str, unit: str,
                     quantity: str | None, provenance: str, unknown_reason: str | None = None) -> bool:
        for n,v in (("project_id",project_id),("request_id",request_id),("source_id",source_id),("source_event_key",source_event_key),("metric_name",metric_name),("unit",unit),("provenance",provenance)):_required(v,n)
        if quantity is None:
            _required(unknown_reason,"unknown_reason")
            knowledge="unknown"
        else:
            try:
                decimal=Decimal(quantity)
                if not decimal.is_finite() or decimal<0:raise ValueError()
                quantity=str(decimal)
            except (InvalidOperation,ValueError,TypeError) as exc:
                raise ValueError("invalid_quantity") from exc
            if unknown_reason is not None:raise ValueError("known_usage_has_unknown_reason")
            knowledge="known"
        with self._tx() as db:
            row=db.execute("SELECT request_id,attempt,quantity_decimal,knowledge,unknown_reason,provenance FROM usage_facts WHERE project_id=? AND source_id=? AND source_event_key=? AND metric_name=? AND unit=?",(project_id,source_id,source_event_key,metric_name,unit)).fetchone()
            expected=(request_id,attempt,quantity,knowledge,unknown_reason,provenance)
            if row:
                if tuple(row)!=expected:raise CatalogConflict("usage_identity_conflict")
                return False
            db.execute("INSERT INTO usage_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(project_id,request_id,attempt,source_id,source_event_key,metric_name,unit,quantity,knowledge,unknown_reason,provenance,_now()))
            return True

    def record_resource(self, *, observation_id: str, source_id: str, source_key: str,
                        metric_name: str, unit: str, quantity: str | None,
                        captured_at_ns: int | None = None, project_id: str | None = None,
                        model: str | None = None, config_id: str | None = None) -> bool:
        for n,v in (("observation_id",observation_id),("source_id",source_id),("source_key",source_key),("metric_name",metric_name),("unit",unit)):_required(v,n)
        if quantity is not None:
            try:
                d=Decimal(quantity)
                if not d.is_finite():raise ValueError()
                quantity=str(d)
            except (InvalidOperation,ValueError,TypeError) as exc:raise ValueError("invalid_quantity") from exc
        knowledge="known" if quantity is not None else "unknown"
        with self._tx() as db:
            row=db.execute("SELECT source_id,project_id,model,config_id,metric_name,unit,quantity_decimal,knowledge,captured_at_ns,source_key FROM resource_observations WHERE observation_id=?",(observation_id,)).fetchone()
            expected=(source_id,project_id,model,config_id,metric_name,unit,quantity,knowledge,captured_at_ns,source_key)
            if row:
                if tuple(row)!=expected:raise CatalogConflict("resource_identity_conflict")
                return False
            db.execute("INSERT INTO resource_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(observation_id,source_id,project_id,model,config_id,metric_name,unit,quantity,knowledge,captured_at_ns,source_key,_now()))
            return True

    def record_gap(self, *, source_id: str, gap_key: str, reason: str,
                   project_id: str | None = None, request_id: str | None = None) -> bool:
        for n,v in (("source_id",source_id),("gap_key",gap_key),("reason",reason)):_required(v,n)
        with self._tx() as db:
            row=db.execute("SELECT reason,project_id,request_id FROM source_gaps WHERE source_id=? AND gap_key=?",(source_id,gap_key)).fetchone()
            if row:
                if tuple(row)!=(reason,project_id,request_id):raise CatalogConflict("gap_identity_conflict")
                return False
            db.execute("INSERT INTO source_gaps VALUES (?,?,?,?,?,?,NULL)",(source_id,gap_key,project_id,request_id,reason,_now()))
            return True

    def commit_import_page(self, page: dict[str, Any],
                           apply_record: Callable[[sqlite3.Connection,dict[str, Any]],None]) -> bool:
        """Commit mapped source facts and their cursor in the same transaction.

        The importer supplies a bounded mapper that writes through the provided
        connection. A mapper must never launch a nested Catalog transaction.
        """
        if not isinstance(page,dict) or page.get("format")!=1 or not isinstance(page.get("records"),list):
            raise ValueError("invalid_import_page")
        source_id=_required(page.get("source_id"),"source_id")
        if source_id not in {"v9-local-intake","v9-local-intake-events"}:
            raise ValueError("unsupported_import_page_source")
        project_id=_required(page.get("project_id"),"project_id")
        source_instance=_digest(page.get("source_instance"),"source_instance")
        records=page["records"]
        if len(records)>16:raise ValueError("import_page_exceeds_bound")
        stream="events" if source_id=="v9-local-intake-events" else "requests"
        expected_name="expected_events" if stream=="events" else "expected_requests"
        emitted_name="emitted_events" if stream=="events" else "emitted_requests"
        count_name="page_events" if stream=="events" else "page_requests"
        numbers={name:page.get(name) for name in ("start_after_sequence","watermark_sequence",
                 "last_sequence",expected_name,emitted_name,count_name)}
        if any(type(v) is not int or v<0 for v in numbers.values()):raise ValueError("invalid_import_page_count")
        if page[count_name]!=len(records):raise ValueError("import_page_count_mismatch")
        if not (page["start_after_sequence"]<=page["last_sequence"]<=page["watermark_sequence"]):
            raise ValueError("invalid_import_page_range")
        if page[emitted_name]>page[expected_name]:raise CatalogConflict("import_count_exceeds_watermark")
        complete=page.get("complete")
        if type(complete) is not bool:raise ValueError("invalid_import_complete")
        if complete and (page.get("count_matches") is not True or page[emitted_name]!=page[expected_name]):
            raise CatalogConflict("completed_import_count_mismatch")
        expected_hash=hashlib.sha256(json.dumps(records,ensure_ascii=False,sort_keys=True,
                                                separators=(",",":"),allow_nan=False).encode()).hexdigest()
        if page.get("records_sha256")!=expected_hash:raise CatalogConflict("import_page_hash_mismatch")
        with self._tx() as db:
            old=db.execute("SELECT source_instance,watermark_sequence,last_sequence,expected_count,emitted_count,completed,last_page_sha256 FROM import_cursors WHERE source_id=? AND project_id=? AND stream=?",
                           (source_id,project_id,stream)).fetchone()
            if old:
                if old["source_instance"]!=source_instance:raise CatalogConflict("source_instance_changed")
                if old["last_sequence"]==page["last_sequence"] and old["last_page_sha256"]==expected_hash:
                    return False
                if old["last_sequence"]>=page["last_sequence"]:
                    raise CatalogConflict("stale_import_page")
                if old["completed"]:
                    if page["start_after_sequence"]!=old["last_sequence"] or page["watermark_sequence"]<old["last_sequence"]:
                        raise CatalogConflict("import_resume_boundary_mismatch")
                    if page[emitted_name]!=len(records):raise CatalogConflict("import_new_run_count_mismatch")
                else:
                    if (page["watermark_sequence"]!=old["watermark_sequence"] or
                        page[expected_name]!=old["expected_count"] or
                        page[emitted_name]!=old["emitted_count"]+len(records)):
                        raise CatalogConflict("import_cursor_progress_mismatch")
            elif page[emitted_name]!=len(records):
                raise CatalogConflict("import_initial_count_mismatch")
            preceding=old["last_sequence"] if old else page["start_after_sequence"]
            for item in records:
                try:
                    sequence=int(item["source_key"])
                except (KeyError,TypeError,ValueError) as exc:
                    raise CatalogConflict("invalid_import_record_sequence") from exc
                if str(sequence)!=item["source_key"] or not preceding<sequence<=page["watermark_sequence"]:
                    raise CatalogConflict("import_record_sequence_out_of_order")
                preceding=sequence
            if preceding!=page["last_sequence"]:
                raise CatalogConflict("import_last_sequence_mismatch")
            for item in records:
                if not isinstance(item,dict) or item.get("source_id")!=source_id or item.get("project_id")!=project_id:
                    raise CatalogConflict("import_record_source_mismatch")
                apply_record(db,item)
            db.execute("""INSERT INTO import_cursors VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id,project_id,stream) DO UPDATE SET
                source_instance=excluded.source_instance,
                watermark_sequence=excluded.watermark_sequence,
                last_sequence=excluded.last_sequence,
                expected_count=excluded.expected_count,
                emitted_count=excluded.emitted_count,
                completed=excluded.completed,
                last_page_sha256=excluded.last_page_sha256,
                updated_at_ns=excluded.updated_at_ns""",
                (source_id,project_id,stream,source_instance,page["watermark_sequence"],
                 page["last_sequence"],page[expected_name],page[emitted_name],int(complete),expected_hash,_now()))
            return True

    def import_legacy_cloud_event(self, event: dict[str, Any], metrics: list[dict[str, Any]],
                                  *, source_id: str = "v9-cloud-relay") -> bool:
        """Preserve a V9 proxy ledger row without inventing a complete request."""
        if not isinstance(event,dict) or not isinstance(metrics,list):raise ValueError("invalid_legacy_cloud_event")
        for name in ("event_id","occurred_at","day_cst","caller","provider","method","path","outcome","usage_status"):
            _required(event.get(name),name,4096 if name=="path" else 256)
        _required(source_id,"source_id")
        model=event.get("model")
        if model is not None:_required(model,"model")
        for name in ("status_code","input_tokens","output_tokens","total_tokens"):
            value=event.get(name)
            if value is not None and (type(value) is not int or value<0):raise ValueError("invalid_legacy_"+name)
        for name in ("upstream_request_id","client_request_id"):
            if event.get(name) is not None:_required(event[name],name)
        path_sha=hashlib.sha256(event["path"].encode()).hexdigest()
        fact=(source_id,event["occurred_at"],event["day_cst"],event["caller"],event["provider"],model,
              event["method"],path_sha,event.get("status_code"),event["outcome"],event["usage_status"],
              event.get("input_tokens"),event.get("output_tokens"),event.get("total_tokens"),
              event.get("upstream_request_id"),event.get("client_request_id"),
              "v9_full_input_result_config_not_stored")
        parsed_metrics=[]
        seen=set()
        for item in metrics:
            if not isinstance(item,dict):raise ValueError("invalid_legacy_metric")
            name=_required(item.get("metric_name"),"metric_name")
            unit=_required(item.get("unit"),"unit")
            value=_required(item.get("quantity_decimal"),"quantity_decimal")
            origin=_required(item.get("source"),"metric_source")
            try:
                decimal=Decimal(value)
                if not decimal.is_finite() or decimal<0:raise ValueError()
            except (InvalidOperation,ValueError) as exc:raise ValueError("invalid_legacy_metric_quantity") from exc
            if (name,unit) in seen:raise CatalogConflict("duplicate_legacy_metric")
            seen.add((name,unit))
            parsed_metrics.append((name,unit,value,origin))
        with self._tx() as db:
            old=db.execute("""SELECT source_id,occurred_at,day_cst,caller,provider,model,method,
                path_sha256,status_code,outcome,usage_status,input_tokens,output_tokens,total_tokens,
                upstream_request_id,client_request_id,unknown_reason
                FROM legacy_cloud_events WHERE event_id=?""",(event["event_id"],)).fetchone()
            if old:
                if tuple(old)!=fact:raise CatalogConflict("legacy_cloud_event_conflict")
                prior=[tuple(row) for row in db.execute("SELECT metric_name,unit,quantity_decimal,source FROM legacy_cloud_metrics WHERE event_id=? ORDER BY metric_name,unit",(event["event_id"],))]
                if prior!=sorted(parsed_metrics):raise CatalogConflict("legacy_cloud_metric_conflict")
                return False
            db.execute("INSERT INTO legacy_cloud_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (event["event_id"],*fact,_now()))
            for name,unit,value,origin in parsed_metrics:
                db.execute("INSERT INTO legacy_cloud_metrics VALUES (?,?,?,?,?)",
                           (event["event_id"],name,unit,value,origin))
            return True

    def get_chain(self, project_id: str, request_id: str, *, caller_id: str | None = None) -> dict[str, Any] | None:
        with self._mutex:
            return self._get_chain(project_id,request_id,caller_id=caller_id)

    def _get_chain(self, project_id: str, request_id: str, *, caller_id: str | None = None) -> dict[str, Any] | None:
        self._check_reader(project_id,caller_id)
        row=self.db.execute("""SELECT r.*,b.object_id,b.manifest_sha256,c.revision AS config_revision,
            b.external_batch_id,c.software_version,c.sha256 AS config_sha256,c.knowledge AS config_knowledge,
            c.unknown_reason AS config_unknown_reason,c.snapshot_artifact_id AS config_snapshot_artifact_id,
            c.source_id AS config_source_id FROM requests r
            JOIN batches b ON (r.project_id=b.project_id AND r.batch_id=b.batch_id)
            JOIN config_revisions c ON (r.project_id=c.project_id AND r.config_id=c.config_id)
            WHERE r.project_id=? AND r.request_id=?""",(project_id,request_id)).fetchone()
        history_row=self.db.execute("SELECT * FROM local_history_records WHERE project_id=? AND request_id=?",
                                    (project_id,request_id)).fetchone()
        history=None
        if history_row:
            history=dict(history_row)
            for key in ("native_usage_json","source_references_json","unknown_json"):
                history[key.removesuffix("_json")]=json.loads(history.pop(key)) if history[key] is not None else None
        source_events=[]
        for event in self.db.execute("SELECT * FROM local_history_events WHERE project_id=? AND request_id=? ORDER BY event_sequence",
                                     (project_id,request_id)):
            item=dict(event)
            item["unknown"]=json.loads(item.pop("unknown_json"))
            item["source_id"]="v9-local-intake-events"
            item["attempt"]=None
            item["attempt_relation"]="not_proven_by_v9_source"
            source_events.append(item)
        if row is None and history is None and not source_events:return None
        if row is None:
            first_event=source_events[0] if source_events else None
            source_sequence=history["source_sequence"] if history else first_event["request_source_sequence"]
            request={"project_id":project_id,"request_id":request_id,"source_id":"v9-local-intake",
                     "source_key":str(source_sequence),"catalog_presence":False,
                     "state":history["original_state"] if history else None,
                     "object_id":history["object_id"] if history else None,
                     "batch_id":history["batch_key"] if history else None,
                     "external_batch_id":history["external_batch_id"] if history else None,
                     "model":history["model"] if history else None,
                     "request_fingerprint":history["request_fingerprint"] if history else None,
                     "config_sha256":history["config_snapshot_sha256"] if history else None,
                     "config_unknown_reason":history["config_unknown_reason"] if history else None}
            attempts=[];transitions=[];usage=[];gaps=[];links=[];artifacts=[];source_refs=[]
        else:
            request=dict(row)
            attempts=[dict(x) for x in self.db.execute("SELECT * FROM attempts WHERE project_id=? AND request_id=? ORDER BY attempt",(project_id,request_id))]
            transitions=[dict(x) for x in self.db.execute("SELECT * FROM transitions WHERE project_id=? AND request_id=? ORDER BY recorded_at_ns,event_key",(project_id,request_id))]
            usage=[dict(x) for x in self.db.execute("SELECT * FROM usage_facts WHERE project_id=? AND request_id=? ORDER BY recorded_at_ns,source_event_key",(project_id,request_id))]
            gaps=[];source_refs=[]
            links=[dict(x) for x in self.db.execute("SELECT * FROM artifact_links WHERE project_id=? AND request_id=? ORDER BY role,artifact_id",(project_id,request_id))]
            refs={row["input_artifact_id"],row["config_snapshot_artifact_id"]} | {a[k] for a in attempts for k in ("receipt_artifact_id","stop_proof_artifact_id","result_artifact_id","partial_artifact_id")} | {x["artifact_id"] for x in links}
            refs.discard(None)
            artifacts=[dict(x) for x in self.db.execute("SELECT * FROM artifacts WHERE project_id=? AND artifact_id IN (%s) ORDER BY artifact_id" % ",".join("?" for _ in refs),(project_id,*sorted(refs)))] if refs else []
        gaps=[dict(x) for x in self.db.execute("SELECT * FROM source_gaps WHERE project_id=? AND request_id=? ORDER BY source_id,gap_key",(project_id,request_id))]
        source_refs.extend(dict(x) for x in self.db.execute("SELECT * FROM source_references WHERE project_id=? AND request_id=? ORDER BY role,ref_id",(project_id,request_id)))
        local_refs=[];local_usage=[]
        capture_row=self.db.execute(
            "SELECT * FROM cloud_request_capture WHERE project_id=? AND request_id=?",
            (project_id,request_id)).fetchone()
        if history:
            local_refs=[dict(x) for x in self.db.execute("SELECT * FROM local_history_refs WHERE project_id=? AND source_sequence=? ORDER BY role,ref_id",(project_id,history["source_sequence"]))]
            local_usage=[dict(x) for x in self.db.execute("SELECT * FROM local_history_usage WHERE project_id=? AND source_sequence=? ORDER BY metric_name,unit",(project_id,history["source_sequence"]))]
            for ref in local_refs:
                ref["source_id"]="v9-local-intake"
                ref["request_id"]=request_id
            for fact in local_usage:
                fact["source_id"]="v9-local-intake"
                fact["request_id"]=request_id
                fact["attempt"]=None
        return {"request":request,"attempts":attempts,"transitions":transitions,"artifacts":artifacts,
                "artifact_links":links,"source_references":source_refs,"usage":usage,"gaps":gaps,
                "local_history":history,"local_source_references":local_refs,
                "local_usage":local_usage,"source_events":source_events,
                "cloud_request_capture":dict(capture_row) if capture_row else None}

    def get_chain_by_execution(self, project_id: str, execution_id: str, *, caller_id: str | None = None) -> dict[str, Any] | None:
        with self._mutex:
            self._check_reader(project_id,caller_id)
            row=self.db.execute("SELECT request_id FROM attempts WHERE project_id=? AND execution_id=?",(project_id,execution_id)).fetchone()
            historical=self.db.execute("SELECT request_id FROM local_history_records WHERE project_id=? AND execution_id=? LIMIT 2",
                                       (project_id,execution_id)).fetchall()
            if len(historical)>1 or (row and historical and row[0]!=historical[0][0]):
                raise CatalogConflict("historical_execution_identity_ambiguous")
            row=row or (historical[0] if historical else None)
            return self._get_chain(project_id,row[0],caller_id=caller_id) if row else None

    def _check_reader(self, project_id: str, caller_id: str | None) -> None:
        _required(project_id,"project_id")
        if caller_id is not None:
            row=self.db.execute("SELECT active FROM callers WHERE project_id=? AND caller_id=?",(project_id,caller_id)).fetchone()
            if row is None or not row[0]:raise CatalogConflict("caller_not_authorized_for_project")

    def list_chains(self, project_id: str, *, caller_id: str | None = None,
                    model: str | None = None, batch_id: str | None = None,
                    object_id: str | None = None, execution_id: str | None = None,
                    from_ns: int | None = None, to_ns: int | None = None,
                    after: tuple[int,str] | None = None,
                    limit: int = 100) -> dict[str, Any]:
        with self._mutex:
            return self._list_chains(project_id,caller_id=caller_id,model=model,batch_id=batch_id,
                                     object_id=object_id,execution_id=execution_id,from_ns=from_ns,
                                     to_ns=to_ns,after=after,limit=limit)

    def _list_chains(self, project_id: str, *, caller_id: str | None = None,
                     model: str | None = None, batch_id: str | None = None,
                     object_id: str | None = None, execution_id: str | None = None,
                     from_ns: int | None = None, to_ns: int | None = None,
                     after: tuple[int,str] | None = None,
                     limit: int = 100) -> dict[str, Any]:
        self._check_reader(project_id,caller_id)
        if type(limit) is not int or limit<1 or limit>500:raise ValueError("invalid_limit")
        conditions=["r.project_id=?"]
        args:[Any]=[project_id]
        if model is not None:conditions.append("r.model=?");args.append(model)
        if batch_id is not None:conditions.append("r.batch_id=?");args.append(batch_id)
        if object_id is not None:conditions.append("b.object_id=?");args.append(object_id)
        if execution_id is not None:
            conditions.append("(EXISTS(SELECT 1 FROM attempts a WHERE a.project_id=r.project_id AND a.request_id=r.request_id AND a.execution_id=?) OR EXISTS(SELECT 1 FROM local_history_records h WHERE h.project_id=r.project_id AND h.request_id=r.request_id AND h.execution_id=?))")
            args.extend((execution_id,execution_id))
        if from_ns is not None:conditions.append("r.created_at_ns>=?");args.append(from_ns)
        if to_ns is not None:conditions.append("r.created_at_ns<=?");args.append(to_ns)
        if after is not None:
            conditions.append("(r.created_at_ns,r.request_id)>(?,?)")
            args.extend(after)
        rows=[dict(x) for x in self.db.execute("""SELECT r.project_id,r.request_id,r.batch_id,b.object_id,
            b.external_batch_id,r.model,r.state,r.created_at_ns,r.updated_at_ns FROM requests r JOIN batches b
            ON (r.project_id=b.project_id AND r.batch_id=b.batch_id) WHERE """+" AND ".join(conditions)+
            " ORDER BY r.created_at_ns,r.request_id LIMIT ?",(*args,limit+1))]
        local_conditions=["h.project_id=?","h.core_request_registered=0"]
        local_args:[Any]=[project_id]
        for column,value in (("model",model),("batch_key",batch_id),("object_id",object_id),
                             ("execution_id",execution_id)):
            if value is not None:
                local_conditions.append(f"h.{column}=?")
                local_args.append(value)
        if from_ns is not None:local_conditions.append("h.imported_at_ns>=?");local_args.append(from_ns)
        if to_ns is not None:local_conditions.append("h.imported_at_ns<=?");local_args.append(to_ns)
        if after is not None:
            local_conditions.append("(h.imported_at_ns,h.request_id)>(?,?)")
            local_args.extend(after)
        rows.extend(dict(x) for x in self.db.execute("""SELECT h.project_id,h.request_id,
            h.batch_key AS batch_id,h.object_id,h.external_batch_id,h.model,
            h.original_state AS state,h.imported_at_ns AS created_at_ns,
            h.imported_at_ns AS updated_at_ns FROM local_history_records h WHERE """+
            " AND ".join(local_conditions)+" ORDER BY h.imported_at_ns,h.request_id LIMIT ?",
            (*local_args,limit+1)))
        rows.sort(key=lambda item:(item["created_at_ns"],item["request_id"]))
        page=rows[:limit]
        next_cursor=(page[-1]["created_at_ns"],page[-1]["request_id"]) if len(rows)>limit else None
        return {"items":page,"next_cursor":next_cursor}
