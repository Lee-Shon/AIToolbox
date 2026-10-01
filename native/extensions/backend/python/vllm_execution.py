"""One native preparation for counting and generation, shared by chat and ASR.

Prepared inputs live only in their loaded backend. The gateway's existing quota
pool waits between prepare and execute; discard ends the preparation lifetime.
No generation, retry, durable queue, or second quota pool is hidden in prepare.
"""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import time
from google.protobuf.json_format import MessageToDict

from vllm.renderers.params import TokenizeParams
from vllm.utils import random_uuid


PREFIX = "aitoolbox_"
ADAPTER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass
class PreparedInput:
    engine_input: dict
    prompt: str
    template_kwargs: dict
    input_tokens: int
    fingerprint: str
    field_hashes: dict
    choices: int = 1
    executions: int = 0
    request_id: str = ''
    output_limit: int = 0
    cancelled: bool = False
    native_executions: dict = field(default_factory=dict)


def execution_status(entry):
    executions = list(entry.native_executions.values())
    return {'request_id': entry.request_id, 'native_executions': executions,
            'execution_started': bool(executions),
            'stop_confirmed': (entry.cancelled or len(executions) == entry.choices)
                              and all(item['finished'] for item in executions),
            'confirmed_output_hit': any(item.get('finish_reason') == 'length'
                and item.get('output_tokens', 0) >= entry.output_limit for item in executions),
            'cancel_requested': entry.cancelled,
            'basis': 'vllm_engine_output_and_ordered_abort_worker_barrier'}


class AIToolboxWorkerExtension:
    """Named, typed RPCs; vLLM's safe serializer never receives Python code."""
    def aitoolbox_gpu_barrier(self):
        import torch
        torch.cuda.synchronize()
        return {'device_barrier_completed': True}

    def aitoolbox_memory_profile(self):
        import torch
        torch.cuda.synchronize()
        # Full native startup profile includes activation at the registered T.
        # Keep that headroom for later media/generation; a short input must not
        # make the placement forecast smaller than the full native profile.
        full_profile = int(getattr(self, 'requested_memory', 0)) + int(getattr(self, 'peak_activation_memory', 0))
        # Native startup consumed memory includes CUDA allocations outside
        # PyTorch. The excess over native weight memory also includes any
        # persistent activation; retaining it is a conservative ownership bound.
        extra = max(0, int(self.total_consumed) - int(self.model_runner.model_memory_usage))
        return dict(resident_allocator_bytes=max(torch.cuda.memory_reserved() + extra, full_profile),
                    peak_allocator_bytes=max(torch.cuda.max_memory_reserved() + extra, full_profile),
                    persistent_non_weight_bytes=extra, full_context_profile_bytes=full_profile)


async def resource_profile(backend):
    workers = await backend.llm.engine_core.collective_rpc_async('aitoolbox_memory_profile', timeout=30)
    if len(workers) != 1:
        raise ValueError('native_resource_geometry_unsupported')
    # WSL device observations include CUDA/driver working space beyond the
    # allocator profile. Keep an explicit 3 GiB envelope for startup and media.
    headroom = 3 * 1024 ** 3
    worker = workers[0]
    previous = getattr(backend, '_ait_resource_profile', {})
    # Media encoder allocations can peak during generation, after prepare.
    # Carry that observed maximum forward to every subsequent full-T instance.
    peak = max(int(worker['resident_allocator_bytes']), int(worker['peak_allocator_bytes'])) + headroom
    profile = dict(
        resident_gpu_bytes=max(peak, previous.get('resident_gpu_bytes', 0)),
        load_gpu_bytes=max(peak, previous.get('load_gpu_bytes', 0)),
        driver_workspace_headroom_bytes=headroom,
        persistent_non_weight_bytes=worker.get('persistent_non_weight_bytes', 0),
        full_context_profile_bytes=worker.get('full_context_profile_bytes', 0),
        total_context_tokens=backend.llm.model_config.max_model_len,
        precision=str(backend.llm.model_config.dtype),
        adapter_sha256=ADAPTER_SHA256,
        source='vllm_native_worker_allocator_with_driver_headroom')
    backend._ait_resource_profile = profile
    return profile


