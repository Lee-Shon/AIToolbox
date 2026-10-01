"""LocalAI-managed runtime for the desktop formal local-product service."""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
import hashlib
import io
from http.client import HTTPConnection
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import uuid

from .service import (CAPABILITIES, Handler, Product, ProductError,
                      Server, SharedContext, digest, red_png, save_json)
from .scheduling import Placement, gpu_snapshot, instance_budget


OCR_FIXTURE = Path(__file__).parent / "fixtures" / "ocr-7319.png"

class NativePreparation(tuple):
    def __new__(cls, payload, input_tokens, info):
        value = super().__new__(cls, (payload, input_tokens))
        value.info = info
        return value
def config_hash(config: dict) -> str:
    config = dict(config)
    # LocalAI expands these defaults on first load without changing behavior.
    if config.get("reasoning") == {
            "disable_reasoning_tag_prefill": True, "disable": False}:
        config["reasoning"] = {}
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def config_matches(config, expected):
    if config_hash(config) == expected:
        return True
    # Some tokenizer templates expand an absent reasoning default to false.
    # Keep existing raw-false hashes valid, and accept that expansion only
    # for a binding originally frozen with absent defaults.
    if config.get('reasoning') == {'disable_reasoning_tag_prefill': False, 'disable': False}:
        return config_hash(dict(config, reasoning={})) == expected
    return False


class LocalAIClient:
    resource_snapshot = staticmethod(gpu_snapshot)
    resource_budget = staticmethod(instance_budget)
    def __init__(self, base_url: str, key_file: Path):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError("localai_requires_loopback")
        self.base_url = base_url.rstrip("/")
        self.port = parsed.port or 80
        self.key_file = key_file.resolve(strict=True)
        if len(self.key_file.read_text(encoding="ascii").strip()) < 32:
            raise ValueError("invalid_localai_key")

    def _key(self) -> str:
        return self.key_file.read_text(encoding="ascii").strip()

    def request(self, method: str, path: str, value: dict | None = None,
                *, timeout: int = 30) -> dict:
        data = json.dumps(value, ensure_ascii=False).encode() if value is not None else None
        headers = {"Authorization": "Bearer " + self._key()}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read(512).decode(errors="replace")
            raise ProductError(f"localai_http_{exc.code}:{detail}", 502) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise ProductError("localai_unavailable", 503) from exc

    def config(self, name: str) -> dict:
        return self.request("GET", "/api/models/config-json/" + quote(name))

    def import_model(self, name: str, spec: dict) -> str:
        try:
            self.config(name)
        except ProductError as exc:
            if not exc.code.startswith("localai_http_404:"):
                raise
            seed = {"name": name, "backend": spec["backend"],
                    "parameters": {"model": spec["localai_model"]},
                    "context_size": spec["max_context_tokens"],
                    "known_usecases": spec["usecases"]}
            self.request("POST", "/models/import", seed, timeout=60)
        tuning = {"backend": spec["backend"],
                  "parameters": {"model": spec["localai_model"]},
                  "context_size": spec["max_context_tokens"],
                  "max_model_len": spec["max_context_tokens"],
                  "known_usecases": spec["usecases"],
                  "known_input_modalities": spec["input_modalities"],
                  "known_output_modalities": ["text"],
                  "description": "Managed by AIToolbox V11"}
        tuning.update(spec["tuning"])
        if spec.get('batch_size') is not None:
            tuning['parameters']['batch'] = spec['batch_size']
        self.request("PATCH", "/api/models/config-json/" + quote(name), tuning, timeout=60)
        return config_hash(self.config(name))

    def clone_model(self, name, row):
        original = self.config(row['localai_name'])
        if not config_matches(original, row['localai_hash']):
            raise ProductError('localai_configuration_changed', 409)
        frozen = dict(original, name=name)
        self.request('POST', '/models/import', frozen, timeout=60)
        self.request('PATCH', '/api/models/config-json/'+quote(name), frozen, timeout=60)

    def retire(self, name: str) -> None:
        try:
            self.request("PATCH", "/api/models/config-json/" + quote(name), {
                "parameters": {"model": ".retired/" + name + ".missing"},
                "description": "Retired by AIToolbox V11; original assets preserved",
                "known_usecases": [],
            }, timeout=60)
        except ProductError as exc:
            if not exc.code.startswith("localai_http_404:"):
                raise

    def shutdown(self, name: str) -> None:
        try:
            self.request("POST", "/backend/shutdown", {"model": name}, timeout=90)
        except ProductError as exc:
            if not exc.code.startswith("localai_http_404:"):
                raise

    def chat(self, name: str, content: Any, max_tokens: int = 96,
             context_tokens: int | None = None) -> dict:
        payload = {
            "model": name, "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens, "temperature": 0,
        }
        if context_tokens is None:
            context_tokens = int(self.config(name)["context_size"])
        request_id = "probe-" + uuid.uuid4().hex
        original = json.dumps(payload, sort_keys=True).encode()
        prepared = None
        try:
            prepared_info = self.prepare(payload, request_id, context_tokens, 1,
                                         hashlib.sha256(original).hexdigest())
            prepared, count = prepared_info
            answer = self.request("POST", "/v1/chat/completions", prepared, timeout=360)
            answer["native_preparation"] = {"input_tokens": count}
            profile = getattr(prepared_info, 'info', {}).get('resource_profile')
            if profile:
                answer['native_preparation']['resource_profile'] = profile
            return answer
        finally:
            if prepared is not None:
                try:
                    self.discard(name, request_id)
                except ProductError:
                    pass  # The probe owner also unloads the backend in finally.

    def transcribe(self, name: str, wav: bytes, context_tokens: int | None = None) -> dict:
        audio = {"type": "input_audio", "input_audio": {
            "data": base64.b64encode(wav).decode(), "format": "wav"}}
        return self.chat(name, [
            {"type": "text", "text": "Transcribe the speech in this audio verbatim. "
                                    "Return only the spoken words, no explanation."},
            audio], 128, context_tokens)

    def prepare(self, payload: dict, request_id: str, context: int,
                choices: int, input_digest: str) -> tuple[dict, int]:
        payload = dict(payload)
        if payload.get("seed", -1) == -1:
            # LocalAI otherwise chooses a new seed for each HTTP request.
            # Preparation and generation are phases of ONE original call.
            payload["seed"] = secrets.randbelow(2 ** 31)
        metadata = dict(payload.get("metadata") or {})
        if any(key.startswith("aitoolbox_") for key in metadata):
            raise ProductError("reserved_metadata_key", 422)
        layout = []
        for message in payload["messages"]:
            content = message.get("content")
            layout.append([{"type": "text", "text": part["text"]}
                           if part["type"] == "text" else {"type":
                           "image" if part["type"] == "image_url" else "audio"}
                           for part in content] if isinstance(content, list) else None)
        metadata.update(aitoolbox_request_id=request_id,
                        aitoolbox_context_tokens=str(context),
                        aitoolbox_choices=str(choices),
                        aitoolbox_input_digest=input_digest,
                        aitoolbox_media_layout=json.dumps(layout, ensure_ascii=False),
                        aitoolbox_phase="prepare")
        prepared_payload = {**payload, "metadata": metadata, "stream": False, "n": 1}
        prepared_payload.pop("stream_options", None)
        try:
            answer = self.request("POST", "/v1/chat/completions", prepared_payload, timeout=360)
        except ProductError as exc:
            if "requested_context_insufficient" in exc.code:
                raise ProductError("requested_context_insufficient", 422) from exc
            raise
        try:
            prepared = json.loads(answer["choices"][0]["message"]["content"])
            tokens = prepared["input_tokens"]
            native_kwargs = prepared["native_template_kwargs"]
            if prepared["aitoolbox_prepared"] != 1 or type(tokens) is not int or tokens < 0:
                raise ValueError()
            if not isinstance(native_kwargs, dict):
                raise ValueError()
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise ProductError("native_preparation_contract_unavailable", 502) from exc
        if tokens + payload["max_tokens"] > context:
            self.discard(payload["model"], request_id)
            raise ProductError("requested_context_insufficient", 422)
        return NativePreparation({**payload, "metadata": {**metadata, "aitoolbox_phase": "execute",
                "chat_template_kwargs": json.dumps(native_kwargs, sort_keys=True)}}, tokens, prepared)

    def discard(self, model: str, request_id: str) -> None:
        self.request("POST", "/v1/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": "discard"}],
            "max_tokens": 1, "temperature": 0,
            "metadata": {"aitoolbox_phase": "discard", "aitoolbox_request_id": request_id}},
            timeout=30)

    def execution_control(self, model: str, request_id: str, phase='status') -> dict:
        answer = self.request('POST', '/v1/chat/completions', {
            'model': model, 'messages': [{'role': 'user', 'content': phase}],
            'max_tokens': 1, 'temperature': 0,
            'metadata': {'aitoolbox_phase': phase, 'aitoolbox_request_id': request_id}}, timeout=45)
        return json.loads(LocalAIProduct._content(answer))


