"""Map bounded V9 local exporter facts inside Catalog.commit_import_page's transaction.

The request page is an observation of intake and native references. The event
page is a separate state-event source. Neither proves an event's attempt number.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import quote

from .core import CatalogConflict, _now


REQUEST_SOURCE = "v9-local-intake"
EVENT_SOURCE = "v9-local-intake-events"
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _sha(value: str | None, name: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not _HEX.fullmatch(value)):
        raise ValueError("invalid_" + name)
    return value


def _string(value: Any, name: str, *, required: bool = False, limit: int = 4096) -> str | None:
    if value is None and not required:
        return None
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(ord(c) < 32 for c in value)):
        raise ValueError("invalid_" + name)
    return value


def _number(value: Any, name: str, *, required: bool = False) -> int | None:
    if value is None and not required:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("invalid_" + name)
    return value


def _source_sequence(value: Any, name: str) -> int:
    if not isinstance(value, str) or not value.isdecimal() or str(int(value)) != value:
        raise ValueError("invalid_" + name)
    return int(value)


def _private_uri(value: Any) -> str:
    value = _string(value, "source_uri", required=True)
    if value.lower().startswith(("https://", "http://")) or "?" in value:
        raise ValueError("source_uri_not_private")
    return value


def _source_path_uri(value: Any) -> str:
    raw = _string(value, "source_path", required=True)
    path = Path(raw)
    return _private_uri(path.as_uri() if path.is_absolute() else "unresolved:" + quote(raw, safe=""))


def _unknown_list(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError("invalid_history_unknown")
    return value


def _gap(db: sqlite3.Connection, source: str, project: str, request_id: str,
         key: str, reason: str) -> None:
    previous = db.execute("SELECT project_id,request_id,reason FROM source_gaps WHERE source_id=? AND gap_key=?",
                          (source,key)).fetchone()
    if previous:
        if tuple(previous) != (project,request_id,reason):
            raise CatalogConflict("history_gap_conflict")
        return
    db.execute("INSERT INTO source_gaps VALUES (?,?,?,?,?,?,NULL)",
               (source,key,project,request_id,reason,_now()))


class LocalHistoryMapper:
    """Callable mapper; pass the instance to Catalog.commit_import_page(page, mapper).

    Register both V9 local source IDs and the project first. This mapper never
    reads V9 files or publishes any artifact; even verified media remains an
    original-source reference pending separate ownership and restore checks.
    """

    def __call__(self, db: sqlite3.Connection, record: dict[str, Any]) -> None:
        source = record.get("source_id")
        if source == REQUEST_SOURCE:
            self._request(db, record)
        elif source == EVENT_SOURCE:
            self._event(db, record, evidence="events_page")
        else:
            raise ValueError("unsupported_local_history_source")

    def _request(self, db: sqlite3.Connection, record: dict[str, Any]) -> None:
        project = _string(record.get("project_id"), "project_id", required=True)
        request_id = _string(record.get("request_id"), "request_id", required=True)
        sequence = _source_sequence(record.get("source_key"), "request_source_key")
        unknown = _unknown_list(record.get("unknown"))
        digest = hashlib.sha256(_json(record).encode()).hexdigest()
        previous = db.execute("SELECT record_sha256 FROM local_history_records WHERE project_id=? AND source_sequence=?",
                              (project,sequence)).fetchone()
        if previous:
            if previous[0] != digest:
                raise CatalogConflict("local_history_record_conflict")
            return
        occupied=db.execute("SELECT source_sequence FROM local_history_records WHERE project_id=? AND request_id=?",
                            (project,request_id)).fetchone()
        if occupied:
            raise CatalogConflict("local_history_request_conflict")
        fields = {name: _string(record.get(name), name) for name in
                  ("object_id","batch_id","model","binding_revision","request_fingerprint_source",
                   "execution_id","state","delivery","observed_receipt_state","config_unknown_reason")}
        batch_key = record.get("batch_key")
        if batch_key is not None:
            batch_key = _string(batch_key, "batch_key", required=True, limit=128)
        manifest = _sha(record.get("manifest_sha256"),"manifest_sha256")
        fingerprint = _sha(record.get("request_fingerprint"),"request_fingerprint")
        config_hash = _sha(record.get("config_snapshot_sha256"),"config_snapshot_sha256")
        attempt = _number(record.get("attempt"),"attempt")
        if attempt == 0:raise ValueError("invalid_attempt")
        batch_created = _number(record.get("batch_created_ns"),"batch_created_ns")
        native_usage = record.get("native_usage")
        if native_usage is not None and not isinstance(native_usage,dict):
            raise ValueError("invalid_native_usage")
        source_paths = record.get("source_references")
        if source_paths is not None and not isinstance(source_paths,dict):
            raise ValueError("invalid_source_references")
        references = record.get("artifacts",[])
        if not isinstance(references,list):raise ValueError("invalid_history_artifacts")
        eligible = all((fields["object_id"],batch_key,fields["batch_id"],manifest,
                        fingerprint,fields["model"],fields["state"]))
        config_id = None
        stamp = _now()
        if eligible:
            config_id = self._core_request(db,project,request_id,sequence,fields,batch_key,
                                           manifest,fingerprint,stamp)
        db.execute("""INSERT INTO local_history_records VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project,sequence,request_id,digest,fields["object_id"],batch_key,fields["batch_id"],
             manifest,batch_created,fingerprint,fields["request_fingerprint_source"],fields["model"],
             fields["binding_revision"],config_hash,fields["config_unknown_reason"],config_id,
             fields["execution_id"],attempt,fields["state"],fields["delivery"],
             fields["observed_receipt_state"],_json(native_usage) if native_usage is not None else None,
             _json(source_paths) if source_paths is not None else None,_json(unknown),int(eligible),stamp))
        for index, reason in enumerate(unknown):
            _gap(db,REQUEST_SOURCE,project,request_id,f"request:{sequence}:{index}",reason)
        if not eligible:
            _gap(db,REQUEST_SOURCE,project,request_id,f"request:{sequence}:incomplete",
                 "source_record_lacks_core_identity")
        self._references(db,project,sequence,references,source_paths or {})
        if native_usage is not None:
            self._usage(db,project,sequence,native_usage)
        transitions = record.get("transitions",[])
        if not isinstance(transitions,list):raise ValueError("invalid_request_transitions")
        for item in transitions:
            if not isinstance(item,dict):raise ValueError("invalid_request_transition")
            self._event(db,{"source_id":EVENT_SOURCE,"source_key":str(item.get("source_sequence")),
                            "project_id":project,"request_id":request_id,
                            "request_source_key":str(sequence),**item},evidence="request_preview")

    @staticmethod
    def _core_request(db: sqlite3.Connection, project: str, request_id: str, sequence: int,
                      fields: dict[str,str | None], batch_key: str, manifest: str,
                      fingerprint: str, stamp: int) -> str:
        object_id=fields["object_id"]
        model=fields["model"]
        revision=fields["binding_revision"] or "unknown"
        config_id="v9-local-"+hashlib.sha256(_json([project,model,revision]).encode()).hexdigest()
        db.execute("INSERT INTO objects VALUES (?,?,?,?) ON CONFLICT(project_id,object_id) DO NOTHING",
                   (project,object_id,REQUEST_SOURCE,stamp))
        obj=db.execute("SELECT source_id FROM objects WHERE project_id=? AND object_id=?",
                       (project,object_id)).fetchone()
        if obj[0]!=REQUEST_SOURCE:raise CatalogConflict("local_object_source_conflict")
        db.execute("""INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id,batch_id) DO NOTHING""",
            (project,batch_key,fields["batch_id"],object_id,manifest,REQUEST_SOURCE,batch_key,stamp))
        batch=db.execute("SELECT external_batch_id,object_id,manifest_sha256,source_id,source_key FROM batches WHERE project_id=? AND batch_id=?",
                         (project,batch_key)).fetchone()
        if tuple(batch)!=(fields["batch_id"],object_id,manifest,REQUEST_SOURCE,batch_key):
            raise CatalogConflict("local_batch_identity_conflict")
        db.execute("""INSERT INTO config_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id,config_id) DO NOTHING""",
            (project,config_id,model,"V9 historical; exact build unknown",revision,None,"unknown",
             fields["config_unknown_reason"] or "historical_effective_config_unverified",
             None,REQUEST_SOURCE,stamp))
        config=db.execute("SELECT model,software_version,revision,sha256,knowledge,unknown_reason,source_id FROM config_revisions WHERE project_id=? AND config_id=?",
                          (project,config_id)).fetchone()
        if tuple(config)!=(model,"V9 historical; exact build unknown",revision,None,"unknown",
                           fields["config_unknown_reason"] or "historical_effective_config_unverified",
                           REQUEST_SOURCE):
            raise CatalogConflict("local_config_identity_conflict")
        core=db.execute("SELECT source_id,source_key FROM requests WHERE project_id=? AND request_id=?",
                        (project,request_id)).fetchone()
        if core:raise CatalogConflict("local_request_identity_conflict")
        db.execute("""INSERT INTO requests
            (project_id,request_id,batch_id,caller_id,model,config_id,input_artifact_id,
             request_fingerprint,source_id,source_key,state,revision,created_at_ns,updated_at_ns)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project,request_id,batch_key,None,model,config_id,None,fingerprint,
             REQUEST_SOURCE,str(sequence),fields["state"],0,stamp,stamp))
        return config_id

    @staticmethod
    def _references(db: sqlite3.Connection, project: str, sequence: int,
                    artifacts: list[Any], source_paths: dict[str,Any]) -> None:
        def insert(ref_id: str,role: str,uri: str,sha: str | None,size: int | None,
                   state: str,unknown: str | None,declared_sha: str | None=None,
                   declared_bytes: int | None=None) -> None:
            db.execute("INSERT INTO local_history_refs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (project,sequence,ref_id,role,uri,sha,size,declared_sha,
                        declared_bytes,state,unknown))
        seen=set()
        for item in artifacts:
            if not isinstance(item,dict):raise ValueError("invalid_history_artifact")
            ref_id=_string(item.get("artifact_id"),"artifact_id",required=True)
            if ref_id in seen:raise CatalogConflict("duplicate_local_source_reference")
            seen.add(ref_id)
            role=_string(item.get("role"),"artifact_role",required=True)
            uri=_private_uri(item.get("uri"))
            sha=_sha(item.get("sha256"),"artifact_sha256")
            size=_number(item.get("bytes"),"artifact_bytes")
            state=_string(item.get("recovery_state"),"recovery_state",required=True)
            declared_sha=_sha(item.get("declared_sha256"),"declared_sha256")
            declared_bytes=_number(item.get("declared_bytes"),"declared_bytes")
            insert(ref_id,role,uri,sha,size,state,state if sha is None else None,
                   declared_sha,declared_bytes)
        for role,path in source_paths.items():
            role=_string(role,"source_path_role",required=True)
            insert("source:"+role,"source_"+role,_source_path_uri(path),None,None,
                   "source_unverified","source_file_not_hashed")

    @staticmethod
    def _usage(db: sqlite3.Connection, project: str, sequence: int, usage: dict[str,Any]) -> None:
        status=_string(usage.get("status"),"usage_status",required=True)
        unit=_string(usage.get("unit"),"usage_unit",required=True)
        provenance=_string(usage.get("source"),"usage_source")
        reason=_string(usage.get("unknown_reason"),"usage_unknown_reason")
        values=usage.get("values")
        unknown=usage.get("unknown_fields")
        if not isinstance(values,dict) or not isinstance(unknown,list):
            raise ValueError("invalid_native_usage")
        seen=set()
        for name,value in values.items():
            name=_string(name,"usage_metric",required=True)
            if type(value) is not int or value<0:raise ValueError("invalid_native_usage_quantity")
            seen.add(name)
            db.execute("INSERT INTO local_history_usage VALUES (?,?,?,?,?,?,?,?)",
                       (project,sequence,name,unit,str(value),"known",None,provenance))
        for name in unknown:
            name=_string(name,"unknown_usage_metric",required=True)
            if name in seen:raise CatalogConflict("conflicting_native_usage_knowledge")
            seen.add(name)
            db.execute("INSERT INTO local_history_usage VALUES (?,?,?,?,?,?,?,?)",
                       (project,sequence,name,unit,None,"unknown",reason or status,provenance))

    @staticmethod
    def _event(db: sqlite3.Connection, record: dict[str,Any], *, evidence: str) -> None:
        project=_string(record.get("project_id"),"project_id",required=True)
        request_id=_string(record.get("request_id"),"request_id",required=True)
        sequence=_source_sequence(record.get("source_key"),"event_source_key")
        request_sequence=_source_sequence(record.get("request_source_key"),"request_source_key")
        event_key=_string(record.get("event_key"),"event_key",required=True)
        state=_string(record.get("state"),"event_state",required=True)
        occurred=_number(record.get("at_ns"),"event_at_ns")
        reason_sha=_sha(record.get("reason_sha256"),"event_reason_sha256")
        reason_bytes=_number(record.get("reason_bytes"),"event_reason_bytes")
        reason_uri=_private_uri(record.get("reason_uri")) if record.get("reason_uri") is not None else None
        unknown=_unknown_list(record.get("unknown",[]))
        old=db.execute("SELECT request_id,request_source_sequence,event_key,state,occurred_at_ns,reason_sha256,reason_bytes,reason_uri,evidence_kind,unknown_json FROM local_history_events WHERE project_id=? AND event_sequence=?",
                       (project,sequence)).fetchone()
        if old:
            if tuple(old)[:5]!=(request_id,request_sequence,event_key,state,occurred):
                raise CatalogConflict("local_event_identity_conflict")
            if old[5] and reason_sha and old[5]!=reason_sha:
                raise CatalogConflict("local_event_reason_conflict")
            if old[6] is not None and reason_bytes is not None and old[6]!=reason_bytes:
                raise CatalogConflict("local_event_reason_size_conflict")
            if old[7] is not None and reason_uri is not None and old[7]!=reason_uri:
                raise CatalogConflict("local_event_reason_uri_conflict")
            merged_sha=old[5] or reason_sha
            merged_bytes=old[6] if old[6] is not None else reason_bytes
            merged_uri=old[7] or reason_uri
            merged_kind="events_page" if evidence=="events_page" or old[8]=="events_page" else "request_preview"
            merged_unknown=list(dict.fromkeys(json.loads(old[9])+unknown))
            db.execute("""UPDATE local_history_events SET reason_sha256=?,reason_bytes=?,
                reason_uri=?,evidence_kind=?,unknown_json=? WHERE project_id=? AND event_sequence=?""",
                (merged_sha,merged_bytes,merged_uri,merged_kind,_json(merged_unknown),project,sequence))
        else:
            db.execute("INSERT INTO local_history_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (project,sequence,request_id,request_sequence,event_key,state,occurred,
                        reason_sha,reason_bytes,reason_uri,evidence,_json(unknown),_now()))
        for index,reason in enumerate(unknown):
            _gap(db,EVENT_SOURCE,project,request_id,f"event:{sequence}:{index}",reason)