def fingerprint_fields(request):
    value = type(request)()
    value.CopyFrom(request)
    value.CorrelationId = ""
    value.Metadata.pop(PREFIX + "phase", None)
    value.Metadata["chat_template_kwargs"] = json.dumps(
        json.loads(value.Metadata.get("chat_template_kwargs", "{}")), sort_keys=True)
    # Staged media paths can differ between the two HTTP calls. The gateway
    # supplies a digest of the complete original request, including its media.
    if value.Metadata.get(PREFIX + "input_digest"):
        del value.Images[:]
        del value.Audios[:]
        del value.Videos[:]
    fields = MessageToDict(value, preserving_proto_field_name=True)
    metadata = fields.pop("Metadata", {})
    fields.update({"Metadata." + key: item for key, item in metadata.items()})
    return {key: hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
            for key, item in fields.items()}


def fingerprint(request):
    return hashlib.sha256(json.dumps(fingerprint_fields(request), sort_keys=True).encode()).hexdigest()


def _messages(backend, request, counts):
    messages = backend._messages_to_dicts(request.Messages)
    layout = request.Metadata.get(PREFIX + "media_layout")
    if layout:
        layout = json.loads(layout)
        if len(layout) != len(messages):
            raise ValueError("invalid_media_layout")
        actual = {"image": 0, "audio": 0, "video": 0}
        for message, parts in zip(messages, layout):
            if parts is None:
                continue
            content = []
            for part in parts:
                kind = part.get("type")
                if kind == "text":
                    content.append({"type": "text", "text": part["text"]})
                elif kind in actual:
                    actual[kind] += 1
                    content.append({"type": kind})
                else:
                    raise ValueError("invalid_media_layout")
            message["content"] = content
        if actual != counts:
            raise ValueError("media_layout_count_mismatch")
        return messages
    # LocalAI's flat RPC omits media/message positions. A single user message
    # is unambiguous (including the ASR adapter); do not guess for multi-turn.
    if any(counts.values()):
        users = [m for m in messages if m["role"] == "user"]
        if len(users) != 1:
            raise ValueError("media_layout_required_for_multiturn")
        message = users[0]
        message["content"] = [
            {"type": kind} for kind, count in counts.items() for _ in range(count)
        ] + [{"type": "text", "text": message.get("content") or ""}]
    return messages


async def prepare_input(backend, request):
    from vllm_asr_bridge import decode_audio

    images = [backend.load_image(path) for path in request.Images]
    videos = [backend.load_video(path) for path in request.Videos]
    audios = [decode_audio(path) for path in request.Audios]
    data = {kind: values for kind, values in
            (("image", images), ("video", videos), ("audio", audios)) if values}
    prompt = request.Prompt
    kwargs = {}
    if not prompt and request.UseTokenizerTemplate and request.Messages:
        kwargs = json.loads(request.Metadata.get("chat_template_kwargs", "{}"))
        kwargs.update(tokenize=False, add_generation_prompt=True)
        if request.Tools:
            kwargs["tools"] = json.loads(request.Tools)
        thinking = request.Metadata.get("enable_thinking", "").lower()
        if thinking in ("true", "false"):
            kwargs["enable_thinking"] = thinking == "true"
        messages = _messages(backend, request, {
            "image": len(images), "video": len(videos), "audio": len(audios)})
        # Never retry with a text-only template: that silently drops media or
        # tools and prepares a different request from the one being counted.
        prompt = backend.tokenizer.apply_chat_template(messages, **kwargs)
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("native_prompt_required")
    raw = {"prompt": prompt}
    if data:
        raw["multi_modal_data"] = data
    rendered = await backend.llm.renderer.render_cmpl_async(
        [raw], TokenizeParams(max_total_tokens=None, add_special_tokens=not bool(kwargs)),
        # Preparation may wait or be rejected without ever reaching EngineCore.
        # Keep its media self-contained instead of advancing a sender cache
        # whose receiver has not seen the corresponding features yet.
        skip_mm_cache=True)
    engine_input = rendered[0]
    tokens = engine_input.get("prompt_token_ids")
    if not isinstance(tokens, list) or "encoder_prompt_token_ids" in engine_input:
        raise ValueError("native_context_dimension_unsupported")
    limit = int(request.Tokens)
    quota = int(request.Metadata.get(PREFIX + "context_tokens") or
                backend.llm.model_config.max_model_len)
    if limit <= 0 or quota <= 0:
        raise ValueError("context_and_output_limits_required")
    if quota > backend.llm.model_config.max_model_len:
        raise ValueError("request_exceeds_instance_capacity")
    if len(tokens) + limit > quota:
        raise ValueError(f"requested_context_insufficient:input={len(tokens)},output={limit},context={quota}")
    choices = int(request.Metadata.get(PREFIX + "choices", "1"))
    if choices <= 0:
        raise ValueError("invalid_response_choices")
    return PreparedInput(engine_input, prompt, kwargs, len(tokens),
                         fingerprint(request), fingerprint_fields(request), choices)