class LocalAIRuntime:
    def __init__(self, client: LocalAIClient):
        self.client = client
        self.lifecycle = threading.RLock()
        self.lock = threading.RLock()
        self.model_id: str | None = None
        self.localai_name: str | None = None
        self.binding: tuple | None = None
        self.capacity: dict = {}
        self.pool: SharedContext | None = None
        self.placement = Placement(client)
        self.management_instance = None
        self.management_ticket = None

    def acquire(self, row, tokens, order=None, cancelled=lambda: False, ticket=None):
        try:
            if not config_matches(self.client.config(row['localai_name']), row['localai_hash']):
                raise ProductError('localai_configuration_changed', 409)
            instance = self.placement.acquire(ticket or self.placement.enqueue(row, tokens, order, cancelled))
        except BaseException:
            if ticket is not None:
                self.placement.cancel(ticket)
            raise
        with self.lock:
            self.model_id, self.localai_name, self.binding = row['id'], instance.name, instance.binding
            self.pool = instance.pool
            self.capacity = {'native_context_tokens': instance.pool.total,
                             'total_context_tokens': instance.pool.total, 'shared_context': True}
        return instance

    def release_instance(self, instance, tokens):
        self.placement.release(instance, tokens)
        with self.lock:
            if not self.placement.instances:
                self.model_id = self.localai_name = self.binding = None
                self.pool = None
                self.capacity = {}

    def start(self, row: dict) -> int:
        with self.lifecycle:
            binding = (row["id"], row["revision"], row["localai_hash"])
            if self.management_instance is not None and self.management_instance.binding == binding:
                return self.client.port
            self.stop_management()
            name = row["localai_name"]
            if not config_matches(self.client.config(name), row["localai_hash"]):
                raise ProductError("localai_configuration_changed", 409)
            self.management_ticket = self.placement.enqueue(row, row['max_context_tokens'])
            self.management_instance = self.acquire(row, row['max_context_tokens'], ticket=self.management_ticket)
            return self.client.port

    def stop_management(self):
        if self.management_instance is not None:
            instance, self.management_instance = self.management_instance, None
            instance.pool.cancel(self.management_ticket['pool_ticket'])
            self.management_ticket = None
            self.release_instance(instance, instance.pool.total)

    def stop(self) -> None:
        with self.lifecycle:
            self.stop_management()
            with self.placement.condition:
                for instance in list(self.placement.instances):
                    if instance.inflight:
                        raise ProductError('runtime_has_active_requests', 409)
                    self.client.shutdown(instance.name)
                    self.placement.instances.remove(instance)
            self.model_id = None
            self.localai_name = None
            self.binding = None
            self.capacity = {}
            self.pool = None

    def stop_model(self, model_id):
        with self.placement.condition:
            for instance in list(self.placement.instances):
                if instance.row['id'] != model_id:
                    continue
                if instance.inflight:
                    raise ProductError('model_has_active_requests', 409)
                self.client.shutdown(instance.name)
                self.placement.instances.remove(instance)
                if instance.name != instance.row['localai_name']:
                    self.client.retire(instance.name)
            self.placement.condition.notify_all()


