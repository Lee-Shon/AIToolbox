import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from aitoolbox_app.application import Application


class Upstream(BaseHTTPRequestHandler):
    calls = 0
    def log_message(self, *args):
        pass
    def do_POST(self):
        type(self).calls += 1
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer test-provider-secret":
            self.send_error(401)
            return
        if payload.get("stream"):
            body = b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\ndata: [DONE]\n\n'
            content_type = "text/event-stream"
        else:
            body = json.dumps({"choices": [{"message": {"content": "OK"}}],
                               "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(sys.platform == "win32", "Windows DPAPI product")
class Integration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aitoolbox-unit-")
        self.root = Path(self.temp.name)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.app = Application(self.root, cloud_port=0, local_port=0)
        self.token = self.app.cloud_token_path.read_text()
        Upstream.calls = 0

    def tearDown(self):
        self.app.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, payload=None, rid=None, token=None):
        headers = {"Authorization": "Bearer " + (token or self.token), "Content-Type": "application/json"}
        if rid:
            headers["X-Request-Id"] = rid
        request = Request(self.app.cloud_url + path,
                          data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with urlopen(request, timeout=10) as response:
            return response.read()

    def configure(self):
        self.app.save_provider({"id": "custom", "base_url": f"http://127.0.0.1:{self.upstream.server_port}/v1",
                               "auth": "bearer", "models": ["user-model"]}, "test-provider-secret")

    def test_empty_first_start_duplicate_process_and_restart(self):
        self.assertEqual(self.app.provider_rows(), [])
        self.assertEqual(self.app.product.list_ready(), [])
        self.assertEqual(self.app.product.database_usage_day("2026-09-28"), {"day": "2026-09-28", "cloud": [], "local": []})
        with self.assertRaises(RuntimeError):
            Application(self.root, cloud_port=0, local_port=0)
        with self.assertRaises(HTTPError) as failure:
            self.request("/p/custom/v1/models", token="invalid")
        self.assertEqual(failure.exception.code, 401)
        self.app.close()
        self.app = Application(self.root, cloud_port=0, local_port=0)
        self.assertEqual(self.app.cloud_token_path.read_text(), self.token)

    def test_custom_provider_capture_idempotence_and_restart(self):
        self.configure()
        self.assertNotIn("test-provider-secret", self.app.config_path.read_text())
        response = json.loads(self.request("/p/custom/v1/chat/completions", {"model": "user-model", "messages": []}, "original"))
        self.assertEqual(response["choices"][0]["message"]["content"], "OK")
        with self.assertRaises(HTTPError) as duplicate:
            self.request("/p/custom/v1/chat/completions", {"model": "user-model", "messages": []}, "original")
        self.assertEqual(duplicate.exception.code, 409)
        self.assertEqual(Upstream.calls, 1)
        chain = json.loads(self.request("/requests/original"))
        self.assertEqual(chain["attempts"][-1]["state"], "COMPLETED")
        self.assertEqual(json.loads(self.request("/requests/original/result")), response)
        from datetime import datetime, timezone, timedelta
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        self.assertEqual(self.app.product.database_usage_day(day)["cloud"][0]["input_tokens"], 3)
        self.app.close()
        self.app = Application(self.root, cloud_port=0, local_port=0)
        self.assertEqual(json.loads(self.request("/requests/original/result")), response)
        self.assertEqual(Upstream.calls, 1)

    def test_stream_and_provider_removal_keep_result(self):
        self.configure()
        response = self.request("/p/custom/v1/chat/completions", {"model": "user-model", "stream": True}, "stream")
        self.assertIn(b"[DONE]", response)
        self.app.save_provider({"id": "custom"}, delete=True)
        self.assertFalse(self.app.state.has_provider_key("custom"))
        self.assertEqual(self.app.provider_rows(), [])
        self.assertEqual(self.request("/requests/stream/result"), response)

    def test_invalid_provider_does_not_replace_working_configuration(self):
        self.configure()
        old = self.app.config_path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.save_provider({"id": "bad", "base_url": "http://public.example/v1", "auth": "bearer", "models": []})
        self.assertEqual(self.app.config_path.read_bytes(), old)
        self.assertFalse(self.app.config_path.with_suffix(".pending.json").exists())


if __name__ == "__main__":
    unittest.main()