async def resolve_input(backend, request):
    phase = request.Metadata.get(PREFIX + "phase", "")
    key = request.Metadata.get(PREFIX + "request_id", "")
    if not hasattr(backend, "_prepared_inputs"):
        backend._prepared_inputs = {}
    cache = backend._prepared_inputs
    if not phase:
        return await prepare_input(backend, request), None
    if not key or phase not in {"prepare", "execute", "discard", "status", "cancel"}:
        raise ValueError("invalid_preparation_operation")
    if phase == "discard":
        cache.pop(key, None)
        return None, {"aitoolbox_discarded": 1}
    entry = cache.get(key)
    if phase in {'status', 'cancel'}:
        if entry is None:
            raise ValueError('prepared_input_missing')
        if phase == 'cancel':
            if (entry.executions == entry.choices and len(entry.native_executions) == entry.choices
                    and all(item['finished'] for item in entry.native_executions.values())):
                return None, dict(execution_status(entry), already_finished=True)
            entry.cancelled = True
            ids = [item['native_request_id'] for item in entry.native_executions.values() if not item['finished']]
            if ids:
                # Both messages travel through the same EngineCore input queue.
                # Its ABORT handler removes only these scheduler requests;
                # the following worker barrier confirms prior GPU work ended.
                await backend.llm.abort(ids)
                proof = await backend.llm.engine_core.collective_rpc_async('aitoolbox_gpu_barrier', timeout=30)
                if not proof or not all(item.get('device_barrier_completed') for item in proof):
                    raise ValueError('native_stop_unconfirmed')
                for item in entry.native_executions.values():
                    if item['native_request_id'] in ids:
                        item.update(finished=True, finish_reason='abort', stopped_at=time.time(),
                                    engine_abort_confirmed=True)
        status = execution_status(entry)
        status['native_context_tokens'] = backend.llm.model_config.max_model_len
        status['precision'] = str(backend.llm.model_config.dtype)
        if phase == 'status' and status['stop_confirmed']:
            status['resource_profile'] = await resource_profile(backend)
        return None, status
    if entry is not None and entry.fingerprint != fingerprint(request):
        fields = fingerprint_fields(request)
        changed = sorted(k for k in entry.field_hashes.keys() | fields.keys()
                         if entry.field_hashes.get(k) != fields.get(k))
        raise ValueError("prepared_input_changed:" + ",".join(changed))
    if phase == "prepare":
        if entry is None:
            entry = await prepare_input(backend, request)
            entry.request_id = key
            entry.output_limit = int(request.Tokens)
            # An overlapping duplicate cannot replace an already prepared input.
            if key in cache:
                raise ValueError("preparation_id_conflict")
            cache[key] = entry
        if not hasattr(backend, '_ait_resource_profile'):
            await resource_profile(backend)
        return entry, {"aitoolbox_prepared": 1, "input_tokens": entry.input_tokens,
                       "resource_profile": backend._ait_resource_profile,
                       "native_template_kwargs": json.loads(
                           request.Metadata.get("chat_template_kwargs", "{}"))}
    if entry is None:
        raise ValueError("prepared_input_missing")
    if entry.cancelled:
        raise ValueError('prepared_input_cancelled')
    if entry.executions >= entry.choices:
        raise ValueError("prepared_input_already_executed")
    entry.executions += 1
    return entry, None


async def generate_prepared(backend, entry, sampling_params):
    # EngineInput is already tokenized and multimodal-processed. AsyncLLM
    # explicitly accepts it without rendering or applying the template again.
    native_id = 'ait-' + (entry.request_id or random_uuid()) + '-' + str(entry.executions)
    status = {'native_request_id': native_id, 'started_at': time.time(), 'finished': False,
              'output_tokens': 0, 'finish_reason': None}
    entry.native_executions[native_id] = status
    outputs = backend.llm.generate(entry.engine_input, sampling_params=sampling_params,
                                   request_id=native_id)
    try:
        async for output in outputs:
            for item in output.outputs:
                status['output_tokens'] = max(status['output_tokens'], len(item.token_ids))
                status['partial_text'] = item.text
                if item.finish_reason:
                    status['finish_reason'] = item.finish_reason
            if output.finished:
                status.update(finished=True, stopped_at=time.time())
            yield output
    finally:
        await outputs.aclose()