class LocalAIProduct(Product):
    def __init__(self, root: Path, data_service_url: str,
                 data_admin_token_file: Path, client: LocalAIClient,
                 mounts: list[tuple[Path, str]]):
        self.client = client
        self.mounts = [(host.resolve(strict=True), dest.strip("/")) for host, dest in mounts]
        if not self.mounts or any(not dest.startswith("models/assets-") for _, dest in self.mounts):
            raise ValueError("invalid_localai_asset_mounts")
        super().__init__(root, LocalAIRuntime(client), data_service_url, data_admin_token_file)
        self.batch_lock = threading.RLock()
        self.batches = {}
        self.active_requests = {}
        self.arrival_sequence = 0
        contract = json.loads(Path(__file__).with_name('native-contract.json').read_text('utf-8'))
        adapter_hash, cpp_hash = contract['vllm_adapter_sha256'], contract['llama_adapter_sha256']
        for row in self.rows.values():
            profile = row.get('native_resource_profile')
            if profile and profile.get('adapter_sha256') in {adapter_hash, cpp_hash}:
                self.runtime.placement.profiles[(row['id'],row['revision'],row['localai_hash'])] = profile
        for path in (self.root / 'requests').glob('*.json'):
            record = json.loads(path.read_text('utf-8'))
            profile = record.get('native_resource_profile')
            row = self.rows.get(record.get('model'))
            if (profile and row and profile.get('adapter_sha256') in {adapter_hash, cpp_hash}
                    and (record.get('revision'), record.get('localai_hash')) == (row['revision'], row.get('localai_hash'))):
                binding = (row['id'], row['revision'], row['localai_hash'])
                prior = self.runtime.placement.profiles.get(binding, {})
                self.runtime.placement.profiles[binding] = dict(profile,
                    resident_gpu_bytes=max(profile['resident_gpu_bytes'], prior.get('resident_gpu_bytes', 0)),
                    load_gpu_bytes=max(profile['load_gpu_bytes'], prior.get('load_gpu_bytes', 0)))
        with self.lock:
            for row in self.rows.values():
                if row["state"] == "READY" and not row.get("localai_hash"):
                    row.update(state="REJECTED", error="legacy_cpu_registration_requires_update")
            self._save()

    def batch_view(self, key):
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ProductError('invalid_batch_key')
        records = []
        with self.lock:
            for path in (self.root / 'requests').glob('*.json'):
                record = json.loads(path.read_text('utf-8'))
                if record.get('association', {}).get('key') == key:
                    records.append(record)
        if not records:
            raise ProductError('batch_not_found', 404)
        records.sort(key=lambda value: value['association']['ordinal'])
        terminal = all(record['state'] in {'COMPLETED', 'FAILED', 'CANCELLED', 'UNKNOWN'} for record in records)
        return dict(key=key, object_id=records[0]['association']['object_id'],
                    batch_id=records[0]['association']['batch_id'],
                    state=('COMPLETED' if all(r['state'] == 'COMPLETED' for r in records) else 'FAILED')
                          if terminal else 'PENDING', requests=records)

    def submit_batch(self, body, original=None, content_type='application/json'):
        if not isinstance(body, dict) or set(body) != {'object_id', 'batch_id', 'requests'}:
            raise ProductError('invalid_ordered_batch')
        if any(not isinstance(body[key], str) or not 0 < len(body[key]) <= 200
               for key in ('object_id', 'batch_id')):
            raise ProductError('object_and_batch_required')
        if not isinstance(body['requests'], list) or not 0 < len(body['requests']) <= 256:
            raise ProductError('invalid_batch_requests')
        key = hashlib.sha256(json.dumps([body['object_id'], body['batch_id']], ensure_ascii=False).encode()).hexdigest()
        manifest_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        with self.batch_lock:
            try:
                prior = self.batch_view(key)
            except ProductError as exc:
                if exc.code != 'batch_not_found':
                    raise
            else:
                if prior['requests'][0]['association']['manifest_sha256'] != manifest_hash:
                    raise ProductError('batch_identity_conflict', 409)
                return prior
            accepted, segment, previous = [], -1, None
            dispatch_ready, intake_aborted = threading.Event(), threading.Event()
            try:
                for ordinal, member in enumerate(body['requests']):
                    if not isinstance(member, dict) or set(member) != {'request_id', 'body'}:
                        raise ProductError('invalid_batch_member')
                    request = member['body']
                    if not isinstance(request, dict) or request.get('stream'):
                        raise ProductError('batch_nonstream_chat_required', 422)
                    model = request.get('model')
                    if model != previous:
                        segment += 1
                    previous = model
                    handler = object.__new__(V11Handler)
                    handler.server = SimpleNamespace(product=self)
                    handler.headers = {'Content-Type': 'application/json', 'X-Request-ID': member['request_id']}
                    handler._body = lambda request=request: json.dumps(request, ensure_ascii=False).encode()
                    handler.intake_only = True
                    handler.association = dict(key=key, object_id=body['object_id'], batch_id=body['batch_id'],
                        ordinal=ordinal, segment=segment, manifest_sha256=manifest_hash)
                    handler.send_response = handler.send_header = handler.end_headers = lambda *args: None
                    handler.wfile = io.BytesIO()
                    args = handler._proxy('/v1/chat/completions')
                    if ordinal == 0:
                        # The members have no individual HTTP byte stream.
                        # Keep the original manifest once in the sole receipts.
                        manifest = original if original is not None else json.dumps(body, ensure_ascii=False).encode()
                        args['record']['batch_input_base64'] = base64.b64encode(manifest).decode()
                        args['record']['batch_input_content_type'] = content_type
                    accepted.append((handler, args))
                self.batches[key] = {'failed_segments': set(),
                    'segment_cancellations': {args['record']['association']['segment']: threading.Event()
                                              for _, args in accepted}}
                # Enqueue the complete manifest before the first placement.
                # This reaches same-model tails across intervening models.
                for handler, args in accepted:
                    record = args['record']
                    with self.lock:
                        save_json(args['record_path'], record)
                    # Placement holds its condition while checking cancellation.
                    # Batch intake holds batch_lock while enqueuing placement.
                    # A lock-free Event avoids that reverse lock acquisition.
                    cancelled = self.batches[key]['segment_cancellations'][
                        record['association']['segment']].is_set
                    args['ticket'] = self.runtime.placement.enqueue(args['row'], args['context'] * args['choices'],
                                                                   record['arrival_sequence'], cancelled)
                for handler, args in accepted:
                    def run(handler=handler, args=args):
                        dispatch_ready.wait()
                        if intake_aborted.is_set():
                            return
                        try:
                            handler._execute(**args)
                        except Exception:
                            # The shared execution path writes the sole receipt;
                            # a background HTTP caller has no socket to report to.
                            pass
                    self._launch(run, ())
                dispatch_ready.set()
            except BaseException as intake_error:
                intake_aborted.set()
                try:
                    for handler, args in accepted:
                        try:
                            if args.get('ticket') is not None:
                                self.runtime.placement.cancel(args['ticket'])
                        except Exception as cleanup_error:
                            args['record']['placement_cleanup_error'] = str(cleanup_error)
                            intake_error.add_note('placement cleanup: ' + str(cleanup_error))
                        try:
                            args['record'].update(state='FAILED', error='batch_intake_aborted',
                                stop_proof={'stop_confirmed': True, 'basis': 'not_submitted'}, updated_at=time.time())
                            with self.lock:
                                save_json(args['record_path'], args['record'])
                        except Exception as save_error:
                            intake_error.add_note('aborted receipt: ' + str(save_error))
                        finally:
                            self.release_request(args['row'])
                finally:
                    dispatch_ready.set()
                raise
        return self.batch_view(key)

    def fail_segment(self, origin):
        association = origin['association']
        with self.batch_lock:
            self.batches[association['key']]['failed_segments'].add(association['segment'])
            cancellation = self.batches[association['key']].get('segment_cancellations', {}).get(
                association['segment'])
            if cancellation is not None:
                cancellation.set()
            targets = []
            for request_id, control in self.active_requests.items():
                if request_id == origin['request_id'] or control.get('terminal'):
                    continue
                other = control.get('association')
                if other and (other['key'], other['segment']) == (association['key'], association['segment']):
                    control['cancel'].set()
                    targets.append((request_id, control))
        for request_id, control in targets:
            instance = control['instance']
            if instance is not None and control['prepared']:
                try:
                    control['stop_proof'] = self.client.execution_control(instance.name, request_id, 'cancel')
                except Exception as exc:
                    control['stop_proof'] = {'stop_confirmed': False, 'error': str(exc)}

    def _mapped(self, path: Path) -> str:
        resolved = path.resolve(strict=True)
        for host, target in self.mounts:
            try:
                relative = resolved.relative_to(host)
                return (Path(target) / relative).as_posix().removeprefix("models/")
            except ValueError:
                continue
        raise ProductError("model_asset_outside_readonly_mount", 422)

    def registration_spec(self, body: dict) -> dict:
        model_id = body.get("id")
        if (not isinstance(model_id, str) or not 1 <= len(model_id) <= 80
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", model_id)):
            raise ProductError("invalid_model_id")
        context = self.settings(body.get("max_context_tokens"))
        caps = body.get("capabilities")
        if (not isinstance(caps, list) or not caps
                or not all(isinstance(cap, str) for cap in caps)
                or len(set(caps)) != len(caps)
                or not set(caps) <= CAPABILITIES or "text_output" not in caps
                or not ({"text_input", "image_input", "audio_input"} & set(caps))):
            raise ProductError("invalid_capabilities")
        path = body.get("model_path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ProductError("model_path_absolute_required")
        try:
            main = Path(path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProductError("model_asset_missing", 422) from exc
        mmproj_value = body.get("mmproj_path")
        try:
            mmproj = Path(mmproj_value).resolve(strict=True) if mmproj_value else None
        except (OSError, RuntimeError) as exc:
            raise ProductError("model_projector_missing", 422) from exc
        if main.is_file() and main.suffix.lower() == ".gguf":
            backend = "cuda12-llama-cpp"
            if {"image_input", "audio_input"} & set(caps) and not mmproj:
                raise ProductError("multimodal_projector_required")
            if mmproj and (not mmproj.is_file() or mmproj.suffix.lower() != ".gguf"):
                raise ProductError("multimodal_projector_invalid")
            usecases = ["chat"]
            if "image_input" in caps:
                usecases.append("vision")
            if "audio_input" in caps:
                usecases.append("transcript")
            tuning = {"gpu_layers": 999, "threads": 16,
                      "template": {"use_tokenizer_template": True}}
            batch_size = body.get('batch_size')
            if batch_size is not None:
                if type(batch_size) is not int or not 16 <= batch_size <= 8192:
                    raise ProductError('invalid_batch_size')
            if mmproj:
                tuning["mmproj"] = self._mapped(mmproj)
        elif main.is_dir() and (main / "config.json").is_file() and list(main.glob("*.safetensors")):
            if body.get('batch_size') is not None:
                raise ProductError('batch_size_not_supported_for_backend', 422)
            backend = "cuda13-vllm"
            if mmproj:
                raise ProductError("projector_not_valid_for_safetensors")
            try:
                config = json.loads((main / "config.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProductError("invalid_model_config", 422) from exc
            architectures = config.get("architectures", [])
            architecture = architectures[0] if architectures else ""
            usecases = ["transcript"] if "audio_input" in caps else ["chat", "vision"]
            tuning = {"gpu_memory_utilization": 0.35 if "audio_input" in caps else 0.45,
                      "enforce_eager": True,
                      "env": {"VLLM_USE_V2_MODEL_RUNNER": "0",
                              "VLLM_USE_FLASHINFER_SAMPLER": "0"},
                      "template": {"use_tokenizer_template": True}}
            if architecture == "DeepseekOCR2ForCausalLM":
                tuning.update(engine_args={
                    "moe_backend": "triton", "enable_prefix_caching": False,
                    "mm_processor_cache_gb": 0,
                    "logits_processors": [
                        "vllm.model_executor.models.deepseek_ocr:NGramPerReqLogitsProcessor"]},
                    template={"use_tokenizer_template": False,
                              "chat": "<image>\n{{.Input}}"})
            elif architecture != "GlmAsrForConditionalGeneration" and "audio_input" in caps:
                raise ProductError("asr_backend_unverified", 422)
        else:
            raise ProductError("unsupported_local_model_format", 422)
        localai_model = self._mapped(main)
        if mmproj:
            self._mapped(mmproj)
        modalities = [name for cap, name in (("text_input", "text"),
                                             ("image_input", "image"),
                                             ("audio_input", "audio")) if cap in caps]
        return {"id": model_id, "model_path": str(main),
                **({'batch_size': body['batch_size']} if body.get('batch_size') is not None else {}),
                "mmproj_path": str(mmproj) if mmproj else None,
                "capabilities": sorted(caps), "max_context_tokens": context,
                "backend": backend, "localai_model": localai_model,
                "usecases": usecases, "input_modalities": modalities, "tuning": tuning}

    def _assets(self, row: dict) -> dict:
        result = {}
        for field in ("model_path", "mmproj_path"):
            value = row.get(field)
            if not value:
                continue
            path = Path(value)
            entries = list(path.rglob("*")) if path.is_dir() else [path]
            if any(item.is_symlink() for item in entries):
                raise ProductError("model_asset_symlink_not_allowed", 422)
            files = sorted(p for p in entries if p.is_file())
            if not files:
                raise ProductError("model_asset_empty", 422)
            for item in files:
                self._mapped(item)
            result[field] = [{"path": str(item), "size": item.stat().st_size,
                              "mtime_ns": item.stat().st_mtime_ns, "sha256": digest(item)}
                             for item in files]
        return result

    @staticmethod
    def _content(answer: dict) -> str:
        return str(answer.get("choices", [{}])[0].get("message", {}).get("content") or "")

    def _probe(self, row: dict) -> dict:
        checked = {}
        name = row["localai_name"]
        caps = set(row["capabilities"])
        if "text_input" in caps:
            answer = self.client.chat(name, "Reply with the word ready.",
                                      512,
                                      context_tokens=row["max_context_tokens"])
            if "ready" not in self._content(answer).lower():
                raise ProductError("text_probe_wrong_answer:" + self._content(answer)[:120], 422)
            checked["text_input"] = {"content": self._content(answer)[:120],
                                      "native_preparation": answer.get('native_preparation')}
        if "image_input" in caps:
            if row["backend"] == "cuda13-vllm":
                image = OCR_FIXTURE.read_bytes()
                prompt, expected = "Free OCR.", "AIT V11 OCR 7319"
            else:
                image = red_png()
                prompt, expected = "What is the main color? One color.", "red"
            picture = "data:image/png;base64," + base64.b64encode(image).decode()
            answer = self.client.chat(name, [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": picture}},
            ], 128, context_tokens=row["max_context_tokens"])
            content = self._content(answer)
            if expected.lower() not in content.lower():
                raise ProductError("image_probe_wrong_answer:" + content[:100], 422)
            checked["image_input"] = {"content": content[:120],
                                      "native_preparation": answer.get('native_preparation')}
        if "audio_input" in caps:
            from .probes import speech_probe
            answer = self.client.transcribe(name, speech_probe(), row["max_context_tokens"])
            content = self._content(answer)
            if not all(word in content.lower() for word in ("blue", "sky", "stars")):
                raise ProductError("audio_probe_wrong_transcript:" + content[:120], 422)
            checked["audio_input"] = {"content": content[:220],
                                      "native_preparation": answer.get('native_preparation')}
        checked["text_output"] = next(iter(checked.values()))
        if set(checked) != caps:
            raise ProductError("capability_probe_incomplete", 422)
        return checked

    def _validate_row(self, row: dict) -> dict:
        try:
            spec = self.registration_spec(row["spec"])
            row.update(spec)
            row["spec"] = spec
            row["assets"] = self._assets(row)
            row["localai_name"] = row.get("localai_name") or row["id"]
            with self.runtime.lifecycle:
                try:
                    row["localai_hash"] = self.client.import_model(row["localai_name"], spec)
                    self.runtime.start(row)
                    checked = self._probe(row)
                    for evidence in checked.values():
                        profile = (evidence.get('native_preparation') or {}).get('resource_profile')
                        if profile:
                            self.runtime.placement.observe_profile(self.runtime.management_instance, profile)
                            row['native_resource_profile'] = dict(profile,
                                observed_gpu_total_bytes=self.runtime.placement.last_observation['total_bytes'])
                    row["runtime_shape"] = dict(self.runtime.capacity)
                finally:
                    self.runtime.stop_management()
            row.update(state="READY", error=None, checked=checked,
                       updated_at=time.time())
        except Exception as exc:
            row.update(state="REJECTED", error=str(exc), checked={},
                       updated_at=time.time())
        return row

    def _apply_update(self, model_id: str, revision: int) -> None:
        with self.idle:
            old = self.rows[model_id]
            while self.request_counts.get((model_id, old["revision"]), 0):
                self.idle.wait()
            pending = old["pending_update"]
            candidate_key = hashlib.sha256(f"{model_id}:{revision}".encode()).hexdigest()[:24]
            name = f"ait-v11-candidate-{candidate_key}"
            candidate = {**pending["spec"], "spec": pending["spec"],
                         "revision": revision, "state": "VALIDATING", "assets": {},
                         "checked": {}, "error": None, "localai_name": name}
        result = self._validate_row(candidate)
        rollback_failed = False
        if result["state"] == "READY":
            try:
                with self.runtime.lifecycle:
                    # The old config stays intact throughout candidate probing.
                    self.runtime.stop_model(model_id)
                    self.client.shutdown(model_id)
                    result["localai_hash"] = self.client.import_model(model_id, pending["spec"])
                    result["localai_name"] = model_id
            except Exception as exc:
                result.update(state="REJECTED", error=str(exc), checked={})
                try:
                    if old.get("localai_hash"):
                        restored = self.client.import_model(model_id, old["spec"])
                        if restored != old["localai_hash"]:
                            raise ProductError("old_configuration_restore_mismatch", 409)
                    else:
                        self.client.retire(model_id)
                except Exception as rollback_error:
                    rollback_failed = True
                    with self.lock:
                        old.update(state="REJECTED", error="update_rollback_failed:" + str(rollback_error))
                        self._save()
        try:
            self.client.retire(name)
        except Exception as exc:
            result["candidate_retire_error"] = str(exc)
        with self.lock:
            if self.rows.get(model_id) is not old or old.get("pending_update", {}).get("revision") != revision:
                return
            outcome = {"state": result["state"], "revision": revision,
                       "error": result["error"],
                       "max_context_tokens": candidate["max_context_tokens"]}
            if result["state"] == "READY":
                result["last_update"] = outcome
                self.rows[model_id] = result
            else:
                old.update(state="REJECTED" if rollback_failed else pending["previous_state"],
                           update_error=result["error"],
                           last_update=outcome, updated_at=time.time())
                old.pop("pending_update", None)
            self._save()

    def unregister(self, model_id: str) -> dict:
        with self.idle:
            row = self.rows.get(model_id)
            if row is None:
                raise ProductError("model_not_found", 404)
            if row["state"] == "REMOVED":
                return row.copy()
            if row["state"] in {"VALIDATING", "UPDATING"}:
                raise ProductError("model_validation_in_progress", 409)
            row["state"] = "DRAINING"
            self._save()
            while any(key[0] == model_id for key in self.request_counts):
                self.idle.wait()
        with self.runtime.lifecycle:
            with self.lock:
                if self.rows.get(model_id) is not row or row["state"] == "REMOVED":
                    return row.copy()
            self.runtime.stop_model(model_id)
            try:
                if row.get("localai_name"):
                    self.client.retire(row["localai_name"])
            except Exception:
                with self.lock:
                    row.update(state="REJECTED", error="localai_disable_failed")
                    self._save()
                raise
            with self.lock:
                row.update(state="REMOVED", updated_at=time.time())
                self._save()
                return row.copy()

    def get(self, model_id: str) -> dict:
        row = super().get(model_id)
        if row["state"] == "READY" and row.get("localai_hash"):
            changed = not config_matches(self.client.config(row["localai_name"]), row["localai_hash"])
            reason = "localai_configuration_changed"
            if not changed:
                for entries in row.get("assets", {}).values():
                    for item in entries:
                        try:
                            path = Path(item["path"])
                            self._mapped(path)
                            stat = path.stat()
                            if (stat.st_size != item["size"]
                                    or stat.st_mtime_ns != item["mtime_ns"]):
                                changed = True
                                break
                        except (OSError, ProductError):
                            changed = True
                            break
                    if changed:
                        break
                reason = "model_asset_changed"
            if changed:
                with self.lock:
                    latest = self.rows.get(model_id)
                    if latest and latest["revision"] == row["revision"] and latest["state"] == "READY":
                        latest.update(state="REJECTED", error=reason)
                        self._save()
                return super().get(model_id)
        return row

    def list_ready(self) -> list[dict]:
        for model_id in list(self.rows):
            self.get(model_id)
        rows = super().list_ready()
        for row in rows:
            row["owned_by"] = "aitoolbox-v11-localai"
        return rows

    def list_all(self) -> list[dict]:
        rows = super().list_all()
        for row in rows:
            original = self.rows[row["id"]]
            row.update(backend=original.get("backend"),
                       localai_name=original.get("localai_name"),
                       localai_hash=original.get("localai_hash"))
        return rows


class V11Handler(Handler):
    def _handle(self) -> None:
        if self.path.split("?", 1)[0] == "/health" and self.command == "GET":
            self.server.product.client.request("GET", "/v1/models", timeout=5)
            return self._send(200, {"status": "ok", "component": "v11-localai-product",
                                    "version": "11.8.0"})
        path = self.path.split('?', 1)[0]
        if path == '/v1/batches' and self.command == 'POST':
            self._auth()
            original = self._body()
            return self._send(202, self.server.product.submit_batch(json.loads(original), original,
                self.headers.get('Content-Type', 'application/json')))
        if path.startswith('/v1/batches/') and self.command == 'GET':
            self._auth()
            return self._send(200, self.server.product.batch_view(path[len('/v1/batches/'):]))
        if path == '/admin/scheduling' and self.command == 'GET':
            self._auth()
            placement = self.server.product.runtime.placement
            with placement.condition:
                return self._send(200, {'instances': [dict(name=i.name, model=i.row['id'],
                    revision=i.row['revision'], budget=i.budget, inflight=i.inflight,
                    assigned_context_tokens=i.assigned_tokens, reserved_context_tokens=i.pool.tokens)
                    for i in placement.instances], 'events': placement.events[-300:]})
        return super()._handle()

    def _proxy(self, path: str) -> None:
        p: LocalAIProduct = self.server.product
        original = self._body()
        content_type = self.headers.get("Content-Type", "")
        audio = path.endswith("audio/transcriptions")
        response_format = "json"
        if audio:
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
                + original)
            if not message.is_multipart():
                raise ProductError("invalid_transcription_multipart")
            fields, audio_file = {}, None
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name or name in fields or (name == "file" and audio_file is not None):
                    raise ProductError("invalid_transcription_multipart")
                value = part.get_payload(decode=True)
                if name == "file":
                    audio_file = value
                else:
                    fields[name] = value.decode("utf-8")
            allowed = {"model", "context_tokens", "max_tokens", "max_completion_tokens",
                       "temperature", "response_format", "prompt", "language", "stream"}
            for key in fields:
                if key not in allowed:
                    raise ProductError("unsupported_transcription_parameter:" + key, 422)
            for key in ("prompt", "language"):
                if fields.get(key):
                    raise ProductError("unsupported_transcription_parameter:" + key, 422)
            if fields.get("stream", "false").lower() not in {"false", "0"}:
                raise ProductError("unsupported_transcription_parameter:stream", 422)
            response_format = fields.get("response_format", "json")
            if response_format not in {"json", "text"}:
                raise ProductError("unsupported_transcription_parameter:response_format", 422)
            model_id = fields.get("model")
            if (not audio_file or audio_file[:4] != b"RIFF"
                    or audio_file[8:12] != b"WAVE"):
                raise ProductError("transcription_wav_required", 422)
            try:
                context = int(fields.get("context_tokens", ""))
                limit = int(fields.get("max_completion_tokens", fields.get("max_tokens", "")))
                if "max_tokens" in fields and int(fields["max_tokens"]) != limit:
                    raise ProductError("conflicting_output_limits")
                temperature = float(fields.get("temperature", "0"))
            except ValueError as exc:
                raise ProductError("context_and_output_limits_required") from exc
            if not math.isfinite(temperature) or not 0 <= temperature <= 2:
                raise ProductError("invalid_temperature", 422)
            required = {"audio_input", "text_output"}
            choices = 1
            payload = {"model": model_id, "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Transcribe the speech in this audio verbatim. "
                                        "Return only the spoken words, no explanation."},
                {"type": "input_audio", "input_audio": {"format": "wav",
                    "data": base64.b64encode(audio_file).decode()}}]}],
                "max_tokens": limit, "temperature": temperature}
            upstream_stream = False
        else:
            try:
                payload = json.loads(original)
            except ValueError as exc:
                raise ProductError("invalid_json") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
                raise ProductError("invalid_chat_request")
            model_id = payload.get("model")
            context = payload.get("context_tokens")
            limit = payload.get("max_completion_tokens", payload.get("max_tokens"))
            if "max_tokens" in payload and "max_completion_tokens" in payload and payload["max_tokens"] != limit:
                raise ProductError("conflicting_output_limits")
            choices = payload.get("n", 1)
            if type(choices) is not int or choices < 1:
                raise ProductError("invalid_response_choices", 422)
            if choices > 1 and (payload.get("stream") or payload.get("tools") or payload.get("functions")):
                raise ProductError("multiple_choices_stream_or_tools_unsupported", 422)
            required = {"text_output"}
            has_text = has_image = has_audio = False
            for msg in payload["messages"]:
                if not isinstance(msg, dict):
                    raise ProductError("invalid_chat_message")
                if isinstance(msg.get("content"), str):
                    has_text = True
                elif isinstance(msg.get("content"), list):
                    for part in msg["content"]:
                        if not isinstance(part, dict):
                            raise ProductError("invalid_chat_content")
                        if part.get("type") == "text":
                            if not isinstance(part.get("text"), str):
                                raise ProductError("invalid_chat_text", 422)
                            has_text = True
                        elif part.get("type") == "image_url":
                            has_image = True
                        elif part.get("type") in {"input_audio", "audio_url"}:
                            has_audio = True
                        else:
                            raise ProductError("unsupported_chat_content", 422)
                else:
                    raise ProductError("invalid_chat_content", 422)
            if has_image:
                required.add("image_input")
            if has_audio:
                required.add("audio_input")
            if has_text and not (has_image or has_audio):
                required.add("text_input")
            if not (has_text or has_image or has_audio):
                raise ProductError("input_required", 422)
            upstream_stream = bool(payload.get("stream"))
            if upstream_stream and "text/event-stream" not in self.headers.get(
                    "Accept", "text/event-stream"):
                raise ProductError("stream_accept_required", 422)
            payload = {key: value for key, value in payload.items()
                       if key not in {"context_tokens", "max_completion_tokens"}}
            payload["max_tokens"] = limit
            if upstream_stream:
                payload.setdefault("stream_options", {"include_usage": True})
        if not isinstance(model_id, str) or not model_id:
            raise ProductError("model_required")
        if type(context) is not int or context <= 0:
            raise ProductError("max_context_tokens_required")
        if type(limit) is not int or limit <= 0:
            raise ProductError("max_output_tokens_required")
        row = p.get(model_id)
        if row["state"] != "READY":
            raise ProductError("model_not_ready", 409)
        if not required.issubset(row["capabilities"]):
            raise ProductError("capability_not_registered", 422)
        if context * choices > row["max_context_tokens"]:
            raise ProductError("request_exceeds_instance_capacity", 422)
        if limit >= context:
            raise ProductError("requested_context_insufficient", 422)
        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        if len(request_id) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
            raise ProductError("invalid_request_id")
        self.request_id = request_id
        record_path = p.root / "requests" / (request_id + ".json")
        record = {"request_id": request_id, "model": model_id,
                  "revision": row["revision"], "localai_hash": row["localai_hash"],
                  "configuration": {"max_context_tokens": row["max_context_tokens"]},
                  "path": path, "input_base64": base64.b64encode(original).decode(),
                  "input_content_type": content_type,
                  "state": "PENDING", "created_at": time.time(),
                  "response": None, "error": None}
        association = getattr(self, 'association', None)
        if association is not None:
            record['association'] = association
        record_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with p.lock:
                if p.closing:
                    raise ProductError("service_stopping", 503)
                current = p.get(model_id)
                if current["revision"] != row["revision"] or current["state"] != "READY":
                    raise ProductError("model_binding_changed", 409)
                with record_path.open("x", encoding="utf-8") as output:
                    json.dump(record, output, ensure_ascii=False, separators=(",", ":"))
                    output.flush()
                    os.fsync(output.fileno())
                key = (model_id, row["revision"])
                p.request_counts[key] = p.request_counts.get(key, 0) + 1
                p.arrival_sequence += 1
                record['arrival_sequence'] = p.arrival_sequence
        except FileExistsError as exc:
            raise ProductError("request_id_conflict", 409) from exc

        accepted = dict(path=path, original=original, audio=audio, response_format=response_format,
            model_id=model_id, context=context, limit=limit, choices=choices, payload=payload,
            upstream_stream=upstream_stream, row=row, request_id=request_id,
            record_path=record_path, record=record)
        if getattr(self, 'intake_only', False):
            return accepted
        return self._execute(**accepted)

    def _execute(self, *, path, original, audio, response_format, model_id, context,
                 limit, choices, payload, upstream_stream, row, request_id, record_path,
                 record, ticket=None):
        p = self.server.product
        control = {'cancel': threading.Event(), 'instance': None, 'prepared': False,
                   'association': record.get('association')}
        with p.batch_lock:
            p.active_requests[request_id] = control
            association = record.get('association')
            if association:
                batch = p.batches[association['key']]
                if association['segment'] in batch['failed_segments']:
                    control['cancel'].set()
        cancelled = control['cancel'].is_set

        started = request_sent = headers_sent = disconnected = False
        conn = response = pool = None
        chunks = []
        terminal_tail = bytearray()
        reserved = 0
        prepared = False
        output_body = b""
        instance = None

        def send_headers(status: int, headers: dict[str, str]) -> None:
            nonlocal headers_sent, disconnected
            headers_sent = self._proxy_headers_sent = True
            try:
                self.send_response(status)
                for key, value in headers.items():
                    if key.lower() not in {"connection", "transfer-encoding", "content-length",
                                           "server", "date"}:
                        self.send_header(key, value)
                self.send_header("X-AIToolbox-Request-ID", request_id)
                self.end_headers()
            except OSError:
                disconnected = True

        def forward(block: bytes) -> None:
            nonlocal disconnected
            if not disconnected:
                try:
                    self.wfile.write(block)
                    self.wfile.flush()
                except OSError:
                    disconnected = True

        try:
            current = p.get(model_id)
            if current['revision'] != row['revision'] or current['state'] not in {'READY', 'UPDATING', 'DRAINING'}:
                raise ProductError('model_binding_changed', 409)
            ticket = ticket or p.runtime.placement.enqueue(row, context * choices, record['arrival_sequence'], cancelled)
            instance = p.runtime.acquire(row, context * choices, record['arrival_sequence'], cancelled, ticket)
            started, pool = True, instance.pool
            control['instance'] = instance
            payload = dict(payload, model=instance.name)
            record['instance'] = dict(name=instance.name, budget=instance.budget,
                                      assigned_at=time.time())
            reservation = context * choices
            preparation = p.client.prepare(
                payload, request_id, context, choices, hashlib.sha256(original).hexdigest())
            payload, input_tokens = preparation
            prepared = True
            profile = getattr(preparation, 'info', {}).get('resource_profile')
            if profile:
                p.runtime.placement.observe_profile(instance, profile)
                record['native_resource_profile'] = dict(profile,
                    observed_gpu_total_bytes=p.runtime.placement.last_observation['total_bytes'])
            control['prepared'] = True
            record['prepared_at'] = time.time()
            reservation = context * choices
            record["capacity"] = {"input_tokens": input_tokens, "token_count_margin": 0,
                                  "max_output_tokens": limit,
                                  "context_tokens": context, "reserved_context_tokens": reservation,
                                  "response_choices": choices, "total_context_tokens": pool.total}
            record["capacity"]["input_tokens_source"] = (
                "vllm_prepared_engine_input" if row["backend"] == "cuda13-vllm"
                else "llama_prepared_server_tokens")
            record["native_options"] = {"seed": payload.get("seed"),
                "chat_template_kwargs": json.loads(payload["metadata"]["chat_template_kwargs"])}
            pool.acquire(reservation, record['arrival_sequence'], cancelled, ticket=ticket['pool_ticket'])
            reserved = reservation
            record['admitted_at'] = time.time()
            if cancelled():
                raise ProductError('request_cancelled', 409)
            upstream = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            conn = HTTPConnection("127.0.0.1", p.client.port, timeout=360)
            request_sent = True
            record['execution_started_at'] = time.time()
            with p.lock:
                save_json(record_path, record)
            conn.request("POST", "/v1/chat/completions", upstream, {
                "Authorization": "Bearer " + p.client._key(),
                "Content-Type": "application/json", "Content-Length": str(len(upstream))})
            response = conn.getresponse()
            is_stream = upstream_stream and response.getheader("Content-Type", "").split(";", 1)[0] == "text/event-stream"
            if is_stream:
                send_headers(response.status, dict(response.getheaders()))
            read = response.readline if is_stream else response.read1
            while block := read(64 * 1024):
                chunks.append(block)
                if is_stream:
                    if terminal_tail or block.strip() == b"data: [DONE]":
                        terminal_tail.extend(block)
                    else:
                        forward(block)
            if response.length not in (None, 0):
                raise OSError("localai_response_incomplete")
            upstream_body = b"".join(chunks)
            control['terminal'] = True
            output_body = upstream_body
            if audio and response.status == 200:
                answer = json.loads(upstream_body)
                text = LocalAIProduct._content(answer)
                if not text:
                    raise ProductError("transcription_empty", 502)
                output_body = (text.encode("utf-8") if response_format == "text" else
                               json.dumps({"text": text, "usage": answer.get("usage")},
                                          ensure_ascii=False).encode())
            record["state"] = "COMPLETED" if 200 <= response.status < 300 else "FAILED"
            native = {}
            try:
                native = p.client.execution_control(instance.name, request_id)
                record['native_execution'] = native
                profile = native.get('resource_profile')
                if profile:
                    p.runtime.placement.observe_profile(instance, profile)
                    record['native_resource_profile'] = dict(profile,
                        observed_gpu_total_bytes=p.runtime.placement.last_observation['total_bytes'])
            except Exception as native_error:
                record['native_execution_error'] = str(native_error)
                if record.get('association'):
                    raise
            if record.get('association'):
                if native.get('confirmed_output_hit') or native.get('confirmed_context_hit') or native.get('confirmed_budget_hit'):
                    if native.get('stop_confirmed') is True:
                        record.update(state='FAILED', error='native_limit_reached')
                        record['stop_proof'] = native
                        p.fail_segment(record)
                if cancelled() and native.get('cancel_requested'):
                    record.update(state='CANCELLED' if native.get('stop_confirmed') else 'UNKNOWN',
                                  error='original_segment_failure')
                    record['stop_proof'] = native
            if is_stream and b"data: [DONE]" not in upstream_body:
                record.update(state="UNKNOWN", error="localai_stream_incomplete")
        except Exception as exc:
            record.update(state="UNKNOWN" if request_sent else "FAILED", error=str(exc))
            if cancelled():
                if not request_sent:
                    record.update(state='CANCELLED', stop_proof={'stop_confirmed': True, 'basis': 'not_submitted'})
                else:
                    try:
                        proof = p.client.execution_control(instance.name, request_id, 'cancel')
                    except Exception as stop_error:
                        proof = {'stop_confirmed': False, 'error': str(stop_error)}
                    record['stop_proof'] = proof
                    if proof.get('stop_confirmed') and (proof.get('cancel_requested') or not proof.get('already_finished')):
                        record['state'] = 'CANCELLED'
            if not headers_sent:
                raise
        finally:
            if conn is not None:
                conn.close()
            try:
                if response is not None:
                    upstream_body = b"".join(chunks)
                    if not output_body:
                        output_body = upstream_body
                    record["response"] = {"status": response.status,
                                          "headers": dict(response.getheaders()),
                                          "body_base64": base64.b64encode(output_body).decode()}
                    if audio and response.status == 200:
                        record["response"]["headers"]["Content-Type"] = (
                            "text/plain; charset=utf-8" if response_format == "text" else
                            "application/json; charset=utf-8")
                    record["usage"] = None
                    try:
                        if not upstream_stream:
                            record["usage"] = json.loads(upstream_body).get("usage")
                        else:
                            for line in upstream_body.splitlines():
                                if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
                                    event = json.loads(line[5:])
                                    if isinstance(event.get("usage"), dict):
                                        record["usage"] = event["usage"]
                    except (ValueError, KeyError, TypeError):
                        pass
                    if record["usage"] is None:
                        record["usage_unknown_reason"] = "localai_did_not_report_usage"
                record["updated_at"] = time.time()
                with p.lock:
                    save_json(record_path, record)
            finally:
                try:
                    if prepared:
                        try:
                            p.client.discard(instance.name, request_id)
                        except ProductError:
                            # No replay. Last-user runtime shutdown also drops
                            # backend-local preparations after a failed discard.
                            pass
                finally:
                    try:
                        if reserved:
                            pool.release(reserved)
                        elif instance is not None and ticket.get('pool_ticket'):
                            pool.cancel(ticket['pool_ticket'])
                        elif ticket is not None:
                            p.runtime.placement.cancel(ticket)
                    finally:
                        try:
                            if started:
                                p.runtime.release_instance(instance, context * choices)
                        finally:
                            p.release_request(row)
                            with p.batch_lock:
                                p.active_requests.pop(request_id, None)
        if not headers_sent:
            response_headers = dict(response.getheaders())
            if audio and response.status == 200:
                response_headers["Content-Type"] = ("text/plain; charset=utf-8"
                    if response_format == "text" else "application/json; charset=utf-8")
            send_headers(response.status, response_headers)
            forward(output_body)
        elif record["state"] == "COMPLETED":
            forward(bytes(terminal_tail))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="AIToolbox V11 LocalAI-managed service")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data-service-url", default="http://127.0.0.1:49011")
    parser.add_argument("--data-admin-token-file", type=Path, required=True)
    parser.add_argument("--localai-url", required=True)
    parser.add_argument("--localai-key-file", type=Path, required=True)
    parser.add_argument("--asset-mount", action="append", required=True,
                        help="Windows root=/models/assets-name, repeat per read-only mount")
    parser.add_argument("--port", type=int, default=49088)
    args = parser.parse_args()
    mounts = []
    for item in args.asset_mount:
        if "=" not in item:
            parser.error("--asset-mount requires Windows-root=/models/assets-name")
        host, target = item.split("=", 1)
        mounts.append((Path(host), target))
    client = LocalAIClient(args.localai_url, args.localai_key_file)
    product = LocalAIProduct(args.root, args.data_service_url,
                             args.data_admin_token_file, client, mounts)
    server = Server(("127.0.0.1", args.port), product, V11Handler)
    print(json.dumps({"listening": f"127.0.0.1:{args.port}",
                      "token_file": str(product.token_path),
                      "localai": client.base_url}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
