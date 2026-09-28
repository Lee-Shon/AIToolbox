"""Cloud capture Protocol implementation for the one-writer catalog service.

The adapter accepts immutable private file references from the candidate relay.
It never invokes a provider or retries an uncertain execution.
"""
from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .core import Catalog, CatalogConflict, ManagedArtifacts, _digest, _now, _required


SOURCE_ID = "v10-cloud-capture"
_PRIVATE_HEADER = re.compile(r"auth|cookie|token|secret|key|credential|password|session", re.I)


def _verify_chunked_body(path: str, digest: str, size: int) -> None:
    observed=hashlib.sha256()
    count=0
    with open(path,"rb") as source:
        while True:
            line=source.readline(128)
            if not line.endswith(b"\r\n") or len(line)>=128:
                raise CatalogConflict("invalid_request_wire_chunk_header")
            try:
                chunk_size=int(line[:-2].split(b";",1)[0].strip(),16)
            except ValueError as exc:
                raise CatalogConflict("invalid_request_wire_chunk_size") from exc
            if chunk_size<0 or count+chunk_size>size:
                raise CatalogConflict("request_wire_body_size_mismatch")
            if chunk_size==0:
                break
            remaining=chunk_size
            while remaining:
                chunk=source.read(min(1024*1024,remaining))
                if not chunk:
                    raise CatalogConflict("incomplete_request_wire")
                observed.update(chunk)
                count+=len(chunk)
                remaining-=len(chunk)
            if source.read(2)!=b"\r\n":
                raise CatalogConflict("invalid_request_wire_chunk_ending")
        trailer_bytes=0
        trailer_line_bytes=0
        preceding=None
        while True:
            trailer=source.readline(8192)
            trailer_bytes+=len(trailer)
            if not trailer or trailer_bytes>8*1024*1024:
                raise CatalogConflict("invalid_request_wire_trailer")
            trailer_line_bytes+=len(trailer)
            if trailer.endswith(b"\n"):
                before_lf=trailer[-2] if len(trailer)>1 else preceding
                if before_lf!=13:
                    raise CatalogConflict("invalid_request_wire_trailer")
                if trailer_line_bytes==2:
                    break
                trailer_line_bytes=0
            preceding=trailer[-1]
        if source.read(1):
            raise CatalogConflict("request_wire_has_extra_bytes")
    if count!=size or observed.hexdigest()!=digest:
        raise CatalogConflict("request_wire_body_hash_mismatch")


def cloud_config_id(provider: str, revision: str) -> str:
    return "cloud-" + hashlib.sha256(f"{provider}\0{revision}".encode()).hexdigest()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


