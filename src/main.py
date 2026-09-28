"""Windows desktop and unattended entry point for the same application."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

from aitoolbox_app.application import Application, default_data_root
from aitoolbox_app.panel import Panel, enable_dpi_awareness


def main():
    parser = argparse.ArgumentParser(description="AIToolbox: connect your own models and providers")
    parser.add_argument("--data-dir", type=Path, default=default_data_root())
    parser.add_argument("--cloud-port", type=int)
    parser.add_argument("--local-port", type=int)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--stop-file", type=Path, help="In headless mode, stop gracefully when this file appears")
    parser.add_argument("--smoke-ui", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=args.data_dir / "application.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = None
    try:
        app = Application(args.data_dir, cloud_port=args.cloud_port, local_port=args.local_port)
        if args.headless:
            if sys.stdout:
                print(json.dumps({"cloud": app.cloud_url, "local": app.local_url, "data": str(app.root)}), flush=True)
            try:
                while args.stop_file is None or not args.stop_file.exists():
                    time.sleep(.2)
            except KeyboardInterrupt:
                pass
            return 0
        import tkinter as tk
        from tkinter import messagebox
        enable_dpi_awareness()
        root = tk.Tk()
        panel = Panel(root, app)
        closing = False
        finished = threading.Event()
        def close():
            nonlocal closing
            if closing:
                return
            closing = True
            panel.message.set("正在等待本产品调用结束并保存数据…")
            def work():
                try:
                    app.close()
                finally:
                    finished.set()
            threading.Thread(target=work, daemon=True).start()
            def check():
                if finished.is_set():
                    root.destroy()
                else:
                    root.after(100, check)
            root.after(100, check)
        root.protocol("WM_DELETE_WINDOW", close)
        if args.smoke_ui:
            def verify():
                result = {"title": root.title(), "geometry": root.geometry(),
                          "providers": len(panel.providers.get_children()),
                          "models": len(panel.models.get_children()),
                          "usage_days": len(panel.usage_series), "message": panel.message.get()}
                args.smoke_ui.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                close()
            root.after(2500, verify)
        root.mainloop()
        return 0
    except Exception as exc:
        logging.exception("application_failed")
        if args.headless:
            if sys.stderr:
                print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        else:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            message = str(exc)
            if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10048:
                message = "端口已被占用。请关闭已打开的本产品，或修改数据目录 settings.json 中的端口后重试。"
            messagebox.showerror("AIToolbox 无法启动", message + "\n\n日志位置：" + str(args.data_dir / "application.log"), parent=root)
            root.destroy()
        return 1
    finally:
        if app is not None:
            app.close()


if __name__ == "__main__":
    raise SystemExit(main())
