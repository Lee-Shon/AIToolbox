"""V11 model-management invariants without a GPU or production state."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from local_product.service import ProductError
from local_product.v11 import LocalAIProduct, V11Handler, config_hash, config_matches


class V11Regressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ait-v11-test-")
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        token = self.root / "data.token"
        token.write_text("t" * 40, encoding="ascii")
        self.client = SimpleNamespace()
        self.product = LocalAIProduct(
            self.root / "product", "http://127.0.0.1:49011",
            token, self.client, [(self.assets, "/models/assets-e")])

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def spec(model_id, path, caps):
        return {"id": model_id, "model_path": str(path), "capabilities": caps,
                "max_context_tokens": 4096}

    def test_localai_default_expansion_does_not_change_binding(self):
        before = {"backend": "cuda12-llama-cpp", "reasoning": {},
                  "parameters": {"model": "assets-e/model.gguf"}}
        after = {**before, "reasoning": {
            "disable_reasoning_tag_prefill": True, "disable": False}}
        self.assertEqual(config_hash(before), config_hash(after))
        false_default = {**after, 'reasoning': {'disable_reasoning_tag_prefill': False, 'disable': False}}
        self.assertTrue(config_matches(false_default, config_hash(before)))
        self.assertTrue(config_matches(false_default, config_hash(false_default)))
        self.assertFalse(config_matches(after, config_hash(false_default)))
        self.assertNotEqual(config_hash(before), config_hash({
            **after, "parameters": {"model": "assets-e/other.gguf"}}))

    def test_safetensors_and_gguf_map_to_readonly_assets(self):
        for name, architecture, caps in (
            ("glm", "GlmAsrForConditionalGeneration", ["audio_input", "text_output"]),
            ("ocr", "DeepseekOCR2ForCausalLM", ["image_input", "text_output"]),
        ):
            directory = self.assets / name
            directory.mkdir()
            (directory / "config.json").write_text(json.dumps({"architectures": [architecture]}))
            (directory / "model.safetensors").write_bytes(b"test")
            spec = self.product.registration_spec(self.spec(name, directory, caps))
            self.assertEqual(spec["backend"], "cuda13-vllm")
            self.assertEqual(spec["localai_model"], f"assets-e/{name}")
            self.assertEqual(set(spec["capabilities"]), set(caps))
            self.assertTrue(self.product._assets(spec)["model_path"])
        gguf = self.assets / "omni.gguf"
        projector = self.assets / "projector.gguf"
        gguf.write_bytes(b"GGUF")
        projector.write_bytes(b"GGUF")
        body = {**self.spec("omni", gguf, ["text_input", "image_input", "text_output"]),
                "mmproj_path": str(projector)}
        spec = self.product.registration_spec(body)
        self.assertEqual(spec["backend"], "cuda12-llama-cpp")
        self.assertEqual(spec["tuning"]["mmproj"], "assets-e/projector.gguf")

    def test_bad_model_inputs_are_client_errors(self):
        bad = self.spec("missing", self.assets / "missing.gguf",
                        ["text_input", "text_output"])
        with self.assertRaisesRegex(ProductError, "model_asset_missing"):
            self.product.registration_spec(bad)
        bad["capabilities"] = [{"invalid": True}, "text_output"]
        with self.assertRaisesRegex(ProductError, "invalid_capabilities"):
            self.product.registration_spec(bad)

    def test_ready_binding_rejects_modified_asset(self):
        model = self.assets / "model.gguf"
        model.write_bytes(b"GGUF")
        spec = self.product.registration_spec(self.spec(
            "model", model, ["text_input", "text_output"]))
        config = {"backend": "cuda12-llama-cpp"}
        self.client.config = lambda _name: config
        row = {**spec, "spec": spec, "revision": 1, "state": "READY",
               "localai_name": "model", "localai_hash": config_hash(config),
               "assets": self.product._assets(spec)}
        self.product.rows["model"] = row
        model.write_bytes(b"GGUF changed")
        result = self.product.get("model")
        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(result["error"], "model_asset_changed")

    def test_text_probe_must_prove_text_only_ability(self):
        def chat(_name, content, _max_tokens=96, **kwargs):
            answer = "AIT V11 OCR 7319" if isinstance(content, list) else "unrelated description"
            return {"choices": [{"message": {"content": answer}}]}
        self.client.chat = chat
        row = {"localai_name": "ocr", "backend": "cuda13-vllm", "max_context_tokens": 4096,
               "capabilities": ["image_input", "text_output"]}
        self.assertEqual(set(self.product._probe(row)), set(row["capabilities"]))
        row["capabilities"] = ["text_input", "image_input", "text_output"]
        with self.assertRaisesRegex(ProductError, "text_probe_wrong_answer"):
            self.product._probe(row)

    def _updating_row(self):
        old_spec = self.spec("model", self.assets / "old.gguf",
                             ["text_input", "text_output"])
        new_spec = {**old_spec, "max_context_tokens": 8192}
        row = {**old_spec, "spec": old_spec, "revision": 1, "state": "UPDATING",
               "localai_name": "model", "localai_hash": "old-hash",
               "pending_update": {"spec": new_spec, "revision": 2,
                                  "previous_state": "READY"},
               "updated_at": time.time()}
        self.product.rows["model"] = row
        return row

    def test_failed_candidate_keeps_old_revision(self):
        old = self._updating_row()
        self.product._validate_row = lambda row: {**row, "state": "REJECTED",
                                                   "error": "probe_failed"}
        retired = []
        self.client.retire = retired.append
        self.product._apply_update("model", 2)
        self.assertIs(self.product.rows["model"], old)
        self.assertEqual((old["state"], old["revision"]), ("READY", 1))
        self.assertEqual(old["last_update"]["error"], "probe_failed")
        self.assertEqual(len(retired), 1)

    def test_successful_candidate_promotes_only_after_probe(self):
        self._updating_row()
        self.product._validate_row = lambda row: {**row, "state": "READY",
                                                   "error": None,
                                                   "localai_hash": "candidate-hash"}
        self.client.shutdown = lambda _name: None
        self.client.import_model = lambda _name, _spec: "new-hash"
        retired = []
        self.client.retire = retired.append
        self.product._apply_update("model", 2)
        row = self.product.rows["model"]
        self.assertEqual((row["state"], row["revision"]), ("READY", 2))
        self.assertEqual(row["localai_name"], "model")
        self.assertEqual(row["localai_hash"], "new-hash")
        self.assertEqual(len(retired), 1)

    def test_rejected_before_import_can_be_unregistered(self):
        spec = self.spec("invalid", self.assets / "not-a-model.bin",
                         ["text_input", "text_output"])
        self.product.rows["invalid"] = {**spec, "spec": spec, "revision": 1,
                                        "state": "REJECTED", "error": "unsupported_format"}
        row = self.product.unregister("invalid")
        self.assertEqual(row["state"], "REMOVED")
        self.assertNotIn("localai_name", row)

    def test_multimodal_call_cannot_exceed_instance_context(self):
        row = {"state": "READY", "capabilities": ["image_input", "text_output"],
               "max_context_tokens": 4096}
        handler = object.__new__(V11Handler)
        handler.server = SimpleNamespace(product=SimpleNamespace(get=lambda _id: row))
        handler.headers = {}
        handler._body = lambda: json.dumps({
            "model": "ocr", "messages": [{"role": "user", "content": [
                {"type": "text", "text": "OCR"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ]}], "context_tokens": 4097, "max_tokens": 64,
        }).encode()
        with self.assertRaisesRegex(ProductError, "request_exceeds_instance_capacity"):
            handler._proxy("/v1/chat/completions")

    def test_unregister_drains_accepted_call_before_retirement(self):
        spec = self.spec("model", self.assets / "model.gguf",
                         ["text_input", "text_output"])
        row = {**spec, "spec": spec, "revision": 1, "state": "READY",
               "localai_name": "model"}
        self.product.rows["model"] = row
        self.product.request_counts[("model", 1)] = 1
        retired = []
        self.client.retire = retired.append
        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(self.product.unregister, "model")
            deadline = time.monotonic() + 2
            while row["state"] != "DRAINING" and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(row["state"], "DRAINING")
            self.assertFalse(result.done())
            self.assertFalse(retired)
            self.product.release_request(row)
            self.assertEqual(result.result(timeout=2)["state"], "REMOVED")
        self.assertEqual(retired, ["model"])


if __name__ == "__main__":
    unittest.main()
