"""Small authenticated local model registry backed by an installed llama-server.

The registry is the binding authority for this independent V10 product service.
The existing V9 scheduler and cloud relay are never modified by this service.
"""

from __future__ import annotations

import base64
import ctypes
from email import policy
from email.parser import BytesParser
from datetime import datetime
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlsplit
import uuid
import zlib

from .ownership import ProcessJob


CAPABILITIES = {"text_input", "image_input", "audio_input", "text_output"}
VERSION = "10.3.0"
# Execution headroom only. SharedContext gates every generation by the caller's
# declared quota; this is not a model-registration or per-request context limit.
NATIVE_SLOTS = 32
REQUIRED = {"text_input", "text_output"}
MAX_BODY = 32 * 1024 * 1024
RETRYABLE_REGISTRATION_ERRORS = {
    "runtime_load_timeout",
    "system_memory_status_unavailable", "runtime_memory_estimate_failed",
    "insufficient_free_system_memory", "audio_validation_requires_windows_english_speech_voice",
    "validation_interrupted_by_restart", "service_stopping", "validation_worker_start_failed",
}


class ProductError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            h.update(block)
    return h.hexdigest()


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


def red_png() -> bytes:
    width = height = 32
    raw = b"".join(b"\0" + b"\xff\0\0" * width for _ in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        import struct
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    import struct
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class SharedContext:
    """Use V9's shared-KV rule: caller-declared context reservations, not equal slices."""
    def __init__(self, total: int):
        self.total = total
        self.tokens = 0
        self.condition = threading.Condition()

    def acquire(self, tokens: int) -> None:
        if not 0 < tokens <= self.total:
            raise ProductError("request_exceeds_instance_capacity", 422)
        with self.condition:
            while self.tokens + tokens > self.total:
                self.condition.wait()
            self.tokens += tokens

    def release(self, tokens: int) -> None:
        with self.condition:
            self.tokens -= tokens
            self.condition.notify_all()


class Runtime:
    def __init__(self, executable: Path, root: Path):
        self.executable = executable.resolve(strict=True)
        self.root = root
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self.model_id: str | None = None
        self.binding: tuple | None = None
        self.capacity: dict = {}
        self.pool: SharedContext | None = None
        self.log: io.BufferedWriter | None = None
        self.lifecycle = threading.RLock()
        self.lock = threading.RLock()
        self.inflight = 0
        self.job: ProcessJob | None = None

    def stop(self) -> None:
        with self.lifecycle:
            while True:
                with self.lock:
                    if not self.inflight:
                        break
                time.sleep(0.1)
            with self.lock:
                if self.job is not None:
                    self.job.close()
                    self.job = None
                if self.process is not None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=10)
                if self.log is not None:
                    self.log.close()
                self.process = self.log = None
                self.port = None
                self.model_id = None
                self.binding = None
                self.capacity = {}
                self.pool = None

    def release(self, model_id: str, started: bool) -> None:
        # Drain waiters hold lifecycle while waiting for inflight. Decrement
        # before taking that lock, or the last completion would deadlock.
        with self.lock:
            if started:
                self.inflight -= 1
            if self.inflight:
                return
        with self.lifecycle:
            with self.lock:
                # A waiting switch/new call may have acquired the runtime first.
                if not self.inflight and self.model_id == model_id:
                    self.stop()

    def _check_memory(self, row: dict, total_context: int, parallel: int) -> None:
        estimator = self.executable.with_name("llama-fit-params.exe")
        if not estimator.is_file():
            raise ProductError("runtime_memory_estimator_unavailable", 422)
        estimate = subprocess.run([str(estimator), "--model", row["model_path"],
            "--ctx-size", str(total_context), "--parallel", str(parallel),
            "--gpu-layers", "0", "--fit", "off", "--fit-print", "on"],
            capture_output=True, text=True, timeout=60, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        host = re.findall(r"^Host\s+(\d+)\s+(\d+)\s+(\d+)\s*$", estimate.stdout, re.M)
        if estimate.returncode or len(host) != 1:
            raise ProductError("runtime_memory_estimate_failed", 422)
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                        ("extended", ctypes.c_ulonglong)]
        memory = MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            raise ProductError("system_memory_status_unavailable", 422)
        projector = row["assets"].get("mmproj_path", {}).get("size", 0)
        # The estimator includes weights, KV and compute. Leave a separate
        # projector allowance and 1 GiB of system headroom.
        required = (sum(map(int, host[0])) + (projector + (1 << 20) - 1) // (1 << 20) + 1024) << 20
        used = memory.total_physical - memory.available_physical
        available = min(memory.available_physical, memory.total_physical * 90 // 100 - used)
        if required > available:
            raise ProductError("insufficient_free_system_memory", 422)

    def start(self, row: dict) -> int:
        with self.lifecycle:
            try:
                return self._start(row)
            except BaseException:
                # Covers Popen, malformed health/capacity responses and I/O
                # failures as well as the expected load/validation errors.
                self.stop()
                raise

    def _start(self, row: dict) -> int:
        with self.lifecycle:
            binding = (row["id"], row["revision"], row["max_context_tokens"])
            if self.binding == binding and self.process is not None and self.process.poll() is None:
                return int(self.port)
            self.stop()
            for field in ("model_path", "mmproj_path"):
                item = row.get(field)
                if item:
                    path = Path(item)
                    if (not path.is_file() or path.stat().st_size != row["assets"][field]["size"]
                            or path.stat().st_mtime_ns != row["assets"][field]["mtime_ns"]
                            or digest(path) != row["assets"][field]["sha256"]):
                        raise ProductError("model_asset_changed:" + field, 409)
            total_context = row["max_context_tokens"]
            self._check_memory(row, total_context, NATIVE_SLOTS)
            main = Path(row["model_path"])
            mmproj = row.get("mmproj_path")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            command = [str(self.executable), "--model", str(main), "--alias", row["id"],
                       "--ctx-size", str(total_context), "--gpu-layers", "0",
                       "--threads", str(min(os.cpu_count() or 4, 8)),
                       "--parallel", str(NATIVE_SLOTS), "--host", "127.0.0.1", "--port", str(port),
                       "--no-webui", "--slots", "--fit", "off", "--no-context-shift", "--cont-batching",
                       "--kv-unified"]
            if mmproj:
                command.extend(["--mmproj", mmproj])
            log_path = self.root / "runtime" / (row["id"] + "-r" + str(row["revision"]) + ".log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = log_path.open("ab")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.job = ProcessJob()
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=self.log,
                                            stderr=subprocess.STDOUT, creationflags=flags)
            self.job.assign(self.process)
            self.port, self.model_id, self.binding = port, row["id"], binding
            until = time.monotonic() + 240
            while time.monotonic() < until:
                if self.process.poll() is not None:
                    self.stop()
                    raise ProductError("runtime_exited_during_load", 422)
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                        if response.status == 200:
                            with urlopen(f"http://127.0.0.1:{port}/slots", timeout=5) as slots_response:
                                slots = json.load(slots_response)
                            with urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5) as models_response:
                                models = json.load(models_response)
                            data = models.get("data", []) if isinstance(models, dict) else []
                            train_context = data[0].get("meta", {}).get("n_ctx_train") if data else None
                            if (not isinstance(slots, list) or len(slots) != NATIVE_SLOTS
                                    or type(train_context) is not int or train_context <= 0
                                    or any(not isinstance(s, dict) or type(s.get("n_ctx")) is not int
                                           or s["n_ctx"] < min(total_context, train_context) for s in slots)):
                                self.stop()
                                raise ProductError("runtime_capacity_mismatch", 422)
                            self.capacity = {"native_slots": len(slots), "total_context_tokens": total_context,
                                             "shared_context": True,
                                             "native_context_tokens": min(s["n_ctx"] for s in slots)}
                            self.pool = SharedContext(total_context)
                            return port
                except (HTTPError, URLError, TimeoutError):
                    pass
                time.sleep(1)
            self.stop()
            raise ProductError("runtime_load_timeout", 422)


def post_json(port: int, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=data,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=120) as response:
            return json.load(response)
    except HTTPError as exc:
        raise ProductError("native_chat_failed:" + exc.read(500).decode(errors="replace"), 422) from exc


def output_text(result: dict) -> str:
    return str(result.get("choices", [{}])[0].get("message", {}).get("content") or "")


def speech_probe() -> bytes:
    """Generate our own short sentence locally; no recorded audio is shipped."""
    script = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $voice = $speaker.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'en-*' } | Select-Object -First 1
  if ($null -eq $voice) { throw 'english_speech_voice_missing' }
  $speaker.SelectVoice($voice.VoiceInfo.Name)
  $speaker.SetOutputToWaveFile($env:AITOOLBOX_PROBE_AUDIO)
  $speaker.Speak('The blue sky has seven bright stars.')
} finally { $speaker.Dispose() }
"""
    with tempfile.TemporaryDirectory(prefix="aitoolbox-probe-") as folder:
        target = Path(folder) / "probe.wav"
        env = dict(os.environ, AITOOLBOX_PROBE_AUDIO=str(target))
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                                env=env, capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not target.is_file():
            raise ProductError("audio_validation_requires_windows_english_speech_voice", 422)
        return target.read_bytes()


def probe(port: int, row: dict) -> dict:
    checked: dict[str, Any] = {}
    model = row["id"]
    text_result = post_json(port, {"model": model, "messages": [{"role": "user", "content": "Reply with the word ready."}],
                                   "max_tokens": 64, "temperature": 0,
                                   "chat_template_kwargs": {"enable_thinking": False}})
    if not output_text(text_result).strip():
        raise ProductError("text_probe_empty", 422)
    checked["text_input"] = {"result_id": text_result.get("id"), "content": output_text(text_result)[:120]}
    checked["text_output"] = checked["text_input"]
    if "image_input" in row["capabilities"]:
        picture = "data:image/png;base64," + base64.b64encode(red_png()).decode("ascii")
        image_result = post_json(port, {"model": model, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "What is the main color of this image? Answer with one color."},
            {"type": "image_url", "image_url": {"url": picture}}]}], "max_tokens": 64, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}})
        answer = output_text(image_result)
        if "red" not in answer.lower() and "红" not in answer:
            raise ProductError("image_probe_wrong_answer:" + answer[:100], 422)
        checked["image_input"] = {"result_id": image_result.get("id"), "content": answer[:120]}
    if "audio_input" in row["capabilities"]:
        boundary = "aitoolbox" + secrets.token_hex(8)
        wav = speech_probe()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"probe.wav\"\r\n"
                "Content-Type: audio/wav\r\n\r\n").encode() + wav + f"\r\n--{boundary}--\r\n".encode()
        req = Request(f"http://127.0.0.1:{port}/v1/audio/transcriptions", data=body,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urlopen(req, timeout=180) as response:
                audio_result = json.load(response)
        except HTTPError as exc:
            raise ProductError("native_audio_failed:" + exc.read(500).decode(errors="replace"), 422) from exc
        transcript = str(audio_result.get("text") or "")
        if "blue" not in transcript.lower() or not any(word in transcript.lower() for word in ("seven", "7")):
            raise ProductError("audio_probe_wrong_transcript:" + transcript[:120], 422)
        checked["audio_input"] = {"content": transcript[:220]}
    return checked


class Product:
    def __init__(self, root: Path, executable: Path, data_service_url: str,
                 data_admin_token_file: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        parsed = urlsplit(data_service_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.path not in {"", "/"} or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("data_service_requires_loopback")
        self.data_service_url = data_service_url.rstrip("/")
        self.data_admin_token_file = data_admin_token_file.resolve(strict=True)
        self.registry_path = self.root / "registry.json"
        self.token_path = self.root / "api-token.txt"
        if not self.token_path.exists():
            self.token_path.write_text(secrets.token_urlsafe(40), encoding="ascii")
        self.token = self.token_path.read_text(encoding="ascii").strip()
        if len(self.token) < 32:
            raise ValueError("invalid_product_token")
        self.lock = threading.RLock()
        self.idle = threading.Condition(self.lock)
        self.request_counts: dict[tuple[str, int], int] = {}
        self.closing = False
        self.workers: set[threading.Thread] = set()
        self.runtime = Runtime(executable, root)
        self.rows = json.loads(self.registry_path.read_text(encoding="utf-8")) if self.registry_path.exists() else {}
        # A restarted relay cannot prove what an interrupted native call did.
        # Keep its identity and input, and never replay it automatically.
        for receipt in (self.root / "requests").glob("*.json"):
            record = json.loads(receipt.read_text(encoding="utf-8"))
            if record.get("state") == "PENDING":
                record.update(state="UNKNOWN", error="service_restarted_during_request",
                              updated_at=time.time())
                save_json(receipt, record)
        for row in self.rows.values():
            if row["state"] == "UPDATING":
                pending = row.pop("pending_update")
                row.update(state=pending["previous_state"], update_error="update_interrupted_by_restart",
                           last_update={"state": "REJECTED", "revision": pending["revision"],
                                        "error": "update_interrupted_by_restart"})
            # READY records capability evidence, not a resident native process.
            # Restarting the API must not launch native work by itself.
            if row["state"] == "VALIDATING":
                row.update(state="REJECTED", error="validation_interrupted_by_restart",
                           updated_at=time.time())
        self._save()

    def _launch(self, target, args) -> None:
        # Caller holds the registry lock, making acceptance and worker tracking
        # atomic with close(). Validation itself must not hold that lock.
        def work():
            try:
                target(*args)
            finally:
                with self.idle:
                    self.workers.discard(worker)
                    self.idle.notify_all()
        worker = threading.Thread(target=work, daemon=True)
        self.workers.add(worker)
        try:
            worker.start()
        except BaseException:
            self.workers.discard(worker)
            raise

    def close(self) -> None:
        with self.idle:
            self.closing = True
            while self.request_counts or self.workers:
                self.idle.wait()
        self.runtime.stop()

    def _save(self) -> None:
        save_json(self.registry_path, self.rows)

    @staticmethod
    def settings(context: Any) -> int:
        if type(context) is not int or not 512 <= context <= 2147483647:
            raise ProductError("invalid_max_context_tokens")
        return context

    @classmethod
    def registration_spec(cls, body: dict) -> dict:
        model_id = body.get("id")
        if (not isinstance(model_id, str) or not model_id or not model_id.isascii()
                or model_id in {".", ".."} or len(model_id) > 80
                or not all(c.isalnum() or c in "-_." for c in model_id)):
            raise ProductError("invalid_model_id")
        context = cls.settings(body.get("max_context_tokens"))
        if "parallel" in body:
            raise ProductError("parallel_is_not_a_registration_setting")
        caps = body.get("capabilities")
        if (not isinstance(caps, list) or not all(isinstance(item, str) for item in caps)
                or len(set(caps)) != len(caps) or not REQUIRED.issubset(caps) or not set(caps) <= CAPABILITIES):
            raise ProductError("invalid_capabilities")
        main = body.get("model_path")
        mmproj = body.get("mmproj_path")
        if not isinstance(main, str) or not Path(main).is_absolute():
            raise ProductError("model_path_absolute_required")
        if not Path(main).is_file():
            raise ProductError("model_file_missing")
        if Path(main).suffix.lower() != ".gguf":
            raise ProductError("unsupported_format:llama.cpp_requires_gguf", 422)
        if {"image_input", "audio_input"} & set(caps):
            if not isinstance(mmproj, str) or not Path(mmproj).is_absolute() or not Path(mmproj).is_file():
                raise ProductError("multimodal_projector_required")
        elif mmproj is not None and (not isinstance(mmproj, str) or not Path(mmproj).is_absolute()
                                     or not Path(mmproj).is_file()):
            raise ProductError("multimodal_projector_missing")
        return {"id": model_id, "model_path": str(Path(main).resolve()),
                "mmproj_path": str(Path(mmproj).resolve()) if mmproj else None,
                "capabilities": sorted(caps), "max_context_tokens": context}

    def register(self, body: dict) -> tuple[int, dict]:
        spec = self.registration_spec(body)
        model_id = spec["id"]
        with self.lock:
            if self.closing:
                raise ProductError("service_stopping", 503)
            prior = self.rows.get(model_id)
            if prior and prior["state"] != "REMOVED":
                if prior["state"] in {"UPDATING", "DRAINING"}:
                    raise ProductError("model_busy", 409)
                if prior["spec"] != spec:
                    raise ProductError("model_id_conflict", 409)
                if (prior["state"] != "REJECTED"
                        or prior.get("error") not in RETRYABLE_REGISTRATION_ERRORS):
                    return 200, prior.copy()
                # An explicit retry after a resource failure keeps the binding.
                revision = prior["revision"]
            else:
                revision = prior["revision"] + 1 if prior else 1
            row = {**spec, "spec": spec, "revision": revision, "state": "VALIDATING",
                   "assets": {}, "checked": {}, "error": None, "updated_at": time.time()}
            self.rows[model_id] = row
            self._save()
            try:
                self._launch(self._validate, (model_id,))
            except Exception as exc:
                row.update(state="REJECTED", error="validation_worker_start_failed")
                self._save()
                raise ProductError("validation_worker_start_failed", 503) from exc
        return 202, row.copy()

    def _validate(self, model_id: str) -> None:
        with self.lock:
            row = self.rows[model_id].copy()
        result = self._validate_row(row)
        with self.lock:
            current = self.rows.get(model_id)
            if current and current["revision"] == row["revision"] and current["state"] == "VALIDATING":
                current.update(result)
                self._save()

    def _validate_row(self, row: dict) -> dict:
        try:
            assets = {}
            for field in ("model_path", "mmproj_path"):
                if row.get(field):
                    path = Path(row[field])
                    assets[field] = {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns,
                                     "sha256": digest(path)}
            row["assets"] = assets
            with self.runtime.lifecycle:
                try:
                    if self.closing:
                        raise ProductError("service_stopping", 503)
                    self.runtime.start(row)
                    checked = probe(int(self.runtime.port), row)
                    if set(row["capabilities"]) != set(checked):
                        raise ProductError("capability_probe_incomplete", 422)
                    row["runtime_shape"] = dict(getattr(self.runtime, "capacity", {}))
                finally:
                    # Keep ownership until cleanup is complete: another model
                    # must not start between our probe and our stop operation.
                    if self.runtime.model_id == row["id"]:
                        self.runtime.stop()
            state, error = "READY", None
        except Exception as exc:
            state, error = "REJECTED", str(exc)
        row.update(state=state, error=error, checked=checked if state == "READY" else {}, updated_at=time.time())
        return row

    def update(self, model_id: str, body: dict) -> tuple[int, dict]:
        fields = {"max_context_tokens", "model_path", "mmproj_path", "capabilities"}
        if not isinstance(body, dict) or not fields.intersection(body) or set(body) - fields - {"expected_revision"}:
            raise ProductError("invalid_model_update")
        with self.lock:
            if self.closing:
                raise ProductError("service_stopping", 503)
            row = self.rows.get(model_id)
            if row is None or row["state"] == "REMOVED":
                raise ProductError("model_not_found", 404)
            expected = body.get("expected_revision", row["revision"])
            if type(expected) is not int or expected != row["revision"]:
                raise ProductError("model_revision_conflict", 409)
            if row["state"] not in {"READY", "REJECTED", "UPDATING"}:
                raise ProductError("model_busy", 409)
            spec = self.registration_spec({**row["spec"], **{key: body[key] for key in fields if key in body}})
            if row["state"] == "UPDATING":
                if row["pending_update"]["spec"] == spec:
                    return 202, row.copy()
                raise ProductError("model_update_in_progress", 409)
            if row["spec"] == spec:
                if row.get("update_error"):
                    row["update_error"] = None
                    self._save()
                return 200, row.copy()
            revision = max(row["revision"], row.get("last_update", {}).get("revision", 0)) + 1
            row["pending_update"] = {"spec": spec, "revision": revision, "previous_state": row["state"]}
            row.update(state="UPDATING", update_error=None, updated_at=time.time())
            self._save()
            try:
                self._launch(self._apply_update, (model_id, revision))
            except Exception as exc:
                pending = row.pop("pending_update")
                row.update(state=pending["previous_state"], update_error="validation_worker_start_failed",
                           last_update={"state": "REJECTED", "revision": revision,
                                        "error": "validation_worker_start_failed"})
                self._save()
                raise ProductError("validation_worker_start_failed", 503) from exc
        return 202, self.get(model_id)

    def _apply_update(self, model_id: str, revision: int) -> None:
        with self.idle:
            old = self.rows[model_id]
            while self.request_counts.get((model_id, old["revision"]), 0):
                self.idle.wait()
            pending = old["pending_update"]
            candidate = {**pending["spec"], "spec": pending["spec"], "revision": revision,
                         "state": "VALIDATING", "assets": {}, "checked": {}, "error": None}
        result = self._validate_row(candidate)
        with self.lock:
            if self.rows.get(model_id) is not old or old.get("pending_update", {}).get("revision") != revision:
                return
            outcome = {"state": result["state"], "revision": revision, "error": result["error"],
                       "max_context_tokens": candidate["max_context_tokens"]}
            if result["state"] == "READY":
                result["last_update"] = outcome
                self.rows[model_id] = result
            else:
                old.update(state=pending["previous_state"], update_error=result["error"], last_update=outcome,
                           updated_at=time.time())
                old.pop("pending_update", None)
            self._save()

    def release_request(self, row: dict) -> None:
        with self.idle:
            key = (row["id"], row["revision"])
            self.request_counts[key] -= 1
            if not self.request_counts[key]:
                del self.request_counts[key]
            self.idle.notify_all()

    def unregister(self, model_id: str) -> dict:
        with self.lock:
            row = self.rows.get(model_id)
            if not row:
                raise ProductError("model_not_found", 404)
            if row["state"] == "REMOVED":
                return row.copy()
            if row["state"] in {"VALIDATING", "UPDATING"}:
                raise ProductError("model_validation_in_progress", 409)
            row["state"] = "DRAINING"
            self._save()
        with self.runtime.lifecycle:
            with self.lock:
                current = self.rows.get(model_id)
                # A concurrent cancellation may have finished and a new
                # binding may already exist while we waited for the runtime.
                if current is not row or row["state"] == "REMOVED":
                    return row.copy()
            if self.runtime.model_id == model_id:
                self.runtime.stop()
            with self.lock:
                row["state"] = "REMOVED"
                row["updated_at"] = time.time()
                self._save()
                return row.copy()

    def get(self, model_id: str) -> dict:
        with self.lock:
            if model_id not in self.rows:
                raise ProductError("model_not_found", 404)
            return self.rows[model_id].copy()

    def list_ready(self) -> list[dict]:
        with self.lock:
            return [{"id": row["id"], "object": "model", "owned_by": "aitoolbox-v10-local",
                     "capabilities": row["capabilities"], "max_context_tokens": row["max_context_tokens"],
                     "runtime_shape": row.get("runtime_shape"),
                     "request_context_required": True, "request_context_parameter": "context_tokens",
                     "revision": row["revision"]} for row in self.rows.values() if row["state"] == "READY"]

    def list_all(self) -> list[dict]:
        with self.lock:
            return [{"id": row["id"], "state": row["state"], "error": row.get("error"),
                     "model_path": row["model_path"], "mmproj_path": row.get("mmproj_path"),
                     "capabilities": row["capabilities"], "max_context_tokens": row["max_context_tokens"],
                     "pending_update": row.get("pending_update"),
                     "update_error": row.get("update_error"),
                     "revision": row["revision"]} for row in self.rows.values()]

    def usage_day(self, day: str) -> list[dict]:
        return self.database_usage_day(day)["local"]

    def database_usage_day(self, day: str) -> dict:
        try:
            if datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                raise ValueError
        except ValueError as exc:
            raise ProductError("invalid_usage_day") from exc
        try:
            token = self.data_admin_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProductError("usage_database_credential_unavailable", 503) from exc
        if len(token) < 32:
            raise ProductError("usage_database_credential_invalid", 503)
        payload = json.dumps({"day_cst": day}).encode("utf-8")
        result = {}
        for name, route in (("cloud", "/v1/data/daily-usage"),
                            ("local", "/v1/data/local-product-usage")):
            request = Request(self.data_service_url + route, data=payload, method="POST",
                              headers={"Authorization": "Bearer " + token,
                                       "Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=20) as response:
                    answer = json.load(response)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                raise ProductError("usage_database_unavailable", 503) from exc
            rows = answer.get("result") if isinstance(answer, dict) else None
            if not isinstance(rows, list):
                raise ProductError("usage_database_response_invalid", 503)
            result[name] = rows
        return {"day": day, **result}


class Handler(BaseHTTPRequestHandler):
    server: "Server"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, value: Any, headers: dict | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "request_id", None):
            self.send_header("X-AIToolbox-Request-ID", self.request_id)
        for key, val in (headers or {}).items():
            self.send_header(key, str(val))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> None:
        provided = self.headers.get("Authorization", "")
        if not secrets.compare_digest(provided, "Bearer " + self.server.product.token):
            raise ProductError("invalid_local_token", 401)

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ProductError("invalid_content_length") from exc
        if not 0 < length <= MAX_BODY:
            raise ProductError("invalid_body_size", 413)
        return self.rfile.read(length)

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health" and self.command == "GET":
            return self._send(200, {"status": "ok", "component": "v10-local-product", "version": VERSION})
        self._auth()
        p = self.server.product
        if path == "/v1/models" and self.command == "GET":
            return self._send(200, {"object": "list", "data": p.list_ready()})
        if path == "/admin/models" and self.command == "GET":
            return self._send(200, {"object": "list", "data": p.list_all()})
        if path == "/admin/usage" and self.command == "GET":
            day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
            return self._send(200, {"day": day, "data": p.usage_day(day)})
        if path == "/admin/usage-dashboard" and self.command == "GET":
            day = parse_qs(urlsplit(self.path).query).get("day", [""])[0]
            return self._send(200, p.database_usage_day(day))
        if path == "/admin/models" and self.command == "POST":
            try:
                value = json.loads(self._body())
            except (ValueError, UnicodeDecodeError) as exc:
                raise ProductError("invalid_json") from exc
            if not isinstance(value, dict):
                raise ProductError("invalid_registration_body")
            status, result = p.register(value)
            return self._send(status, result)
        if path.startswith("/admin/models/"):
            model_id = path[len("/admin/models/"):]
            if self.command == "GET":
                return self._send(200, p.get(model_id))
            if self.command == "DELETE":
                return self._send(200, p.unregister(model_id))
            if self.command == "PATCH":
                try:
                    value = json.loads(self._body())
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ProductError("invalid_json") from exc
                status, result = p.update(model_id, value)
                return self._send(status, result)
        if path.startswith("/requests/") and self.command == "GET":
            request_id = path[len("/requests/"):]
            if not request_id or not all(c.isalnum() or c in "-_" for c in request_id):
                raise ProductError("invalid_request_id")
            file = p.root / "requests" / (request_id + ".json")
            if not file.is_file():
                raise ProductError("request_not_found", 404)
            return self._send(200, json.loads(file.read_text(encoding="utf-8")))
        if path in ("/v1/chat/completions", "/v1/audio/transcriptions") and self.command == "POST":
            return self._proxy(path)
        raise ProductError("unsupported_path", 404)

    def _proxy(self, path: str) -> None:
        p = self.server.product
        body = self._body()
        content_type = self.headers.get("Content-Type", "")
        model_id = None
        context = None
        audio_fields = audio_file = None
        if path.endswith("chat/completions"):
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise ProductError("invalid_json") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
                raise ProductError("invalid_chat_request")
            model_id = payload.get("model")
            context = payload.get("context_tokens")
            if type(context) is not int or context <= 0:
                raise ProductError("max_context_tokens_required")
            limit = payload.get("max_completion_tokens", payload.get("max_tokens"))
            if type(limit) is not int or limit <= 0:
                raise ProductError("max_output_tokens_required")
            if "max_tokens" in payload and "max_completion_tokens" in payload and payload["max_tokens"] != limit:
                raise ProductError("conflicting_output_limits")
            if payload.get("stream") and "text/event-stream" not in self.headers.get("Accept", "text/event-stream"):
                raise ProductError("stream_accept_required")
            required = {"text_input", "text_output"}
            for message in payload.get("messages", []):
                if not isinstance(message, dict):
                    raise ProductError("invalid_chat_message")
                for part in message.get("content", []) if isinstance(message.get("content"), list) else []:
                    if not isinstance(part, dict):
                        raise ProductError("invalid_chat_content")
                    if part.get("type") == "image_url":
                        required.add("image_input")
                    if part.get("type") in ("input_audio", "audio_url"):
                        required.add("audio_input")
        else:
            message = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
            if not message.is_multipart():
                raise ProductError("invalid_transcription_multipart")
            audio_fields = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name or name in audio_fields or (name == "file" and audio_file is not None):
                    raise ProductError("invalid_transcription_multipart")
                value = part.get_payload(decode=True)
                if name == "file":
                    audio_file = (part.get_content_type(), value)
                else:
                    audio_fields[name] = value.decode("utf-8")
            model_id = audio_fields.get("model")
            raw_context = audio_fields.get("context_tokens", "")
            if not raw_context.isascii() or not raw_context.isdigit() or int(raw_context) <= 0:
                raise ProductError("max_context_tokens_required")
            context = int(raw_context)
            raw_limit = audio_fields.get("max_tokens", "")
            if not raw_limit.isascii() or not raw_limit.isdigit() or int(raw_limit) <= 0:
                raise ProductError("max_output_tokens_required")
            limit = int(raw_limit)
            if audio_file is None:
                raise ProductError("transcription_audio_required")
            required = {"audio_input", "text_output"}
        if not isinstance(model_id, str) or not model_id:
            raise ProductError("model_required")
        row = p.get(model_id)
        if row["state"] != "READY":
            raise ProductError("model_not_ready", 409)
        if not required.issubset(row["capabilities"]):
            raise ProductError("capability_not_registered", 422)
        if context > row["max_context_tokens"]:
            raise ProductError("invalid_request_context_tokens", 422)
        choices = 1
        if path.endswith("chat/completions"):
            choices = payload.get("n", 1)
            if type(context) is not int or not 0 < context <= row["max_context_tokens"]:
                raise ProductError("invalid_request_context_tokens", 422)
            if type(choices) is not int or choices < 1:
                raise ProductError("invalid_response_choices", 422)
            if context * choices > row["max_context_tokens"]:
                raise ProductError("request_exceeds_instance_capacity", 422)
        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        if not all(c.isalnum() or c in "-_" for c in request_id) or len(request_id) > 100:
            raise ProductError("invalid_request_id")
        self.request_id = request_id
        record_path = p.root / "requests" / (request_id + ".json")
        if record_path.exists():
            raise ProductError("request_id_conflict", 409)
        record = {"request_id": request_id, "model": model_id, "revision": row["revision"],
                  "configuration": {"max_context_tokens": row["max_context_tokens"]},
                  "path": path, "input_base64": base64.b64encode(body).decode(), "state": "PENDING",
                  "created_at": time.time(), "response": None, "error": None}
        record_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Acceptance and configuration updates share this short lock.
            # An update must drain all accepted calls, including ones that
            # have not yet entered the native runtime.
            with p.lock:
                if p.closing:
                    raise ProductError("service_stopping", 503)
                current = p.get(model_id)
                if current["revision"] != row["revision"]:
                    raise ProductError("model_binding_changed", 409)
                if current["state"] != "READY":
                    raise ProductError("model_not_ready", 409)
                with record_path.open("x", encoding="utf-8") as output:
                    json.dump(record, output, ensure_ascii=False, separators=(",", ":"))
                    output.flush()
                    os.fsync(output.fileno())
                key = (model_id, row["revision"])
                p.request_counts[key] = p.request_counts.get(key, 0) + 1
        except FileExistsError as exc:
            raise ProductError("request_id_conflict", 409) from exc
        started = False
        headers_sent = False
        request_sent = False
        disconnected = False
        is_stream = False
        conn = None
        response = None
        chunks = []
        terminal_tail = bytearray()
        pool = None
        reservation = 0

        def send_headers() -> None:
            nonlocal headers_sent, disconnected
            headers_sent = self._proxy_headers_sent = True
            try:
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in {"connection", "transfer-encoding", "content-length", "server", "date"}:
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
                    # The request already belongs to this service. Finish saving
                    # its native result even when the original client goes away.
                    disconnected = True

        try:
            with p.runtime.lifecycle:
                with p.lock:
                    current = p.get(model_id)
                    if current["revision"] != row["revision"]:
                        raise ProductError("model_binding_changed", 409)
                    if current["state"] not in {"READY", "UPDATING"}:
                        raise ProductError("model_not_ready", 409)
                # Loading or draining can take time. Keep registry queries
                # and cancellation responsive throughout that wait.
                port = p.runtime.start(row)
                with p.lock:
                    current = p.get(model_id)
                    if current["revision"] != row["revision"]:
                        raise ProductError("model_binding_changed", 409)
                    if current["state"] not in {"READY", "UPDATING"}:
                        raise ProductError("model_not_ready", 409)
                    with p.runtime.lock:
                        p.runtime.inflight += 1
                        started = True
                        pool = p.runtime.pool
            if audio_fields is None:
                native_payload = {key: value for key, value in payload.items() if key != "context_tokens"}
                body = json.dumps(native_payload, ensure_ascii=False).encode()
                count_body = body
            else:
                # Match this runtime's ASR-to-chat rendering for token counting;
                # the original multipart still goes to its transcription route.
                with urlopen(f"http://127.0.0.1:{port}/props", timeout=10) as response_props:
                    template = json.load(response_props).get("chat_template", "")
                lfm = "<|tool_list_start|>" in template and "<|tool_list_end|>" in template
                prompt = audio_fields.get("prompt") or ("" if lfm else "Transcribe audio to text")
                if audio_fields.get("language"):
                    prompt += " (language: " + audio_fields["language"] + ")"
                messages = [{"role": "system", "content": "Perform ASR."}] if lfm else []
                mime, content = audio_file
                messages.append({"role": "user", "content": [{"type": "text", "text": prompt},
                    {"type": "input_audio", "input_audio": {"format": mime.split("/")[-1],
                                                            "data": base64.b64encode(content).decode()}}]})
                count_body = json.dumps({"model": model_id, "messages": messages, "max_tokens": limit}).encode()
            count_request = Request(f"http://127.0.0.1:{port}/v1/chat/completions/input_tokens", data=count_body,
                                    headers={"Content-Type": "application/json"})
            try:
                with urlopen(count_request, timeout=90) as counted:
                    input_tokens = json.load(counted).get("input_tokens")
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                raise ProductError("native_input_count_failed", 422) from exc
            if type(input_tokens) is not int or input_tokens < 0:
                raise ProductError("native_input_count_invalid", 422)
            needed = input_tokens + limit
            if needed > context:
                raise ProductError("requested_context_insufficient", 422)
            if needed > p.runtime.capacity["native_context_tokens"]:
                raise ProductError("native_context_exceeded", 422)
            needed = context * choices
            record["capacity"] = {"input_tokens": input_tokens, "max_output_tokens": limit,
                                  "context_tokens": context, "reserved_context_tokens": needed,
                                  "response_choices": choices, "total_context_tokens": pool.total}
            pool.acquire(needed)
            reservation = needed
            conn = HTTPConnection("127.0.0.1", port, timeout=300)
            request_sent = True
            conn.request("POST", path, body, {"Content-Type": content_type, "Content-Length": str(len(body))})
            response = conn.getresponse()
            is_stream = response.getheader("Content-Type", "").split(";", 1)[0].strip() == "text/event-stream"
            if is_stream:
                send_headers()
            read = response.readline if is_stream else response.read1
            while block := read(64 * 1024):
                chunks.append(block)
                if is_stream:
                    if terminal_tail or block.strip() == b"data: [DONE]":
                        terminal_tail.extend(block)
                    else:
                        forward(block)
            if response.length not in (None, 0):
                raise OSError("native_response_incomplete")
            record["state"] = "COMPLETED" if 200 <= response.status < 300 else "FAILED"
        except Exception as exc:
            record["state"] = "UNKNOWN" if request_sent else "FAILED"
            record["error"] = str(exc)
            if not headers_sent:
                raise
        finally:
            if conn is not None:
                conn.close()
            try:
                if response is not None:
                    response_body = b"".join(chunks)
                    record["response"] = {"status": response.status, "headers": dict(response.getheaders()),
                                          "body_base64": base64.b64encode(response_body).decode()}
                    record["usage"] = None
                    if is_stream:
                        complete = False
                        for line in response_body.splitlines():
                            if not line.startswith(b"data:"):
                                continue
                            data = line[5:].strip()
                            if data == b"[DONE]":
                                complete = True
                            else:
                                try:
                                    event = json.loads(data)
                                    if isinstance(event, dict) and isinstance(event.get("usage"), dict):
                                        record["usage"] = event["usage"]
                                except ValueError:
                                    pass
                        if record["state"] == "COMPLETED" and not complete:
                            record.update(state="UNKNOWN", error="native_stream_incomplete")
                    else:
                        try:
                            record["usage"] = json.loads(response_body).get("usage")
                        except (ValueError, AttributeError):
                            pass
                record["updated_at"] = time.time()
                save_json(record_path, record)
            finally:
                # A disk error must not permanently pin the runtime as busy.
                try:
                    if reservation:
                        pool.release(reservation)
                finally:
                    try:
                        p.runtime.release(model_id, started)
                    finally:
                        p.release_request(row)
        if not is_stream:
            send_headers()
            forward(response_body)
        elif record["state"] == "COMPLETED":
            # Publish stream completion only after its result is durable.
            forward(bytes(terminal_tail))

    def do_GET(self) -> None:
        self._safe()

    def do_POST(self) -> None:
        self._safe()

    def do_DELETE(self) -> None:
        self._safe()

    def do_PATCH(self) -> None:
        self._safe()

    def _safe(self) -> None:
        try:
            self._handle()
        except ProductError as exc:
            self._send(exc.status, {"error": {"code": exc.code}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            if getattr(self, "_proxy_headers_sent", False):
                self.close_connection = True
                return
            try:
                self._send(500, {"error": {"code": "internal_error", "detail": str(exc)}})
            except (BrokenPipeError, ConnectionResetError):
                pass


class Server(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], product: Product):
        super().__init__(address, Handler)
        self.product = product
