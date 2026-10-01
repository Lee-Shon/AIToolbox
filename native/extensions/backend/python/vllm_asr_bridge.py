"""Audio decoding and a protocol-only adapter to the shared vLLM execution."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import grpc

import backend_pb2
from vllm.multimodal.media.audio import load_audio


def decode_audio(source):
    value = str(source)
    if len(value) < 512 and Path(value).is_file():
        raw = Path(value).read_bytes()
    else:
        encoded = value.split(",", 1)[1] if value.startswith("data:") else value
        raw = base64.b64decode(encoded, validate=True)
    if not raw or len(raw) > 50 * 1024 * 1024:
        raise ValueError("audio data empty or too large")
    return load_audio(io.BytesIO(raw))


async def audio_transcription(self, request, context):
    """Adapt the legacy RPC without inventing limits absent from its schema.

    The formal HTTP service carries these limits through PredictOptions.Metadata.
    Direct callers of this older RPC must supply the two gRPC metadata values;
    controllers unable to forward them receive an explicit error, not a 256 cap.
    """
    try:
        for field in ("prompt", "language", "translate", "diarize", "timestamp_granularities", "stream"):
            if getattr(request, field, None):
                raise ValueError("unsupported_transcription_parameter:" + field)
        metadata = dict(context.invocation_metadata())
        limit = int(metadata.get("x-aitoolbox-max-tokens", "0"))
        quota = int(metadata.get("x-aitoolbox-context-tokens", "0"))
        if limit <= 0 or quota <= 0:
            raise ValueError("context_and_output_limits_required")
        adapted = backend_pb2.PredictOptions(
            UseTokenizerTemplate=True, Audios=[request.dst], Tokens=limit,
            Temperature=request.temperature, ModelIdentity=request.ModelIdentity,
            Messages=[backend_pb2.Message(role="user", content=
                "Transcribe the speech in this audio verbatim. "
                "Return only the spoken words, no explanation.")],
            Metadata={"aitoolbox_context_tokens": str(quota)})
        stream = self._predict(adapted, context, streaming=False)
        try:
            answer = await anext(stream)
        finally:
            await stream.aclose()
        text = answer.message.decode("utf-8").strip()
        if not text:
            raise ValueError("empty transcription")
        return backend_pb2.TranscriptResult(text=text)
    except ValueError as exc:
        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