class CloudCatalogAdapter:
    """Implements cloud_data_capture's Catalog Protocol without importing its code."""

    def __init__(self, catalog: Catalog, *, file_root: str | Path, source_id: str = SOURCE_ID):
        self.catalog = catalog
        self.file_root = Path(file_root).resolve()
        self.source_id = _required(source_id, "source_id")

    def _ref(self, project: str, ref: Any) -> tuple[str, str, int, str]:
        if ref.project != project:
            raise CatalogConflict("artifact_project_boundary")
        digest = _digest(ref.sha256, "sha256")
        if type(ref.size) is not int or ref.size < 0:
            raise ValueError("invalid_artifact_size")
        path = Path(ref.path).resolve()
        expected = self.file_root / "objects" / project / digest[:2] / digest
        if path != expected:
            raise CatalogConflict("artifact_path_outside_managed_store")
        if path.stat().st_size != ref.size:
            raise CatalogConflict("artifact_size_mismatch")
        return digest, str(path), ref.size, ManagedArtifacts.hash_file(path)

    @staticmethod
    def _insert_artifact(db, project: str, role: str, digest: str, uri: str, size: int,
                         source_id: str) -> str:
        artifact_id=f"cloud-{role}:{digest}"
        existing=db.execute("SELECT uri,sha256,bytes,role FROM artifacts WHERE project_id=? AND artifact_id=?",
                            (project,artifact_id)).fetchone()
        if existing:
            if tuple(existing)!=(uri,digest,size,role):
                raise CatalogConflict("artifact_identity_conflict")
        else:
            db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (project,artifact_id,role,"application/octet-stream",uri,digest,size,source_id,"managed",_now()))
        return artifact_id

    def admit_request(self, *, caller: Any, request_id: str, provider: str,
                      method: str, path: str, model: str | None, input_ref: Any,
                      config_revision: str, request_snapshot_ref: Any,
                      request_wire_ref: Any | None) -> str:
        project=_required(caller.project_id,"project_id")
        caller_id=_required(caller.caller_id,"caller_id")
        for n,v in (("request_id",request_id),("provider",provider),("method",method),("path",path),("config_revision",config_revision)):
            _required(v,n,65536 if n=="path" else 256)
        config_id=cloud_config_id(provider,config_revision)
        with self.catalog._mutex:
            person=self.catalog.db.execute("SELECT active FROM callers WHERE caller_id=? AND project_id=?",
                                           (caller_id,project)).fetchone()
            if person is None or not person[0]:
                raise CatalogConflict("caller_not_authorized_for_project")
            config=self.catalog.db.execute("SELECT knowledge FROM config_revisions WHERE project_id=? AND config_id=?",
                                           (project,config_id)).fetchone()
            if config is None or config[0]!="known":
                raise CatalogConflict("cloud_config_snapshot_not_registered")
        digest,uri,size,observed_hash=self._ref(project,input_ref)
        if observed_hash!=digest:raise CatalogConflict("input_artifact_hash_mismatch")
        snapshot_digest,snapshot_uri,snapshot_size,snapshot_observed=self._ref(project,request_snapshot_ref)
        if snapshot_observed!=snapshot_digest:raise CatalogConflict("snapshot_artifact_hash_mismatch")
        if snapshot_size>8*1024*1024:raise ValueError("request_snapshot_too_large")
        try:
            snapshot=json.loads(Path(snapshot_uri).read_text(encoding="utf-8"))
        except (UnicodeError,ValueError) as exc:
            raise ValueError("invalid_request_snapshot") from exc
        if not isinstance(snapshot,dict) or snapshot.get("schema")!="aitoolbox.v10.cloud-request-snapshot/1":
            raise ValueError("invalid_request_snapshot_schema")
        prefix=f"/p/{provider}/v1"
        expected_targets={prefix+path}
        if path=="/" or path.startswith("/?"):
            expected_targets.add(prefix+path[1:])
        expected={"project_id":project,"caller_id":caller_id,"request_id":request_id,
                  "provider_id":provider,"config_revision":config_revision,"method":method,
                  }
        if any(snapshot.get(name)!=value for name,value in expected.items()):
            raise CatalogConflict("request_snapshot_identity_mismatch")
        if snapshot.get("target") not in expected_targets:
            raise CatalogConflict("request_snapshot_target_mismatch")
        try:
            raw_line=base64.b64decode(snapshot["raw_request_line_base64"],validate=True)
            request_line=raw_line.decode("iso-8859-1")
        except (KeyError,TypeError,ValueError,binascii.Error,UnicodeError) as exc:
            raise ValueError("invalid_request_snapshot_line") from exc
        parts=request_line.removesuffix("\r\n").split(" ")
        if (len(raw_line)>65536 or not request_line.endswith("\r\n") or len(parts)!=3 or
                parts[:2]!=[method,snapshot["target"]] or not parts[2].startswith("HTTP/")):
            raise CatalogConflict("request_snapshot_line_mismatch")
        expected_body={"project":project,"sha256":digest,"size":size,"path":uri}
        if snapshot.get("body_ref")!=expected_body:
            raise CatalogConflict("request_snapshot_body_mismatch")
        headers=snapshot.get("headers")
        if not isinstance(headers,list) or any(
            not isinstance(item,dict) or item.get("index")!=index or
            not isinstance(item.get("name"),str) or not item["name"] or
            not isinstance(item.get("forwarded"),bool) or
            (item.get("value_state")=="known" and
             (set(item)!={"index","name","value_state","value","forwarded"} or
              not isinstance(item.get("value"),str) or _PRIVATE_HEADER.search(item["name"]))) or
            (item.get("value_state")=="unknown" and
             (set(item)!={"index","name","value_state","reason","forwarded"} or
              item.get("reason")!="credential_value_not_retained")) or
            item.get("value_state") not in {"known","unknown"}
            for index,item in enumerate(headers)):
            raise ValueError("unsafe_request_snapshot_headers")
        wire=None
        if request_wire_ref is not None:
            wire=self._ref(project,request_wire_ref)
            if wire[0]!=wire[3]:raise CatalogConflict("wire_artifact_hash_mismatch")
        framing=snapshot.get("framing")
        expected_wire=({"project":project,"sha256":wire[0],"size":wire[2],"path":wire[1]}
                       if wire is not None else None)
        expected_framing={"chunked"} if wire else {"content_length","none"}
        if not isinstance(framing,dict) or framing.get("wire_ref")!=expected_wire or framing.get("kind") not in expected_framing:
            raise CatalogConflict("request_snapshot_framing_mismatch")
        declared=framing.get("declared_content_length")
        if framing["kind"]=="content_length":
            try:
                if declared is None or int(declared)!=size:
                    raise ValueError()
            except (ValueError,TypeError) as exc:
                raise CatalogConflict("request_snapshot_length_mismatch") from exc
        elif declared is not None or (framing["kind"]=="none" and size!=0):
            raise CatalogConflict("request_snapshot_framing_mismatch")
        if wire is not None:
            _verify_chunked_body(wire[1],digest,size)
        route_sha=hashlib.sha256(path.encode()).hexdigest()
        fingerprint=_fingerprint((project,request_id,provider,method,route_sha,model,digest,
                                  snapshot_digest,wire[0] if wire else None,config_revision))
        batch_id=hashlib.sha256(f"cloud\0{project}\0{request_id}".encode()).hexdigest()
        object_id="cloud-"+request_id
        with self.catalog._tx() as db:
            person=db.execute("SELECT active FROM callers WHERE caller_id=? AND project_id=?",
                              (caller_id,project)).fetchone()
            if person is None or not person[0]:raise CatalogConflict("caller_not_authorized_for_project")
            config=db.execute("SELECT config_id,knowledge FROM config_revisions WHERE project_id=? AND config_id=?",
                              (project,config_id)).fetchone()
            if config is None or config["knowledge"]!="known":
                raise CatalogConflict("cloud_config_snapshot_not_registered")
            old=db.execute("SELECT caller_id,request_fingerprint FROM requests WHERE project_id=? AND request_id=?",
                           (project,request_id)).fetchone()
            if old:
                return "existing" if tuple(old)==(caller_id,fingerprint) else "conflict"
            artifact_id=self._insert_artifact(db,project,"input",digest,uri,size,self.source_id)
            snapshot_id=self._insert_artifact(db,project,"request_snapshot",snapshot_digest,
                                              snapshot_uri,snapshot_size,self.source_id)
            wire_id=(self._insert_artifact(db,project,"request_wire",wire[0],wire[1],wire[2],self.source_id)
                     if wire else None)
            stamp=_now()
            db.execute("INSERT INTO objects VALUES (?,?,?,?)",(project,object_id,self.source_id,stamp))
            db.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)",
                       (project,batch_id,request_id,object_id,fingerprint,self.source_id,
                        f"{project}:{request_id}:batch",stamp))
            db.execute("""INSERT INTO requests
                (project_id,request_id,batch_id,caller_id,model,config_id,input_artifact_id,
                 request_fingerprint,provider,method,route_sha256,source_id,source_key,
                 state,revision,created_at_ns,updated_at_ns)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (project,request_id,batch_id,caller_id,model or "unknown",config_id,artifact_id,
                        fingerprint,provider,method,route_sha,self.source_id,
                        f"{project}:{request_id}:request","ACCEPTED",0,stamp,stamp))
            db.execute("INSERT INTO cloud_request_capture VALUES (?,?,?,?,?)",
                       (project,request_id,snapshot_id,wire_id,stamp))
            for role,linked_id in (("request_snapshot",snapshot_id),("request_wire",wire_id)):
                if linked_id is not None:
                    db.execute("INSERT INTO artifact_links VALUES (?,?,?,?,?)",
                               (project,request_id,linked_id,role,stamp))
            if model is None:
                db.execute("INSERT INTO source_gaps VALUES (?,?,?,?,?,?,NULL)",
                           (self.source_id,f"{project}:{request_id}:model",project,request_id,
                            "model_not_present_in_request",stamp))
            return "accepted"

    def begin_attempt(self, *, project: str, request_id: str, attempt_id: str) -> None:
        self.catalog.advance_attempt(project_id=project,request_id=request_id,attempt=1,
                                     execution_id=attempt_id,event_key=f"cloud.begin:{attempt_id}",
                                     state="RUNNING",source_id=self.source_id)

    def _usage_rows(self, usage: dict | None, attempt_id: str) -> list[tuple[str,str,str|None,str,str|None,str]]:
        if usage is not None and not isinstance(usage,dict):raise ValueError("invalid_usage")
        if usage is not None and usage.get("source")!="provider":raise ValueError("usage_source_not_provider")
        values=[]
        for name in ("input_tokens","output_tokens","total_tokens"):
            quantity=None if usage is None else usage.get(name)
            if quantity is not None and (type(quantity) is not int or quantity<0):raise ValueError("invalid_usage_quantity")
            values.append((name,"tokens",str(quantity) if quantity is not None else None,
                           "known" if quantity is not None else "unknown",
                           None if quantity is not None else "provider_usage_missing","provider"))
        seen={(n,u) for n,u,*_ in values}
        for metric in (usage or {}).get("metrics",[]):
            if not isinstance(metric,dict):raise ValueError("invalid_usage_metric")
            name=_required(metric.get("name"),"metric_name")
            unit=_required(metric.get("unit"),"metric_unit")
            if (name,unit) in seen:raise CatalogConflict("duplicate_usage_metric")
            seen.add((name,unit))
            try:
                value=Decimal(metric.get("value"))
                if not value.is_finite() or value<0:raise ValueError()
            except (InvalidOperation,ValueError,TypeError) as exc:raise ValueError("invalid_usage_quantity") from exc
            values.append((name,unit,str(value),"known",None,"provider"))
        return values

    def finalize_attempt(self, *, project: str, request_id: str, attempt_id: str,
                         outcome: str, status_code: int, result_ref: Any,
                         upstream_request_id: str | None, usage: dict | None) -> None:
        if outcome not in {"completed","upstream_error"}:raise ValueError("invalid_cloud_outcome")
        if type(status_code) is not int or status_code<100 or status_code>599:raise ValueError("invalid_status_code")
        if (outcome=="completed")!=(status_code<400):raise ValueError("status_outcome_mismatch")
        project=_required(project,"project_id")
        _required(request_id,"request_id");_required(attempt_id,"attempt_id")
        if upstream_request_id is not None:_required(upstream_request_id,"upstream_request_id")
        digest,uri,size,observed_hash=self._ref(project,result_ref)
        if observed_hash!=digest:raise CatalogConflict("result_artifact_hash_mismatch")
        metrics=self._usage_rows(usage,attempt_id)
        state="COMPLETED" if outcome=="completed" else "FAILED"
        with self.catalog._tx() as db:
            attempt=db.execute("SELECT state,result_artifact_id,status_code,upstream_request_id,outcome FROM attempts WHERE project_id=? AND request_id=? AND execution_id=?",
                               (project,request_id,attempt_id)).fetchone()
            if attempt is None:raise CatalogConflict("cloud_attempt_not_started")
            artifact_id=f"cloud-result:{digest}"
            if attempt["state"] in {"COMPLETED","FAILED"}:
                if tuple(attempt)!=(state,artifact_id,status_code,upstream_request_id,outcome):
                    raise CatalogConflict("cloud_terminal_conflict")
                return
            if attempt["state"]!="RUNNING":raise CatalogConflict("cloud_attempt_not_running")
            artifact_id=self._insert_artifact(db,project,"result",digest,uri,size,self.source_id)
            stamp=_now()
            db.execute("""UPDATE attempts SET state=?,revision=revision+1,result_artifact_id=?,
                status_code=?,upstream_request_id=?,outcome=?,usage_status=?,updated_at_ns=?
                WHERE project_id=? AND request_id=? AND execution_id=?""",
                (state,artifact_id,status_code,upstream_request_id,outcome,
                 "provider" if usage is not None else "unknown",stamp,project,request_id,attempt_id))
            db.execute("UPDATE requests SET state=?,revision=revision+1,updated_at_ns=? WHERE project_id=? AND request_id=?",
                       (state,stamp,project,request_id))
            db.execute("INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?,?)",
                       (project,request_id,1,f"cloud.final:{attempt_id}",state,
                        "upstream_http_error" if outcome=="upstream_error" else None,self.source_id,None,stamp))
            for name,unit,quantity,knowledge,reason,provenance in metrics:
                db.execute("INSERT INTO usage_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (project,request_id,1,self.source_id,attempt_id,name,unit,quantity,
                            knowledge,reason,provenance,stamp))

    def mark_unknown(self, *, project: str, request_id: str, attempt_id: str,
                     reason: str, partial_ref: Any | None) -> str:
        for n,v in (("project_id",project),("request_id",request_id),("attempt_id",attempt_id),("reason",reason)):_required(v,n)
        with self.catalog._mutex:
            current=self.catalog.db.execute("SELECT outcome FROM attempts WHERE project_id=? AND request_id=? AND execution_id=?",
                                            (project,request_id,attempt_id)).fetchone()
        if current is not None and current[0] in {"completed","upstream_error"}:
            return current[0]
        reference=None
        if partial_ref is not None:
            reference=self._ref(project,partial_ref)
            if reference[0]!=reference[3]:raise CatalogConflict("partial_artifact_hash_mismatch")
        with self.catalog._tx() as db:
            attempt=db.execute("SELECT state,outcome,partial_artifact_id FROM attempts WHERE project_id=? AND request_id=? AND execution_id=?",
                               (project,request_id,attempt_id)).fetchone()
            if attempt is None:raise CatalogConflict("cloud_attempt_not_started")
            if attempt["outcome"] in {"completed","upstream_error"}:
                return attempt["outcome"]
            partial_id=None
            if reference is not None:
                digest,uri,size,_=reference
                partial_id=self._insert_artifact(db,project,"partial",digest,uri,size,self.source_id)
            if attempt["state"]=="UNKNOWN":
                if partial_id is not None and attempt["partial_artifact_id"] not in (None,partial_id):
                    raise CatalogConflict("unknown_partial_conflict")
                if partial_id is not None and attempt["partial_artifact_id"] is None:
                    db.execute("UPDATE attempts SET partial_artifact_id=? WHERE project_id=? AND request_id=? AND execution_id=?",
                               (partial_id,project,request_id,attempt_id))
                return "unknown"
            stamp=_now()
            db.execute("UPDATE attempts SET state='UNKNOWN',revision=revision+1,partial_artifact_id=?,error_code=?,usage_status='unknown',updated_at_ns=? WHERE project_id=? AND request_id=? AND execution_id=?",
                       (partial_id,reason,stamp,project,request_id,attempt_id))
            db.execute("UPDATE requests SET state='UNKNOWN',revision=revision+1,updated_at_ns=? WHERE project_id=? AND request_id=?",
                       (stamp,project,request_id))
            db.execute("INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?,?)",
                       (project,request_id,1,f"cloud.unknown:{attempt_id}","UNKNOWN",reason,self.source_id,None,stamp))
            return "unknown"

    def get_attempt_state(self, project: str, request_id: str, attempt_id: str) -> str | None:
        _required(project,"project_id");_required(request_id,"request_id");_required(attempt_id,"attempt_id")
        with self.catalog._mutex:
            row=self.catalog.db.execute("SELECT state,outcome FROM attempts WHERE project_id=? AND request_id=? AND execution_id=?",
                                        (project,request_id,attempt_id)).fetchone()
        return (row["outcome"] or row["state"].lower()) if row else None

    def daily_usage(self, day_cst: str, *, project: str | None = None) -> list[dict[str, Any]]:
        """V9 admin row shape, including deduplicated legacy cloud ledger events."""
        if not isinstance(day_cst,str) or len(day_cst)!=10:
            raise ValueError("invalid_day")
        try:
            parsed=date.fromisoformat(day_cst)
        except ValueError as exc:
            raise ValueError("invalid_day") from exc
        zone=timezone(timedelta(hours=8))
        start=int(datetime(parsed.year,parsed.month,parsed.day,tzinfo=zone).timestamp()*1_000_000_000)
        end=int((datetime(parsed.year,parsed.month,parsed.day,tzinfo=zone)+timedelta(days=1)).timestamp()*1_000_000_000)
        sql="""SELECT a.project_id,a.request_id,a.attempt,a.usage_status,r.caller_id,r.provider,r.model,
            u.metric_name,u.unit,u.quantity_decimal,u.knowledge
            FROM attempts a JOIN requests r ON (a.project_id=r.project_id AND a.request_id=r.request_id)
            LEFT JOIN usage_facts u ON (u.project_id=a.project_id AND u.request_id=a.request_id
                 AND u.attempt=a.attempt AND u.source_id=?)
            WHERE a.updated_at_ns>=? AND a.updated_at_ns<? AND r.source_id=?
              AND (a.outcome IS NOT NULL OR a.state='UNKNOWN')"""
        args:[Any]=[self.source_id,start,end,self.source_id]
        if project is not None:
            sql+=" AND r.project_id=?"
            args.append(_required(project,"project_id"))
        groups:dict[tuple,dict[str,Any]]={}
        def bucket(caller: str, provider: str, model: str | None) -> dict[str,Any]:
            key=(caller,provider,model)
            return groups.setdefault(key,{"caller":caller,"provider":provider,"model":model,
                                          "events":set(),"unknown_events":set(),
                                          "tokens":{},"native":{}})
        def add_token(group: dict[str,Any], name: str, value: Any) -> None:
            if value is not None:
                group["tokens"][name]=group["tokens"].get(name,0)+int(value)
        def add_native(group: dict[str,Any], name: str, unit: str, value: str) -> None:
            key=(name,unit)
            group["native"][key]=group["native"].get(key,Decimal(0))+Decimal(value)
        with self.catalog._mutex:
            rows=self.catalog.db.execute(sql,args).fetchall()
            old_events=(self.catalog.db.execute("SELECT * FROM legacy_cloud_events WHERE day_cst=?",(day_cst,)).fetchall()
                        if project is None else [])
            old_metrics=(self.catalog.db.execute("""SELECT m.event_id,e.caller,e.provider,e.model,
                m.metric_name,m.unit,m.quantity_decimal FROM legacy_cloud_metrics m
                JOIN legacy_cloud_events e ON e.event_id=m.event_id WHERE e.day_cst=?""",
                (day_cst,)).fetchall() if project is None else [])
        for row in rows:
            group=bucket(row["caller_id"],row["provider"],
                         None if row["model"]=="unknown" else row["model"])
            event_key=("new",row["project_id"],row["request_id"],row["attempt"])
            group["events"].add(event_key)
            if row["usage_status"]=="unknown":group["unknown_events"].add(event_key)
            if row["knowledge"]=="known":
                if row["metric_name"] in {"input_tokens","output_tokens","total_tokens"} and row["unit"]=="tokens":
                    add_token(group,row["metric_name"],row["quantity_decimal"])
                else:
                    add_native(group,row["metric_name"],row["unit"],row["quantity_decimal"])
        for row in old_events:
            group=bucket(row["caller"],row["provider"],row["model"])
            event_key=("v9",row["event_id"])
            group["events"].add(event_key)
            if row["usage_status"]=="unknown":group["unknown_events"].add(event_key)
            for name in ("input_tokens","output_tokens","total_tokens"):
                add_token(group,name,row[name])
        for row in old_metrics:
            group=bucket(row["caller"],row["provider"],row["model"])
            add_native(group,row["metric_name"],row["unit"],row["quantity_decimal"])
        result=[]
        for key in sorted(groups,key=lambda x:tuple("" if v is None else str(v) for v in x)):
            group=groups[key]
            result.append({"caller":group["caller"],"provider":group["provider"],
                           "model":group["model"],"calls":len(group["events"]),
                           "unknown_calls":len(group["unknown_events"]),
                           "input_tokens":group["tokens"].get("input_tokens"),
                           "output_tokens":group["tokens"].get("output_tokens"),
                           "total_tokens":group["tokens"].get("total_tokens"),
                           "native_usage":[{"metric_name":name,"unit":unit,"quantity_decimal":str(amount)}
                                           for (name,unit),amount in sorted(group["native"].items())]})
        result.sort(key=lambda row:(-row["calls"],row["caller"],row["provider"],str(row["model"])))
        return result
