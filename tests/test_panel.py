from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from aitoolbox_app.application import Application
from aitoolbox_app.panel import Panel


@unittest.skipUnless(sys.platform == "win32", "Windows desktop")
class Desktop(unittest.TestCase):
    def test_provider_form_saves_updates_and_removes_only_selected_provider(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory(prefix="aitoolbox-ui-test-") as directory:
            app = Application(Path(directory), cloud_port=0, local_port=0, executable=Path(sys.executable))
            root = tk.Tk()
            root.withdraw()
            callback_errors = []
            root.report_callback_exception = lambda *error: callback_errors.append(error)
            try:
                panel = Panel(root, app)
                panel.provider_id.set("my-provider")
                panel.provider_url.set("https://api.example.test/v1")
                panel.provider_models.insert("1.0", "my-model\nmy-second-model")
                panel.key.set("test-key-not-a-real-secret")
                panel.save_key()
                def until(condition):
                    deadline = time.monotonic() + 10
                    while not condition() and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.02)
                    self.assertTrue(condition(), panel.message.get())
                until(lambda: len(panel.providers.get_children()) == 1)
                self.assertTrue(app.state.has_provider_key("my-provider"))
                self.assertEqual(panel.key.get(), "")
                selected = panel.providers.get_children()[0]
                panel.providers.selection_set(selected)
                panel.select_provider()
                panel.provider_models.delete("1.0", "end")
                panel.provider_models.insert("1.0", "changed-model")
                panel.save_key()
                until(lambda: app.provider_rows()[0]["models"] == ["changed-model"] and "已保存" in panel.message.get())
                self.assertEqual(app.state.provider_key("my-provider"), "test-key-not-a-real-secret")
                panel.providers.selection_set(panel.providers.get_children()[0])
                with patch("aitoolbox_app.panel.messagebox.askyesno", return_value=True):
                    panel.delete_provider()
                until(lambda: len(panel.providers.get_children()) == 0)
                self.assertFalse(app.state.has_provider_key("my-provider"))
                self.assertEqual(app.provider_rows(), [])
                until(lambda: len(panel.usage_series) == 7)
                root.update()
                self.assertEqual(callback_errors, [])
            finally:
                root.destroy()
                app.close()


if __name__ == "__main__":
    unittest.main()
