"""User-visible V10 relay regressions; no model/GPU or production state needed."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from local_product import service


class FakeRuntime:
    def __init__(self, port=0):
        self.port = port
        self.lock = threading.RLock()
        self.lifecycle = threading.RLock()
        self.inflight = 0
        self.model_id = None
        self.error = None
        self.pool = service.SharedContext(8192)
        self.capacity = {"native_context_tokens": 8192}

    def start(self, row):
        if self.error:
            raise service.ProductError(self.error, 422)
        self.model_id = row['id']
        return self.port

    def stop(self):
        self.model_id = None


class ImmediateThread:
    def __init__(self, *, target, args=(), **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class Native(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"chat_template":"chatml"}')

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        if self.path.endswith('/input_tokens'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"input_tokens":3}')
            return
        self.server.payload = json.loads(body) if self.headers['Content-Type'].startswith('application/json') else body
        self.server.generations.append(body)
        self.server.entered.set()
        self.send_response(200)
        mode = self.server.mode
        self.send_header('Content-Type', 'text/event-stream' if mode != 'json' else 'application/json')
        self.end_headers()
        try:
            if mode == 'json':
                self.wfile.write(b'{"choices":[{"message":{"content":"working"}}],"usage":{"prompt_tokens":3,"completion_tokens":2}}')
                return
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
            self.wfile.flush()
            if mode == 'gated':
                self.server.release.wait(3)
            self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n')
            if mode != 'truncated':
                self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class Regressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ait-v10-debug-')
        self.root = Path(self.temp.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        token = self.root / 'data.token'
        token.write_text('t' * 40)
        self.product = service.Product(self.root, Path(sys.executable), 'http://127.0.0.1:49011', token)
        self.product.runtime = FakeRuntime()
        weights = self.root / 'model.gguf'
        weights.write_bytes(b'GGUF')
        self.spec = dict(id='model', model_path=str(weights), mmproj_path=None,
                         capabilities=['text_input', 'text_output'], max_context_tokens=8192)

    def tearDown(self):
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.temp.cleanup()

    def ready(self):
        self.product.rows['model'] = dict(self.spec, spec=self.spec, revision=1, state='READY',
                                           assets={}, checked={}, error=None, updated_at=time.time())
        self.product._save()

    @contextmanager
    def native(self, mode):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Native)
        server.mode, server.release = mode, threading.Event()
        server.entered = threading.Event()
        server.generations = []
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.product.runtime.port = server.server_port
        try:
            yield server
        finally:
            server.release.set()
            server.shutdown()
            server.server_close()
            worker.join(3)

    def handler(self, request_id='request', stream=False, broken_client=False):
        handler = object.__new__(service.Handler)
        handler.server = SimpleNamespace(product=self.product)
        handler.headers = {'Content-Type': 'application/json', 'X-Request-ID': request_id}
        payload = json.dumps(dict(model='model', messages=[dict(role='user', content='hello')],
                                  max_tokens=16, context_tokens=8192, stream=stream)).encode()
        handler._body = lambda: payload
        handler.send_response = lambda *_: None
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        if broken_client:
            handler.wfile = SimpleNamespace(write=lambda _: self.disconnect(), flush=lambda: None)
        return handler

    @staticmethod
    def disconnect():
        raise BrokenPipeError('client disconnected')

    def receipt(self, request_id='request'):
        return json.loads((self.root / 'requests' / (request_id + '.json')).read_text())

    def test_retry_transient_registration_failure_same_binding(self):
        self.product.runtime.error = 'insufficient_free_system_memory'
        with patch.object(service.threading, 'Thread', ImmediateThread), patch.object(
                service, 'probe', return_value={'text_input': {}, 'text_output': {}}):
            self.product.register(self.spec)
            self.assertEqual(self.product.get('model')['state'], 'REJECTED')
            self.product.runtime.error = None
            status, _ = self.product.register(self.spec)
            self.assertEqual(status, 202)
            self.assertEqual(self.product.get('model')['state'], 'READY')
            self.assertEqual(self.product.get('model')['revision'], 1)

    def test_existing_ready_and_permanent_rejection_are_idempotent(self):
        self.ready()
        self.assertEqual(self.product.register(self.spec)[0], 200)
        self.product.rows['model'].update(state='REJECTED', error='text_probe_empty')
        self.assertEqual(self.product.register(self.spec)[0], 200)
        self.assertEqual(self.product.get('model')['error'], 'text_probe_empty')
        with self.assertRaises(service.ProductError) as caught:
            self.product.register(dict(self.spec, max_context_tokens=4096))
        self.assertEqual(caught.exception.code, 'model_id_conflict')

    def test_restart_keeps_ready_without_loading_or_revalidating(self):
        self.ready()
        before = self.product.get('model')
        with patch.object(service.threading, 'Thread') as thread, patch.object(
                service.Runtime, 'start') as start:
            restarted = service.Product(self.root, Path(sys.executable),
                                        'http://127.0.0.1:49011', self.root / 'data.token')
            self.assertEqual(restarted.get('model'), before)
            self.assertEqual(restarted.list_ready()[0]['id'], 'model')
            self.assertEqual(restarted.list_all()[0]['state'], 'READY')
            self.assertIsNone(restarted.runtime.process)
            thread.assert_not_called()
            start.assert_not_called()

    def test_interrupted_validation_waits_for_explicit_retry(self):
        self.ready()
        self.product.rows['model']['state'] = 'VALIDATING'
        self.product._save()
        with patch.object(service.threading, 'Thread') as thread:
            restarted = service.Product(self.root, Path(sys.executable),
                                        'http://127.0.0.1:49011', self.root / 'data.token')
            thread.assert_not_called()
        self.assertEqual(restarted.get('model')['state'], 'REJECTED')
        self.assertEqual(restarted.get('model')['error'], 'validation_interrupted_by_restart')
        restarted.runtime = FakeRuntime()
        with patch.object(service.threading, 'Thread', ImmediateThread), patch.object(
                service, 'probe', return_value={'text_input': {}, 'text_output': {}}):
            self.assertEqual(restarted.register(self.spec)[0], 202)
        self.assertEqual(restarted.get('model')['state'], 'READY')
        self.assertEqual(restarted.get('model')['revision'], 1)
        self.assertIsNone(restarted.runtime.model_id)

    def test_validation_success_releases_model_before_ready(self):
        def probe(port, row):
            self.assertEqual(self.product.runtime.model_id, 'model')
            self.assertEqual(self.product.get('model')['state'], 'VALIDATING')
            return {'text_input': {'content': 'ready'}, 'text_output': {'content': 'ready'}}

        with patch.object(service.threading, 'Thread', ImmediateThread), patch.object(
                service, 'probe', side_effect=probe):
            self.product.register(self.spec)
        self.assertEqual(self.product.get('model')['state'], 'READY')
        self.assertEqual(self.product.get('model')['checked']['text_output']['content'], 'ready')
        self.assertIsNone(self.product.runtime.model_id)

    def test_validation_failure_and_incomplete_probe_release_model(self):
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                self.product.rows.clear()
                with patch.object(service.threading, 'Thread', ImmediateThread), patch.object(
                        service, 'probe', return_value={'text_input': {}},
                        side_effect=None if incomplete else service.ProductError('text_probe_empty', 422)):
                    self.product.register(self.spec)
                self.assertEqual(self.product.get('model')['state'], 'REJECTED')
                self.assertIsNone(self.product.runtime.model_id)

    def test_validation_cleanup_failure_does_not_publish_ready(self):
        with patch.object(service.threading, 'Thread', ImmediateThread), patch.object(
                service, 'probe', return_value={'text_input': {}, 'text_output': {}}), patch.object(
                self.product.runtime, 'stop', side_effect=OSError('native process did not exit')):
            self.product.register(self.spec)
        self.assertEqual(self.product.get('model')['state'], 'REJECTED')
        self.assertEqual(self.product.get('model')['error'], 'native process did not exit')

    def test_model_loading_does_not_block_registry_reads(self):
        self.ready()
        loading, release, read_done = threading.Event(), threading.Event(), threading.Event()
        original_start = self.product.runtime.start

        def slow_start(row):
            loading.set()
            if not release.wait(3):
                raise TimeoutError('test load gate')
            return original_start(row)

        def read_registry():
            self.product.list_all()
            read_done.set()

        with self.native('json'), ThreadPoolExecutor(max_workers=2) as workers, patch.object(
                self.product.runtime, 'start', side_effect=slow_start):
            request = workers.submit(self.handler()._proxy, '/v1/chat/completions')
            try:
                self.assertTrue(loading.wait(2))
                reader = workers.submit(read_registry)
                self.assertTrue(read_done.wait(1), 'loading holds the registry lock')
            finally:
                release.set()
                request.result(timeout=3)

    def test_old_binding_cannot_run_after_unregister_and_reregister(self):
        self.ready()
        original_get = self.product.get
        first = True

        def get_and_replace(model_id):
            nonlocal first
            row = original_get(model_id)
            if first:
                first = False
                self.product.rows[model_id] = dict(row, revision=2)
            return row

        with self.native('json'), patch.object(self.product, 'get', side_effect=get_and_replace), patch.object(
                self.product.runtime, 'start', wraps=self.product.runtime.start) as start:
            with self.assertRaises(service.ProductError) as caught:
                self.handler()._proxy('/v1/chat/completions')
            self.assertEqual(caught.exception.code, 'model_binding_changed')
            start.assert_not_called()
        self.assertFalse((self.root / 'requests' / 'request.json').exists())

    def test_unregister_does_not_stop_model_switched_by_another_request(self):
        self.ready()
        runtime = self.product.runtime
        attempted = threading.Event()

        class ObservedLock:
            def __init__(self):
                self.lock = threading.RLock()

            def __enter__(self):
                attempted.set()
                self.lock.acquire()
                return self

            def __exit__(self, *_):
                self.lock.release()

        runtime.lifecycle = ObservedLock()
        runtime.model_id = 'model'

        def stop():
            with runtime.lifecycle:
                runtime.model_id = None

        runtime.lifecycle.lock.acquire()
        with ThreadPoolExecutor(max_workers=1) as workers, patch.object(runtime, 'stop', side_effect=stop):
            request = workers.submit(self.product.unregister, 'model')
            try:
                self.assertTrue(attempted.wait(2))
                runtime.model_id = 'other-model'
            finally:
                runtime.lifecycle.lock.release()
            self.assertEqual(request.result(timeout=3)['state'], 'REMOVED')
        self.assertEqual(runtime.model_id, 'other-model')

    def test_cancel_during_load_blocks_new_calls_and_prevents_native_dispatch(self):
        self.ready()
        loading, release = threading.Event(), threading.Event()
        original_start = self.product.runtime.start

        def slow_start(row):
            loading.set()
            if not release.wait(3):
                raise TimeoutError('test load gate')
            return original_start(row)

        with self.native('json') as native, ThreadPoolExecutor(max_workers=2) as workers, patch.object(
                self.product.runtime, 'start', side_effect=slow_start):
            request = workers.submit(self.handler()._proxy, '/v1/chat/completions')
            try:
                self.assertTrue(loading.wait(2))
                cancellation = workers.submit(self.product.unregister, 'model')
                until = time.monotonic() + 2
                while self.product.get('model')['state'] != 'DRAINING' and time.monotonic() < until:
                    time.sleep(.01)
                self.assertEqual(self.product.get('model')['state'], 'DRAINING')
                with self.assertRaises(service.ProductError) as denied:
                    self.handler(request_id='new-call')._proxy('/v1/chat/completions')
                self.assertEqual(denied.exception.code, 'model_not_ready')
            finally:
                release.set()
            with self.assertRaises(service.ProductError) as stopped:
                request.result(timeout=3)
            self.assertEqual(stopped.exception.code, 'model_not_ready')
            self.assertEqual(cancellation.result(timeout=3)['state'], 'REMOVED')
            self.assertFalse(native.entered.is_set())
        self.assertEqual(self.receipt()['state'], 'FAILED')
        self.assertIsNone(self.product.runtime.model_id)

    def test_switch_and_validation_wait_for_active_response(self):
        for validation in (False, True):
            with self.subTest(validation=validation):
                self.ready()
                other = dict(self.spec, id='other')
                self.product.rows['other'] = dict(other, spec=other, revision=1,
                    state='VALIDATING' if validation else 'READY', assets={}, checked={}, error=None)
                # Use the real drain/stop implementation, with only process
                # loading replaced. Native responses still cross local HTTP.
                runtime = service.Runtime(Path(sys.executable), self.root)
                self.product.runtime = runtime
                switching = threading.Event()
                with self.native('gated') as native:
                    def start(row):
                        if runtime.model_id != row['id']:
                            if row['id'] == 'other':
                                switching.set()
                            runtime.stop()
                            runtime.port = native.server_port
                            runtime.model_id = row['id']
                            runtime.pool = service.SharedContext(row['max_context_tokens'])
                            runtime.capacity = {'native_context_tokens': row['max_context_tokens']}
                        return native.server_port

                    with ThreadPoolExecutor(max_workers=2) as workers, patch.object(
                            runtime, 'start', side_effect=start), patch.object(service, 'probe',
                            return_value={'text_input': {}, 'text_output': {}}):
                        rid = 'active-' + str(validation)
                        active = workers.submit(self.handler(request_id=rid, stream=True)._proxy,
                                                '/v1/chat/completions')
                        try:
                            self.assertTrue(native.entered.wait(2))
                            if validation:
                                waiting = workers.submit(self.product._validate, 'other')
                            else:
                                next_call = self.handler(request_id='other', stream=True)
                                body = json.loads(next_call._body())
                                body['model'] = 'other'
                                next_call._body = lambda: json.dumps(body).encode()
                                waiting = workers.submit(next_call._proxy, '/v1/chat/completions')
                            self.assertTrue(switching.wait(2))
                            self.assertEqual(runtime.model_id, 'model')
                            self.assertEqual(runtime.inflight, 1)
                            self.assertFalse(active.done())
                        finally:
                            native.release.set()
                        active.result(timeout=3)
                        waiting.result(timeout=3)
                self.assertEqual(self.receipt(rid)['state'], 'COMPLETED')
                self.assertEqual(runtime.inflight, 0)
                if validation:
                    self.assertEqual(self.product.get('other')['state'], 'READY')
                    self.assertIsNone(runtime.model_id)
                else:
                    self.assertEqual(self.receipt('other')['state'], 'COMPLETED')
                    self.assertEqual(runtime.model_id, 'other')

    def test_concurrent_duplicate_request_id_runs_native_once(self):
        self.ready()
        with self.native('gated') as native, ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(self.handler(stream=True)._proxy, '/v1/chat/completions')
            try:
                self.assertTrue(native.entered.wait(2))
                duplicate = workers.submit(self.handler(stream=True)._proxy, '/v1/chat/completions')
                with self.assertRaises(service.ProductError) as caught:
                    duplicate.result(timeout=2)
                self.assertEqual(caught.exception.code, 'request_id_conflict')
                self.assertEqual(self.product.runtime.inflight, 1)
            finally:
                native.release.set()
            first.result(timeout=3)
        self.assertEqual(self.receipt()['state'], 'COMPLETED')
        self.assertEqual(self.product.runtime.inflight, 0)

    def test_stream_delivers_before_native_finishes(self):
        self.ready()
        with self.native('gated') as native:
            proxy = service.Server(('127.0.0.1', 0), self.product)
            worker = threading.Thread(target=proxy.serve_forever, daemon=True)
            worker.start()
            payload = json.dumps(dict(model='model', messages=[], max_tokens=16,
                                      context_tokens=8192, stream=True)).encode()
            request = Request(f'http://127.0.0.1:{proxy.server_port}/v1/chat/completions', data=payload,
                              headers={'Authorization': 'Bearer ' + self.product.token,
                                       'Content-Type': 'application/json', 'X-Request-ID': 'stream'})
            try:
                with urlopen(request, timeout=1) as response:
                    self.assertIn(b'hello', response.readline())
                    native.release.set()
                    self.assertIn(b'[DONE]', response.read())
            finally:
                native.release.set()
                proxy.shutdown()
                proxy.server_close()
                worker.join(3)

    def test_stream_usage_saved_in_receipt(self):
        self.ready()
        with self.native('stream'):
            self.handler(stream=True)._proxy('/v1/chat/completions')
        row = self.receipt()
        self.assertEqual(row['state'], 'COMPLETED')
        self.assertEqual(row['usage'], {'prompt_tokens': 3, 'completion_tokens': 2})

    def test_truncated_stream_is_unknown_and_partial_result_is_kept(self):
        self.ready()
        with self.native('truncated'):
            self.handler(stream=True)._proxy('/v1/chat/completions')
        row = self.receipt()
        self.assertEqual(row['state'], 'UNKNOWN')
        self.assertIn(b'hello', base64.b64decode(row['response']['body_base64']))

    def test_client_disconnect_keeps_complete_result(self):
        self.ready()
        with self.native('stream'):
            self.handler(stream=True, broken_client=True)._proxy('/v1/chat/completions')
        row = self.receipt()
        self.assertEqual(row['state'], 'COMPLETED')
        self.assertIn(b'[DONE]', base64.b64decode(row['response']['body_base64']))

    def test_save_failure_releases_inflight_and_sends_no_success_body(self):
        self.ready()
        handler = self.handler()
        with self.native('json'), patch.object(service, 'save_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                handler._proxy('/v1/chat/completions')
        self.assertEqual(self.product.runtime.inflight, 0)
        self.assertEqual(handler.wfile.getvalue(), b'')

    def test_stream_save_failure_does_not_publish_done_or_pin_runtime(self):
        self.ready()
        handler = self.handler(stream=True)
        with self.native('stream'), patch.object(service, 'save_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                handler._proxy('/v1/chat/completions')
        self.assertIn(b'hello', handler.wfile.getvalue())
        self.assertNotIn(b'[DONE]', handler.wfile.getvalue())
        self.assertEqual(self.product.runtime.inflight, 0)

    def test_non_stream_result_saved_before_response(self):
        self.ready()
        handler = self.handler()
        captured = io.BytesIO()

        def write(block):
            self.assertEqual(self.receipt()['state'], 'COMPLETED')
            return captured.write(block)

        handler.wfile = SimpleNamespace(write=write, flush=lambda: None)
        with self.native('json'):
            handler._proxy('/v1/chat/completions')
        self.assertEqual(json.loads(captured.getvalue())['choices'][0]['message']['content'], 'working')

    def test_restart_reconciles_pending_without_replaying(self):
        receipt = self.root / 'requests' / 'interrupted.json'
        service.save_json(receipt, dict(request_id='interrupted', state='PENDING', created_at=time.time(),
                                       input_base64='aGVsbG8=', model='model', revision=1))
        product = service.Product(self.root, Path(sys.executable), 'http://127.0.0.1:49011', self.root / 'data.token')
        row = self.receipt('interrupted')
        self.assertEqual(row['state'], 'UNKNOWN')
        self.assertEqual(row['input_base64'], 'aGVsbG8=')
        self.assertIsNone(product.runtime.process)


    def test_register_total_context(self):
        with patch.object(service.threading, 'Thread', ImmediateThread), patch.object(
                service, 'probe', return_value={'text_input': {}, 'text_output': {}}):
            self.product.register(dict(self.spec, max_context_tokens=16384))
        row = self.product.get('model')
        self.assertEqual((row['max_context_tokens'], row['state']), (16384, 'READY'))
        self.assertTrue(self.product.list_ready()[0]['request_context_required'])
        self.assertIsNone(self.product.runtime.model_id)

    def test_existing_registration_is_preserved_without_native_load(self):
        self.ready()
        row = self.product.rows['model']
        self.product._save()
        with patch.object(service.threading, 'Thread') as thread:
            restarted = service.Product(self.root, Path(sys.executable), 'http://127.0.0.1:49011', self.root / 'data.token')
            thread.assert_not_called()
        self.assertEqual(restarted.get('model')['max_context_tokens'], 8192)
        self.assertEqual(restarted.register({k: v for k, v in self.spec.items() if k != 'parallel'})[0], 200)

    def test_runtime_settings_reject_invalid_and_unrepresentable_values(self):
        self.ready()
        for field, value in [('parallel', 0), ('parallel', -1), ('parallel', True),
                             ('parallel', '4'), ('max_context_tokens', False),
                             ('max_context_tokens', 0), ('parallel', 2147483648)]:
            with self.subTest(field=field, value=value), self.assertRaises(service.ProductError):
                self.product.update('model', {field: value})
        with self.assertRaises(service.ProductError):
            self.product.update('model', {'model_path': 'different.gguf'})
        self.assertEqual(self.product.get('model')['state'], 'READY')
        self.assertEqual(self.product.get('model')['revision'], 1)

    def test_update_publishes_new_binding_and_is_idempotent(self):
        self.ready()
        with patch.object(service.threading, 'Thread', ImmediateThread), patch.object(
                service, 'probe', return_value={'text_input': {}, 'text_output': {}}):
            status, row = self.product.update('model', {'max_context_tokens': 16384, 'expected_revision': 1})
        self.assertEqual(status, 202)
        self.assertEqual((row['state'], row['revision'], row['max_context_tokens']),
                         ('READY', 2, 16384))
        self.assertEqual(row['spec']['max_context_tokens'], 16384)
        self.assertEqual(self.product.update('model', {'max_context_tokens': 16384})[0], 200)
        with self.assertRaises(service.ProductError) as caught:
            self.product.update('model', {'max_context_tokens': 32768, 'expected_revision': 1})
        self.assertEqual(caught.exception.code, 'model_revision_conflict')
        self.assertIsNone(self.product.runtime.model_id)

    def test_failed_update_preserves_previous_ready_configuration(self):
        self.ready()
        self.product.runtime.error = 'insufficient_free_system_memory'
        with patch.object(service.threading, 'Thread', ImmediateThread):
            status, row = self.product.update('model', {'max_context_tokens': 32768})
        self.assertEqual(status, 202)
        self.assertEqual((row['state'], row['revision'], row['max_context_tokens']), ('READY', 1, 8192))
        self.assertEqual(row['update_error'], 'insufficient_free_system_memory')
        self.assertEqual(row['last_update']['state'], 'REJECTED')
        self.assertNotIn('pending_update', row)

    def test_pending_update_is_idempotent_conflicting_update_is_rejected(self):
        self.ready()
        with patch.object(service.threading, 'Thread') as thread:
            self.product.update('model', {'max_context_tokens': 16384})
            self.assertEqual(self.product.update('model', {'max_context_tokens': 16384})[0], 202)
            with self.assertRaises(service.ProductError) as caught:
                self.product.update('model', {'max_context_tokens': 32768})
            self.assertEqual(caught.exception.code, 'model_update_in_progress')
            self.assertEqual(thread.call_count, 1)
        with self.assertRaises(service.ProductError):
            self.handler()._proxy('/v1/chat/completions')

    def test_restart_during_update_recovers_old_ready_without_loading(self):
        self.ready()
        with patch.object(service.threading, 'Thread'):
            self.product.update('model', {'max_context_tokens': 4096})
        with patch.object(service.threading, 'Thread') as thread:
            restarted = service.Product(self.root, Path(sys.executable), 'http://127.0.0.1:49011', self.root / 'data.token')
            thread.assert_not_called()
        row = restarted.get('model')
        self.assertEqual((row['state'], row['max_context_tokens'], row['revision']),
                         ('READY', 8192, 1))
        self.assertEqual(row['update_error'], 'update_interrupted_by_restart')

    def test_update_drains_accepted_call_waiting_before_native_dispatch(self):
        self.ready()
        lifecycle = self.product.runtime.lifecycle
        lifecycle.acquire()
        try:
            with self.native('json'), ThreadPoolExecutor(max_workers=1) as workers, patch.object(
                    service, 'probe', return_value={'text_input': {}, 'text_output': {}}) as probe:
                request = workers.submit(self.handler()._proxy, '/v1/chat/completions')
                try:
                    deadline = time.monotonic() + 2
                    while not self.product.request_counts and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertEqual(self.product.request_counts, {('model', 1): 1})
                    self.product.update('model', {'max_context_tokens': 4096})
                    self.assertEqual(self.product.get('model')['state'], 'UPDATING')
                    probe.assert_not_called()
                finally:
                    lifecycle.release()
                    lifecycle = None
                request.result(timeout=3)
                deadline = time.monotonic() + 3
                while self.product.get('model')['state'] == 'UPDATING' and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(self.product.get('model')['revision'], 2)
        finally:
            if lifecycle is not None:
                lifecycle.release()
        receipt = self.receipt()
        self.assertEqual((receipt['state'], receipt['revision']), ('COMPLETED', 1))
        self.assertEqual(receipt['configuration'], {'max_context_tokens': 8192})
        self.assertFalse(self.product.request_counts)

    def test_native_start_uses_total_shared_context_and_expanded_slots(self):
        self.ready()
        row = dict(self.product.get('model'), max_context_tokens=8192)
        asset = Path(row['model_path'])
        row['assets'] = {'model_path': {'size': asset.stat().st_size,
            'mtime_ns': asset.stat().st_mtime_ns, 'sha256': service.digest(asset)}}
        runtime = service.Runtime(Path(sys.executable), self.root)
        def response(url, **kwargs):
            data = {'data': [{'meta': {'n_ctx_train': 262144}}]} if url.endswith('/models') else [{'n_ctx': 8192}] * 32
            result = io.BytesIO(json.dumps(data).encode())
            result.status = 200
            return result
        with patch.object(runtime, '_check_memory') as memory, patch.object(service.subprocess, 'Popen') as process, patch.object(
                service, 'urlopen', side_effect=response):
            process.return_value.poll.return_value = None
            try:
                runtime.start(row)
                args = process.call_args.args[0]
                self.assertEqual(args[args.index('--ctx-size') + 1], '8192')
                self.assertIn('--kv-unified', args)
                self.assertEqual(args[args.index('--gpu-layers') + 1], '0')
                self.assertEqual(args[args.index('--parallel') + 1], '32')
                self.assertEqual(args[args.index('--fit') + 1], 'off')
                self.assertEqual(runtime.capacity, {'native_slots': 32, 'total_context_tokens': 8192, 'shared_context': True, 'native_context_tokens': 8192})
                memory.assert_called_once_with(row, 8192, 32)
                runtime.start(row)
                self.assertEqual(process.call_count, 1)
                runtime.start(dict(row, revision=2))
                self.assertEqual(process.call_count, 2)
            finally:
                runtime.stop()

    def test_registration_paths_can_be_edited_with_a_new_revision(self):
        self.ready()
        replacement = self.root / 'replacement.gguf'
        replacement.write_bytes(b'GGUF-new')
        with patch.object(service.threading, 'Thread', ImmediateThread), patch.object(
                service, 'probe', return_value={'text_input': {}, 'text_output': {}}):
            self.product.update('model', {'model_path': str(replacement)})
        row = self.product.get('model')
        self.assertEqual((row['state'], row['revision']), ('READY', 2))
        self.assertEqual(row['model_path'], str(replacement))
        self.assertTrue(Path(self.spec['model_path']).exists())
        self.assertEqual(self.product.list_all()[0]['model_path'], str(replacement))

    def test_caller_can_use_more_than_an_equal_slice_of_shared_context(self):
        self.ready()
        handler = self.handler()
        payload = json.loads(handler._body())
        payload.update(context_tokens=5000, max_tokens=4000)
        handler._body = lambda: json.dumps(payload).encode()
        with self.native('json') as native:
            handler._proxy('/v1/chat/completions')
            self.assertNotIn('context_tokens', native.payload)
            self.assertEqual(native.payload['max_tokens'], 4000)
        self.assertEqual(self.receipt()['capacity']['reserved_context_tokens'], 5000)
        self.assertGreater(5000, 8192 / 4)
        self.assertEqual(self.product.runtime.pool.tokens, 0)

    def test_insufficient_caller_context_rejects_before_generation(self):
        self.ready()
        handler = self.handler()
        payload = json.loads(handler._body())
        payload['context_tokens'] = 18  # Actual input 3 plus requested output 16 needs 19.
        handler._body = lambda: json.dumps(payload).encode()
        with self.native('json') as native:
            with self.assertRaises(service.ProductError) as caught:
                handler._proxy('/v1/chat/completions')
            self.assertEqual(caught.exception.code, 'requested_context_insufficient')
            self.assertFalse(native.entered.is_set())
        self.assertEqual(self.receipt()['state'], 'FAILED')
        self.assertFalse(self.product.request_counts)

    def test_call_context_is_mandatory_and_never_inferred_from_registration(self):
        self.ready()
        for context in (None, 0, -1, True, '4096'):
            with self.subTest(context=context):
                handler = self.handler()
                payload = json.loads(handler._body())
                payload.pop('context_tokens')
                if context is not None:
                    payload['context_tokens'] = context
                handler._body = lambda: json.dumps(payload).encode()
                with self.assertRaises(service.ProductError) as caught:
                    handler._proxy('/v1/chat/completions')
                self.assertEqual(caught.exception.code, 'max_context_tokens_required')
                self.assertIsNone(self.product.runtime.model_id)
        self.assertFalse(self.product.request_counts)

    def test_pool_has_no_business_request_count_limit(self):
        pool = service.SharedContext(8192)
        for _ in range(8):
            pool.acquire(1024)
        self.assertEqual(pool.tokens, 8192)
        for _ in range(8):
            pool.release(1024)
        self.assertEqual(pool.tokens, 0)

    def test_context_gate_blocks_generation_before_native_dispatch(self):
        self.ready()
        waiting = threading.Event()
        acquire = self.product.runtime.pool.acquire
        def observed_acquire(tokens):
            if tokens == 4000:
                waiting.set()
            acquire(tokens)
        def caller(rid, context):
            handler = self.handler(request_id=rid, stream=True)
            payload = json.loads(handler._body())
            payload['context_tokens'] = context
            handler._body = lambda: json.dumps(payload).encode()
            handler._proxy('/v1/chat/completions')
        with self.native('gated') as native, ThreadPoolExecutor(max_workers=2) as workers, patch.object(
                self.product.runtime.pool, 'acquire', side_effect=observed_acquire):
            first = workers.submit(caller, 'quota-first', 5000)
            try:
                self.assertTrue(native.entered.wait(2))
                second = workers.submit(caller, 'quota-waiting', 4000)
                self.assertTrue(waiting.wait(2))
                self.assertEqual(len(native.generations), 1)
                self.assertEqual(self.product.runtime.pool.tokens, 5000)
                self.assertFalse(second.done())
            finally:
                native.release.set()
            first.result(timeout=3)
            second.result(timeout=3)
            self.assertEqual(len(native.generations), 2)
        self.assertEqual(self.product.runtime.pool.tokens, 0)
        for rid, context in [('quota-first', 5000), ('quota-waiting', 4000)]:
            self.assertEqual(self.receipt(rid)['state'], 'COMPLETED')
            self.assertEqual(self.receipt(rid)['capacity']['reserved_context_tokens'], context)

    def test_transcription_requires_and_reserves_caller_context(self):
        self.ready()
        self.product.rows['model']['capabilities'].append('audio_input')
        handler = self.handler()
        fields = {'model': 'model', 'context_tokens': '4000', 'max_tokens': '16'}
        body = ''.join(f'--boundary\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
                       for key, value in fields.items()).encode()
        body += b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="test.wav"\r\nContent-Type: audio/wav\r\n\r\nRIFF\r\n--boundary--\r\n'
        handler.headers['Content-Type'] = 'multipart/form-data; boundary=boundary'
        handler._body = lambda: body
        with self.native('json') as native:
            handler._proxy('/v1/audio/transcriptions')
            self.assertEqual(native.payload, body)
        self.assertEqual(self.receipt()['capacity']['reserved_context_tokens'], 4000)
        self.assertEqual(self.product.runtime.pool.tokens, 0)

    def test_shared_pool_waits_for_space_without_equal_slot_limits(self):
        pool = service.SharedContext(8192)
        pool.acquire(6000)
        entered, acquired = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            pool.acquire(3000)
            acquired.set()
            pool.release(3000)
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(blocked)
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(acquired.wait(.05))
                pool.acquire(1000)
                self.assertEqual(pool.tokens, 7000)
                pool.release(1000)
            finally:
                pool.release(6000)
            future.result(timeout=2)
        self.assertEqual(pool.tokens, 0)

    def test_native_capacity_shortfall_is_rejected_and_process_released(self):
        self.ready()
        row = dict(self.product.get('model'), max_context_tokens=8192)
        asset = Path(row['model_path'])
        row['assets'] = {'model_path': {'size': asset.stat().st_size,
            'mtime_ns': asset.stat().st_mtime_ns, 'sha256': service.digest(asset)}}
        runtime = service.Runtime(Path(sys.executable), self.root)
        def response(url, **kwargs):
            data = {'data': [{'meta': {'n_ctx_train': 262144}}]} if url.endswith('/models') else [{'n_ctx': 4096}] * 32
            result = io.BytesIO(json.dumps(data).encode())
            result.status = 200
            return result
        with patch.object(runtime, '_check_memory'), patch.object(service.subprocess, 'Popen') as process, patch.object(
                service, 'urlopen', side_effect=response):
            process.return_value.poll.return_value = None
            with self.assertRaises(service.ProductError) as caught:
                runtime.start(row)
            self.assertEqual(caught.exception.code, 'runtime_capacity_mismatch')
            process.return_value.terminate.assert_called_once()
            self.assertIsNone(runtime.process)

    def test_cpu_memory_estimate_respects_system_headroom(self):
        executable = self.root / 'llama-server.exe'
        executable.write_bytes(b'')
        executable.with_name('llama-fit-params.exe').write_bytes(b'')
        runtime = service.Runtime(executable, self.root)
        row = dict(self.spec, assets={})
        available = 5 << 30

        def memory_status(pointer):
            pointer._obj.total_physical = 8 << 30
            pointer._obj.available_physical = available
            return 1

        observed = SimpleNamespace(kernel32=SimpleNamespace(GlobalMemoryStatusEx=memory_status))
        estimate = SimpleNamespace(returncode=0, stdout='Host 1436 712 491\n')
        with patch.object(service.subprocess, 'run', return_value=estimate), patch.object(
                service.ctypes, 'windll', observed, create=True):
            runtime._check_memory(row, 8192, 32)
            available = 3 << 30
            with self.assertRaises(service.ProductError) as caught:
                runtime._check_memory(row, 8192, 32)
        self.assertEqual(caught.exception.code, 'insufficient_free_system_memory')

if __name__ == '__main__':
    unittest.main()
