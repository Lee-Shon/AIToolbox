"""Current LocalAI contracts using isolated HTTP; no production models or GPU."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from local_product import service, v11


class LocalAI:
    prepare = v11.LocalAIClient.prepare
    discard = v11.LocalAIClient.discard
    execution_control = v11.LocalAIClient.execution_control
    resource_snapshot = staticmethod(lambda: dict(total_bytes=100, used_bytes=0,
                                                  utilization=0, observed_at=time.time()))
    resource_budget = staticmethod(lambda row, observation: dict(gpu_bytes=90,
        total_context_tokens=row['max_context_tokens'], precision='isolated_fixture', basis='fixture'))
    def __init__(self):
        self.port, self.configs = 0, {}
        self.loads, self.stops = [], []
        self.failure = None

    def import_model(self, name, spec):
        self.loads.append(name)
        if self.failure:
            raise self.failure
        self.configs[name] = dict(spec)
        return v11.config_hash(self.configs[name])

    def config(self, name):
        return self.configs[name]

    def retire(self, name):
        self.configs.pop(name, None)

    def clone_model(self, name, row):
        self.import_model(name, row['spec'])

    def shutdown(self, name):
        self.stops.append(name)

    def request(self, *args, **kwargs):
        if len(args) > 1 and args[1] == "/v1/chat/completions":
            return v11.LocalAIClient.request(self, *args, **kwargs)
        return {"tokens": [1, 2, 3]}

    def chat(self, *args, **kwargs):
        return {"choices": [{"message": {"content": "ready"}}]}

    def _key(self):
        return "k" * 40


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(payload)
        phase = payload.get("metadata", {}).get("aitoolbox_phase")
        if phase in {"prepare", "discard", "status", "cancel"}:
            value = {"aitoolbox_prepared": 1, "input_tokens": 37,
                     "native_template_kwargs": {"enable_thinking": False}} if phase == "prepare" else {
                "aitoolbox_discarded": 1}
            if phase in {'status', 'cancel'}:
                value = dict(execution_started=True, stop_confirmed=True, confirmed_output_hit=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {
                "content": json.dumps(value)}}]}).encode())
            return
        self.server.entered.set()
        self.server.generations.append(payload)
        mode, stream = self.server.mode, payload.get("stream", False)
        self.send_response(500 if mode == "error" else 200)
        self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
        if mode == "broken":
            self.send_header("Content-Length", "10000")
        self.end_headers()
        if stream:
            content = "data: [DONE]" if mode == "marker_truncated" else "hello"
            self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {
                "content": content}}]}) + "\n\n").encode())
            self.wfile.flush()
        if mode == "gated":
            self.server.release.wait(3)
        usage = {"prompt_tokens": 3, "completion_tokens": 2}
        if stream:
            self.wfile.write(("data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n").encode())
            if mode == "done_truncated":
                self.wfile.write(b"data: [DONE]\n")
            elif mode == "crlf_stream":
                self.wfile.write(b"data: [DONE]\r\n\r\n")
            elif mode not in {"truncated", "broken", "marker_truncated"}:
                self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.wfile.write(json.dumps({"choices": [{"message": {"content": "hello"}}], "usage": usage}).encode())
        self.wfile.flush()


class Regressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ait-v11-contract-")
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.key = self.root / "data.token"
        self.key.write_text("x" * 40)
        self.client = LocalAI()
        self.product = self.make_product()
        model = self.assets / "model.gguf"
        model.write_bytes(b"GGUF")
        self.spec = dict(id="model", model_path=str(model),
                         capabilities=["text_input", "text_output"], max_context_tokens=8192)
        self.product._launch = lambda target, args: target(*args)
        self.product.register(self.spec)

    def make_product(self):
        return v11.LocalAIProduct(self.root / "product", "http://127.0.0.1:49011",
                                  self.key, self.client, [(self.assets, "/models/assets-e")])

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def backend(self, mode="json"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        server.mode, server.calls, server.generations = mode, [], []
        server.entered, server.release = threading.Event(), threading.Event()
        self.client.port = server.server_port
        self.client.base_url = f"http://127.0.0.1:{server.server_port}"
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.release.set()
            server.shutdown()
            server.server_close()
            worker.join(3)

    def handler(self, request_id="request", stream=False, **changes):
        h = object.__new__(v11.V11Handler)
        h.server = SimpleNamespace(product=self.product)
        h.headers = {"X-Request-ID": request_id, "Content-Type": "application/json"}
        payload = dict(model="model", messages=[dict(role="user", content="hello")],
                       context_tokens=8192, max_tokens=16, stream=stream)
        payload.update(changes)
        h._body = lambda: json.dumps(payload).encode()
        h.send_response = lambda *_: None
        h.send_header = lambda *_: None
        h.end_headers = lambda: None
        h.wfile = io.BytesIO()
        return h

    def receipt(self, request_id="request"):
        return json.loads((self.product.root / "requests" / (request_id + ".json")).read_text())

    def call(self, handler=None):
        return (handler or self.handler())._proxy("/v1/chat/completions")

    def legacy_registry(self):
        old = self.product.rows['model']
        for key in ('localai_name', 'localai_hash', 'native_resource_profile'):
            old.pop(key, None)
        old['spec'] = dict(self.spec)
        self.product._save()
        receipt = self.product.root / 'requests' / 'legacy-completed.json'
        service.save_json(receipt, dict(request_id='legacy-completed', state='COMPLETED', response={'text': 'old result'}))
        return receipt, receipt.read_bytes(), self.product.token_path.read_bytes()

    def test_legacy_cpu_upgrade_preserves_history_and_requires_explicit_new_revision(self):
        receipt, original, token = self.legacy_registry()
        loads = list(self.client.loads)
        product = self.make_product()
        self.assertEqual(self.client.loads, loads)
        self.assertEqual(product.rows['model']['state'], 'REJECTED')
        self.assertEqual(product.rows['model']['error'], 'legacy_cpu_registration_requires_update')
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(product.token_path.read_bytes(), token)
        product._launch = lambda target, args: target(*args)
        status, row = product.update('model', dict(max_context_tokens=8192, expected_revision=1))
        self.assertEqual((status, row['state'], row['revision']), (202, 'READY', 2))
        self.assertEqual(receipt.read_bytes(), original)

    def test_legacy_upgrade_failed_validation_preserves_old_registration(self):
        self.legacy_registry()
        product = self.make_product()
        product._launch = lambda target, args: target(*args)
        self.client.failure = service.ProductError('localai_unavailable', 503)
        _, row = product.update('model', dict(max_context_tokens=8192, expected_revision=1))
        self.assertEqual((row['state'], row['revision']), ('REJECTED', 1))
        self.assertEqual(row['spec'], self.spec)
        self.assertEqual(row['update_error'], 'localai_unavailable')

    def test_startup_has_no_windows_runtime_dependency(self):
        self.assertIsInstance(self.make_product().runtime, v11.LocalAIRuntime)
        for name in ("Runtime", "ProcessJob", "main"):
            self.assertFalse(hasattr(service, name))
        self.assertFalse(hasattr(service.Handler, "_proxy"))

    def native_row(self):
        row = self.product.rows["model"]
        row.update(backend="cuda13-vllm", capabilities=["text_input", "audio_input",
                                                      "image_input", "text_output"])

    def test_native_prepare_before_quota_and_only_one_generation(self):
        self.native_row()
        with self.backend() as server:
            self.call(self.handler(context_tokens=53))
        phases = [p["metadata"]["aitoolbox_phase"] for p in server.calls]
        self.assertEqual(phases, ["prepare", "execute", "status", "discard"])
        self.assertEqual(server.calls[0]["seed"], server.calls[1]["seed"])
        self.assertEqual(json.loads(server.calls[1]["metadata"]["chat_template_kwargs"]),
                         {"enable_thinking": False})
        receipt = self.receipt()
        self.assertEqual(receipt["capacity"]["input_tokens"], 37)
        self.assertEqual(receipt["capacity"]["reserved_context_tokens"], 53)
        self.assertEqual(receipt["state"], "COMPLETED")

    def test_stream_content_cannot_impersonate_terminal_event(self):
        with self.backend("marker_truncated") as backend:
            self.call(self.handler(stream=True))
        receipt = self.receipt()
        self.assertEqual(receipt["state"], "UNKNOWN")
        self.assertEqual(receipt["error"], "localai_stream_incomplete")
        self.assertIn(b"data: [DONE]", base64.b64decode(receipt["response"]["body_base64"]))
        self.assertEqual(len(backend.generations), 1)
        self.assertEqual(self.product.request_counts, {})
        self.assertEqual(self.product.runtime.placement.instances, [])

    def test_stream_requires_a_complete_terminal_event(self):
        with self.backend("done_truncated"):
            self.call(self.handler(stream=True))
        self.assertEqual(self.receipt()["state"], "UNKNOWN")

    def test_stream_accepts_crlf_terminal_event(self):
        with self.backend("crlf_stream"):
            self.call(self.handler(stream=True))
        self.assertEqual(self.receipt()["state"], "COMPLETED")

    def test_presend_receipt_failure_is_known_not_submitted(self):
        persist = v11.save_json
        failed = []
        def fail_once(path, record):
            if record.get("execution_started_at") and not failed:
                failed.append(True)
                raise OSError("presend disk failure")
            persist(path, record)
        with self.backend() as backend, patch.object(v11, "save_json", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "presend disk failure"):
                self.call()
        receipt = self.receipt()
        self.assertEqual(receipt["state"], "FAILED")
        self.assertIsNone(receipt["response"])
        self.assertEqual(backend.generations, [])
        self.assertEqual([call["metadata"]["aitoolbox_phase"] for call in backend.calls],
                         ["prepare", "discard"])
        self.assertEqual(self.product.request_counts, {})
        self.assertEqual(self.product.runtime.placement.instances, [])

    def test_incomplete_http_body_never_accepts_or_generates(self):
        from local_product.service import Server
        relay = Server(("127.0.0.1", 0), self.product, v11.V11Handler)
        worker = threading.Thread(target=relay.serve_forever,
            kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        raw = self.handler()._body()
        try:
            with self.backend() as backend, socket.create_connection(relay.server_address, timeout=3) as client:
                headers = ("POST /v1/chat/completions HTTP/1.0\r\n"
                    "Authorization: Bearer " + self.product.token + "\r\n"
                    "Content-Type: application/json\r\nX-Request-ID: incomplete\r\n"
                    f"Content-Length: {len(raw)+10}\r\n\r\n").encode()
                client.sendall(headers + raw)
                client.shutdown(socket.SHUT_WR)
                with client.makefile("rb") as incoming:
                    response = incoming.read()
                self.assertIn(b"400 Bad Request", response)
                self.assertIn(b"incomplete_request_body", response)
                self.assertEqual(backend.calls, [])
                self.assertFalse((self.product.root / "requests" / "incomplete.json").exists())
                self.assertEqual(self.product.request_counts, {})
        finally:
            relay.shutdown()
            relay.server_close()
            worker.join(3)

    def test_presend_credential_failure_is_known_not_submitted(self):
        with self.backend() as backend, patch.object(self.client, "_key", side_effect=[
                "k" * 40, OSError("presend credential failure"), "k" * 40]):
            with self.assertRaisesRegex(OSError, "presend credential failure"):
                self.call()
        self.assertEqual(self.receipt()["state"], "FAILED")
        self.assertIsNone(self.receipt()["response"])
        self.assertEqual(backend.generations, [])
        self.assertEqual(self.product.request_counts, {})
        self.assertEqual(self.product.runtime.placement.instances, [])

    def test_native_count_over_quota_never_sends_generation(self):
        self.native_row()
        with self.backend() as server:
            with self.assertRaisesRegex(service.ProductError, "requested_context_insufficient"):
                self.call(self.handler(context_tokens=52))
        self.assertEqual([p["metadata"]["aitoolbox_phase"] for p in server.calls],
                         ["prepare", "discard"])
        self.assertEqual(self.receipt()["state"], "FAILED")

    def test_gguf_multimodal_uses_native_count_with_small_context(self):
        row = self.product.rows["model"]
        row["capabilities"] = row["capabilities"] + ["image_input"]
        content = [{"type": "text", "text": "read"},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]
        with self.backend() as server:
            self.call(self.handler(context_tokens=53, messages=[{"role": "user", "content": content}]))
        self.assertEqual(len(server.generations), 1)
        self.assertEqual(self.receipt()["capacity"]["input_tokens_source"], "llama_prepared_server_tokens")
        self.assertEqual(self.receipt()["capacity"]["reserved_context_tokens"], 53)

    def test_unsupported_multiple_choice_modes_are_explicit(self):
        for extra in ({"stream": True}, {"tools": [{"type": "function"}]}):
            with self.subTest(extra=extra), self.assertRaisesRegex(
                    service.ProductError, "multiple_choices_stream_or_tools_unsupported"):
                self.call(self.handler(n=2, **extra))

    def test_native_wait_keeps_preparation_and_full_declared_reservation(self):
        self.native_row()
        with self.backend("gated") as server, ThreadPoolExecutor(max_workers=2) as worker:
            first = worker.submit(self.call, self.handler("first", context_tokens=5000))
            self.assertTrue(server.entered.wait(2))
            second = worker.submit(self.call, self.handler("second", context_tokens=4000))
            deadline = time.monotonic() + 2
            while len(server.calls) < 3 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual([p["metadata"]["aitoolbox_phase"] for p in server.calls],
                             ["prepare", "execute", "prepare"])
            self.assertEqual(self.product.runtime.pool.tokens, 5000)
            server.release.set()
            first.result(3)
            second.result(3)
        self.assertEqual(sum(p["metadata"]["aitoolbox_phase"] == "execute" for p in server.calls), 2)
        self.assertEqual(self.receipt("second")["capacity"]["reserved_context_tokens"], 4000)

    def audio_handler(self, **fields):
        fields = {"model": "model", "context_tokens": "100", "max_tokens": "27", **fields}
        parts = [f'--boundary\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                 for key, value in fields.items()]
        parts.append(b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                     b'Content-Type: audio/wav\r\n\r\nRIFF0000WAVEaudio\r\n--boundary--\r\n')
        h = self.handler()
        h.headers["Content-Type"] = "multipart/form-data; boundary=boundary"
        h._body = lambda: b"".join(parts)
        return h

    def test_asr_uses_same_flow_and_requested_output_format_and_limit(self):
        self.native_row()
        h = self.audio_handler(response_format="text", temperature="0.4")
        with self.backend() as server:
            h._proxy("/v1/audio/transcriptions")
        self.assertEqual(h.wfile.getvalue(), b"hello")
        execution = server.calls[1]
        self.assertEqual(execution["max_tokens"], 27)
        self.assertEqual(execution["temperature"], .4)
        receipt = self.receipt()
        self.assertEqual(receipt["capacity"]["input_tokens"], 37)
        self.assertEqual(receipt["response"]["headers"]["Content-Type"], "text/plain; charset=utf-8")
        self.assertTrue(receipt["input_content_type"].startswith("multipart/"))

    def test_asr_never_silently_ignores_unsupported_options(self):
        for fields in ({"language": "zh"}, {"prompt": "hint"}, {"response_format": "srt"},
                       {"stream": "true"}, {"timestamp_granularities[]": "word"}):
            with self.subTest(fields=fields), self.assertRaisesRegex(
                    service.ProductError, "unsupported_transcription_parameter"):
                self.audio_handler(**fields)._proxy("/v1/audio/transcriptions")

    def test_registration_probe_releases_before_ready(self):
        self.assertEqual(self.product.get("model")["state"], "READY")
        self.assertIsNone(self.product.runtime.model_id)
        self.assertEqual(self.client.stops, ["model"])

    def test_registration_idempotency_and_conflict(self):
        self.assertEqual(self.product.register(self.spec)[0], 200)
        self.assertEqual(len(self.client.loads), 1)
        with self.assertRaisesRegex(service.ProductError, "model_id_conflict"):
            self.product.register({**self.spec, "max_context_tokens": 4096})

    def test_localai_transient_failure_retries_original_binding(self):
        self.client.failure = service.ProductError("localai_unavailable", 503)
        self.product.register({**self.spec, "id": "retry"})
        self.assertEqual(self.product.get("retry")["state"], "REJECTED")
        self.client.failure = None
        self.product.register({**self.spec, "id": "retry"})
        row = self.product.get("retry")
        self.assertEqual((row["state"], row["revision"]), ("READY", 1))

    def test_update_success_and_stale_revision(self):
        self.product.update("model", {"max_context_tokens": 4096, "expected_revision": 1})
        self.assertEqual(self.product.get("model")["revision"], 2)
        with self.assertRaisesRegex(service.ProductError, "model_revision_conflict"):
            self.product.update("model", {"max_context_tokens": 8192, "expected_revision": 1})

    def test_failed_update_keeps_previous_ready_binding(self):
        original = self.product.get("model")
        self.client.failure = service.ProductError("localai_unavailable", 503)
        self.product.update("model", {"max_context_tokens": 4096})
        row = self.product.get("model")
        self.assertEqual((row["state"], row["revision"]), ("READY", 1))
        self.assertEqual(row["localai_hash"], original["localai_hash"])

    def test_unregister_preserves_assets_and_reregistration_advances_revision(self):
        self.product.unregister("model")
        self.assertTrue(Path(self.spec["model_path"]).exists())
        self.assertEqual(self.product.unregister("model")["state"], "REMOVED")
        self.product.register(self.spec)
        self.assertEqual(self.product.get("model")["revision"], 2)

    def test_restart_keeps_ready_without_loading(self):
        loads = list(self.client.loads)
        self.assertEqual(self.make_product().get("model")["state"], "READY")
        self.assertEqual(self.client.loads, loads)

    def test_restart_marks_pending_unknown_without_replay(self):
        path = self.product.root / "requests" / "pending.json"
        service.save_json(path, {"request_id": "pending", "state": "PENDING", "input_base64": "YWJj"})
        loads = list(self.client.loads)
        self.make_product()
        self.assertEqual(self.receipt("pending")["state"], "UNKNOWN")
        self.assertEqual(self.receipt("pending")["input_base64"], "YWJj")
        self.assertEqual(self.client.loads, loads)

    def test_dispatch_once_and_save_before_delivery(self):
        h = self.handler()
        def write(block):
            self.assertEqual(self.receipt()["state"], "COMPLETED")
        h.wfile = SimpleNamespace(write=write, flush=lambda: None)
        with self.backend() as backend:
            self.call(h)
            self.assertEqual(len(backend.generations), 1)
        self.assertEqual(self.receipt()["usage"]["completion_tokens"], 2)
        self.assertFalse(self.product.request_counts)
        self.assertIsNone(self.product.runtime.model_id)

    def test_stream_usage_done_and_original_input(self):
        h = self.handler(stream=True)
        with self.backend():
            self.call(h)
        self.assertIn(b"[DONE]", h.wfile.getvalue())
        self.assertEqual(self.receipt()["state"], "COMPLETED")
        self.assertEqual(base64.b64decode(self.receipt()["input_base64"]), h._body())
        self.assertEqual(self.receipt()["usage"]["prompt_tokens"], 3)

    def test_truncated_stream_preserves_partial_result(self):
        with self.backend("truncated"):
            self.call(self.handler(stream=True))
        self.assertEqual(self.receipt()["state"], "UNKNOWN")
        self.assertIn(b"hello", base64.b64decode(self.receipt()["response"]["body_base64"]))

    def test_broken_http_preserves_partial_bytes_and_unknown_receipt(self):
        with self.backend("broken"):
            self.call(self.handler(stream=True))
        self.assertEqual(self.receipt()["state"], "UNKNOWN")
        self.assertIn(b"hello", base64.b64decode(self.receipt()["response"]["body_base64"]))
        self.assertFalse(self.product.request_counts)

    def test_confirmed_abort_survives_a_broken_response_and_repeat_cancel(self):
        h = self.handler(stream=True)
        def received(block):
            self.product.active_requests['request']['cancel'].set()
        h.wfile = SimpleNamespace(write=received, flush=lambda: None)
        proof = dict(stop_confirmed=True, cancel_requested=True, already_finished=True,
                     execution_started=True, native_executions=[dict(finish_reason='abort')])
        with self.backend('broken'), patch.object(self.client, 'execution_control', return_value=proof):
            self.call(h)
        receipt = self.receipt()
        self.assertEqual(receipt['state'], 'CANCELLED')
        self.assertEqual(receipt['stop_proof'], proof)
        self.assertIn(b'hello', base64.b64decode(receipt['response']['body_base64']))
        self.assertFalse(self.product.request_counts)
        self.assertFalse(self.product.runtime.placement.instances)

    def test_client_disconnect_keeps_complete_result(self):
        h = self.handler(stream=True)
        def broken(_):
            raise BrokenPipeError("client disconnected")
        h.wfile = SimpleNamespace(write=broken, flush=lambda: None)
        with self.backend():
            self.call(h)
        self.assertEqual(self.receipt()["state"], "COMPLETED")

    def test_native_error_saves_failed_receipt(self):
        with self.backend("error"):
            self.call()
        self.assertEqual(self.receipt()["state"], "FAILED")

    def test_duplicate_id_does_not_dispatch_twice(self):
        with self.backend() as backend:
            self.call()
            with self.assertRaisesRegex(service.ProductError, "request_id_conflict"):
                self.call()
            self.assertEqual(len(backend.generations), 1)

    def test_invalid_context_rejected_before_generation(self):
        with self.backend() as backend:
            for changes in ({"context_tokens": None}, {"context_tokens": 16384},
                            {"context_tokens": 52, "max_tokens": 16}):
                with self.subTest(changes=changes), self.assertRaises(service.ProductError):
                    self.call(self.handler(**changes))
            self.assertFalse(backend.generations)
        self.assertFalse(self.product.request_counts)

    def test_shared_quota_waits_before_second_generation(self):
        with self.backend("gated") as backend, ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(self.call, self.handler("first"))
            self.assertTrue(backend.entered.wait(2))
            second = workers.submit(self.call, self.handler("second"))
            time.sleep(.05)
            self.assertEqual(len(backend.generations), 1)
            backend.release.set()
            first.result(timeout=3)
            second.result(timeout=3)
            self.assertEqual(len(backend.generations), 2)

    def test_unregister_drains_and_blocks_new_calls(self):
        with self.backend("gated") as backend, ThreadPoolExecutor(max_workers=2) as workers:
            active = workers.submit(self.call)
            self.assertTrue(backend.entered.wait(2))
            removal = workers.submit(self.product.unregister, "model")
            deadline = time.monotonic() + 2
            while self.product.rows["model"]["state"] != "DRAINING" and time.monotonic() < deadline:
                time.sleep(.01)
            with self.assertRaisesRegex(service.ProductError, "model_not_ready"):
                self.call(self.handler("new"))
            self.assertFalse(removal.done())
            backend.release.set()
            active.result(timeout=3)
            self.assertEqual(removal.result(timeout=3)["state"], "REMOVED")

    def test_result_save_failure_does_not_publish_done_or_pin_model(self):
        h = self.handler(stream=True)
        persist = v11.save_json
        def fail_terminal(path, record):
            if record["state"] == "COMPLETED":
                raise OSError("disk full")
            persist(path, record)
        with self.backend() as backend, patch.object(v11, "save_json", side_effect=fail_terminal):
            with self.assertRaises(OSError):
                self.call(h)
            self.assertEqual(len(backend.generations), 1)
        self.assertNotIn(b"[DONE]", h.wfile.getvalue())
        self.assertFalse(self.product.request_counts)
        self.assertIsNone(self.product.runtime.model_id)

    def test_real_http_entry_and_original_id_lookup(self):
        relay = service.Server(("127.0.0.1", 0), self.product, v11.V11Handler)
        thread = threading.Thread(target=relay.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            with self.backend() as backend:
                headers = {"Authorization": "Bearer " + self.product.token, "Content-Type": "application/json"}
                request = Request(f"http://127.0.0.1:{relay.server_port}/v1/chat/completions",
                                  data=self.handler()._body(), headers=headers)
                with urlopen(request, timeout=3) as response:
                    request_id = response.headers["X-AIToolbox-Request-ID"]
                    self.assertEqual(json.load(response)["choices"][0]["message"]["content"], "hello")
                with urlopen(Request(f"http://127.0.0.1:{relay.server_port}/requests/{request_id}", headers=headers)) as response:
                    self.assertEqual(json.load(response)["state"], "COMPLETED")
                self.assertEqual(len(backend.generations), 1)
        finally:
            relay.shutdown()
            relay.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
