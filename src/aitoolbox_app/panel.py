"""Minimal desktop control panel for AIToolbox cloud and local models."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def enable_dpi_awareness() -> None:
    """Let Windows render Tk text at the monitor's native resolution."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        if set_context(ctypes.c_void_p(-4)):  # Per-monitor DPI aware V2.
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.WinDLL("user32").SetProcessDPIAware()


class Panel:
    def __init__(self, root: tk.Tk, application):
        self.root = root
        self.application = application
        self.relay_state = application.state.path
        self.state_type = type(application.state)
        self.product_url = application.local_url
        self.token_file = application.product.token_path
        self.events: queue.Queue = queue.Queue()
        self.usage_series: list[tuple[str, dict, dict]] = []
        self.usage_generation = 0
        self.usage_placeholder = "正在读取用量…"
        root.title("AIToolbox · 模型中转与调用")
        dpi_scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.dpi_scale = dpi_scale
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(size=10)
        ttk.Style(root).configure("Treeview", rowheight=round(27 * dpi_scale))
        available_width = max(600, root.winfo_screenwidth() - 80)
        available_height = max(440, root.winfo_screenheight() - 100)
        width = min(round(900 * dpi_scale), available_width)
        height = min(round(660 * dpi_scale), available_height)
        root.geometry(f"{width}x{height}")
        root.minsize(min(round(760 * dpi_scale), available_width),
                     min(round(540 * dpi_scale), available_height))

        tabs = ttk.Notebook(root)
        tabs.pack(fill="both", expand=True, padx=12, pady=(12, 4))
        cloud = ttk.Frame(tabs, padding=16)
        local = ttk.Frame(tabs, padding=16)
        usage = ttk.Frame(tabs, padding=16)
        tabs.add(cloud, text="云厂商")
        tabs.add(local, text="本地模型管理")
        tabs.add(usage, text="用量")
        self.message = tk.StringVar(value="就绪")
        ttk.Label(root, textvariable=self.message, anchor="w").pack(fill="x", padx=16, pady=(0, 10))

        connections = ttk.Frame(tabs, padding=16)
        tabs.add(connections, text="接入与测试")
        self._cloud_tab(cloud)
        self._local_tab(local)
        self._usage_tab(usage)
        self._connections_tab(connections)
        self.refresh_models()
        self.refresh_usage()
        self.root.after(100, self._drain_events)

    def _drain_events(self) -> None:
        while True:
            try:
                done, result, error, failed = self.events.get_nowait()
            except queue.Empty:
                break
            if error is not None:
                if failed is not None:
                    failed(error)
                else:
                    self.message.set(error)
            else:
                done(result)
        self.root.after(100, self._drain_events)

    def background(self, work, done, failed=None) -> None:
        def run() -> None:
            try:
                result = work()
            except Exception as exc:
                self.events.put((done, None, "操作失败：" + str(exc)[:220], failed))
            else:
                self.events.put((done, result, None, failed))
        threading.Thread(target=run, daemon=True).start()

    def api(self, method: str, path: str, payload: dict | None = None) -> dict:
        token = self.token_file.read_text(encoding="ascii").strip()
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.product_url + path, data=data, method=method,
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                code = json.load(exc)["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = "request_failed"
            raise RuntimeError(f"本地 API {exc.code}: {code}") from exc
        except URLError as exc:
            raise RuntimeError("本地模型服务未连接") from exc

    def _cloud_tab(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="添加你自己的云厂商", font=("Segoe UI", 13)).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self.provider_id, self.provider_url = tk.StringVar(), tk.StringVar()
        self.provider_auth = tk.StringVar(value="OpenAI 兼容 / Bearer")
        self.key = tk.StringVar()
        for row, (label, variable) in enumerate((("厂商标识（英文小写）", self.provider_id),
                ("上游 API 根地址", self.provider_url), ("API Key（留空保留原密钥）", self.key)), start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            entry = ttk.Entry(frame, textvariable=variable, show="*" if row == 3 else "")
            entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Label(frame, text="协议 / 认证").grid(row=4, column=0, sticky="w")
        ttk.Combobox(frame, textvariable=self.provider_auth, state="readonly",
                     values=("OpenAI 兼容 / Bearer", "Anthropic / x-api-key", "其他 / api-key")).grid(row=4, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="模型名称（每行一个）").grid(row=5, column=0, sticky="nw", pady=5)
        self.provider_models = tk.Text(frame, height=3, width=50)
        self.provider_models.grid(row=5, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Label(frame, text="填写厂商给出的完整 API 根地址，通常以 /v1 结尾。名称目录只是提示，不会限制你调用其他模型。",
                  wraplength=760).grid(row=6, column=0, columnspan=3, sticky="w", pady=5)
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(buttons, text="保存厂商", command=self.save_key).pack(side="left")
        ttk.Button(buttons, text="新增 / 清空表单", command=self.clear_provider).pack(side="left", padx=8)
        ttk.Button(buttons, text="删除选中厂商", command=self.delete_provider).pack(side="left")
        self.providers = ttk.Treeview(frame, columns=("id", "url", "auth", "configured"), show="headings", height=7)
        for name, title, width in (("id", "厂商标识", 120), ("url", "上游地址", 380), ("auth", "认证", 100), ("configured", "密钥状态", 100)):
            self.providers.heading(name, text=title)
            self.providers.column(name, width=width)
        self.providers.grid(row=8, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(8, weight=1)
        self.providers.bind("<<TreeviewSelect>>", self.select_provider)
        self.refresh_key_status()

    def clear_provider(self):
        for variable in (self.provider_id, self.provider_url, self.key):
            variable.set("")
        self.provider_auth.set("OpenAI 兼容 / Bearer")
        self.provider_models.delete("1.0", "end")
        self.providers.selection_remove(self.providers.selection())

    def select_provider(self, _event=None):
        selected = self.providers.selection()
        if not selected:
            return
        name = self.providers.item(selected[0])["values"][0]
        row = next((r for r in self.application.provider_rows() if r["id"] == name), None)
        if row is None:  # A queued selection event can follow a background deletion.
            return
        self.provider_id.set(row["id"])
        self.provider_url.set(row["base_url"])
        self.provider_auth.set({"bearer": "OpenAI 兼容 / Bearer", "x-api-key": "Anthropic / x-api-key", "api-key": "其他 / api-key"}[row["auth"]])
        self.key.set("")
        self.provider_models.delete("1.0", "end")
        self.provider_models.insert("1.0", "\n".join(row["models"]))

    def refresh_key_status(self):
        self.providers.delete(*self.providers.get_children())
        for row in self.application.provider_rows():
            self.providers.insert("", "end", values=(row["id"], row["base_url"], row["auth"],
                "已配置" if self.application.state.has_provider_key(row["id"]) else "未配置"))
        if hasattr(self, "call_provider"):
            self.call_provider.configure(values=["本地模型"] + [r["id"] for r in self.application.provider_rows()])

    def save_key(self):
        if not self.provider_id.get().strip() or not self.provider_url.get().strip():
            self.message.set("请填写厂商标识和厂商提供的 API 根地址。")
            return
        auth = {"OpenAI 兼容 / Bearer": "bearer", "Anthropic / x-api-key": "x-api-key", "其他 / api-key": "api-key"}[self.provider_auth.get()]
        item = {"id": self.provider_id.get().strip(), "base_url": self.provider_url.get().strip(),
                "auth": auth, "models": [v.strip() for v in self.provider_models.get("1.0", "end").splitlines() if v.strip()]}
        if auth == "x-api-key":
            item["static_headers"] = {"anthropic-version": "2023-06-01"}
        key = self.key.get().strip()
        def done(_):
            self.key.set("")
            self.refresh_key_status()
            self.message.set("厂商已保存并生效；可在接入与测试页验证实际调用。")
        self.background(lambda: self.application.save_provider(item, key), done)

    def delete_provider(self):
        selected = self.providers.selection()
        if not selected:
            self.message.set("请先选择要删除的厂商。")
            return
        name = self.providers.item(selected[0])["values"][0]
        if not messagebox.askyesno("删除云厂商", "删除这个厂商及密钥？历史调用记录会保留。", parent=self.root):
            return
        def done(_):
            self.clear_provider()
            self.refresh_key_status()
            self.message.set("厂商已删除，历史记录保留。")
        self.background(lambda: self.application.save_provider({"id": name}, delete=True), done)

    def _connections_tab(self, frame):
        ttk.Label(frame, text="把地址、调用凭据和模型名填到你的 AI 客户端", font=("Segoe UI", 13)).pack(anchor="w")
        self.connection_info = tk.Text(frame, height=5, wrap="word")
        self.connection_info.pack(fill="x", pady=10)
        self.connection_info.insert("1.0", "云端：" + self.application.cloud_url + "/p/<厂商标识>/v1\n"
            + "本地：" + self.application.local_url + "/v1\n"
            + "Anthropic 客户端使用云端地址去掉末尾 /v1，客户端追加 /v1/messages。\n"
            + "调用凭据由本产品生成，与云厂商 API Key 不同；本地凭据也有模型管理权限。")
        self.connection_info.configure(state="disabled")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="复制云端调用凭据", command=lambda: self.copy_token(self.application.cloud_token_path)).pack(side="left")
        ttk.Button(buttons, text="复制本地调用凭据", command=lambda: self.copy_token(self.token_file)).pack(side="left", padx=8)
        ttk.Button(buttons, text="打开数据目录", command=lambda: __import__("os").startfile(self.application.root)).pack(side="left")
        ttk.Label(frame, text="测试调用会实际使用你选定的模型；云端费用由厂商计收。").pack(anchor="w", pady=(18, 6))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.call_target = tk.StringVar(value="本地模型")
        self.call_provider = ttk.Combobox(row, textvariable=self.call_target, state="readonly", width=22,
            values=["本地模型"] + [r["id"] for r in self.application.provider_rows()])
        self.call_provider.pack(side="left")
        self.call_model = tk.StringVar()
        ttk.Label(row, text="模型名").pack(side="left", padx=8)
        ttk.Entry(row, textvariable=self.call_model).pack(side="left", fill="x", expand=True)
        self.test_button = ttk.Button(row, text="发送测试请求", command=self.test_call)
        self.test_button.pack(side="left", padx=8)
        self.test_result = tk.Text(frame, height=12, wrap="word")
        self.test_result.pack(fill="both", expand=True, pady=10)
        ttk.Label(frame, text="本地用量最多约 5 秒后刷新。关闭窗口会停止本产品服务；数据与模型文件保留。").pack(anchor="w")

    def copy_token(self, path):
        value = path.read_text(encoding="ascii").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.message.set("已复制调用凭据，请仅粘贴到可信客户端。")

    def test_call(self):
        target, model = self.call_target.get(), self.call_model.get().strip()
        if not model:
            self.message.set("先填写已注册或云厂商提供的准确模型名。")
            return
        app = self.application
        payload = {"model": model, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 64}
        if target == "本地模型":
            url, token_file = app.local_url + "/v1/chat/completions", self.token_file
        else:
            provider = next((r for r in app.provider_rows() if r["id"] == target), None)
            if provider is None:
                self.message.set("请重新选择已保存的厂商。")
                return
            endpoint = "messages" if provider["auth"] == "x-api-key" else "chat/completions"
            url, token_file = app.cloud_url + "/p/" + target + "/v1/" + endpoint, app.cloud_token_path
        def work():
            if target == "本地模型":
                row = self.api("GET", "/admin/models/" + model)
                payload["context_tokens"] = min(2048, row["max_context_tokens"])
            request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json",
                "Authorization": "Bearer " + token_file.read_text(encoding="ascii").strip()})
            try:
                with urlopen(request, timeout=300) as response:
                    result = json.load(response)
                    return "请求 ID：" + str(response.headers.get("X-AIToolbox-Request-ID") or response.headers.get("X-Request-ID")) + "\n" + json.dumps(result, ensure_ascii=False, indent=2)
            except HTTPError as exc:
                return f"HTTP {exc.code}\n" + exc.read(4096).decode("utf-8", "replace")
        def done(result):
            self.test_button.configure(state="normal")
            self.test_result.delete("1.0", "end")
            self.test_result.insert("1.0", result)
            self.message.set("测试请求已返回；请查看结果。")
            self.refresh_usage()
        def failed(error):
            self.test_button.configure(state="normal")
            self.message.set(error)
        self.test_button.configure(state="disabled")
        self.message.set("正在等待模型回复…")
        self.background(work, done, failed)

    def _local_tab(self, frame: ttk.Frame) -> None:
        form = ttk.LabelFrame(frame, text="新增或修改模型注册", padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.model_id = tk.StringVar()
        self.model_path = tk.StringVar()
        self.mmproj_path = tk.StringVar()
        self.context = tk.StringVar(value="8192")
        self.model_rows = {}
        self.selected_model_id = None
        self.updating_model_id = None
        self.image = tk.BooleanVar(value=False)
        self.audio = tk.BooleanVar(value=False)
        fields = (("模型 ID", self.model_id), ("权重文件", self.model_path),
                  ("多模态投影文件", self.mmproj_path), ("实例总上下文 (token)", self.context))
        for row, (label, variable) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            ttk.Entry(form, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(form, text="浏览…", command=lambda: self.browse(self.model_path)).grid(row=1, column=2, padx=(8, 0))
        ttk.Button(form, text="浏览…", command=lambda: self.browse(self.mmproj_path)).grid(row=2, column=2, padx=(8, 0))
        options = ttk.Frame(form)
        options.grid(row=4, column=1, sticky="w", pady=(6, 4))
        ttk.Label(options, text="文本输入/输出已包含；额外启用：").pack(side="left")
        ttk.Checkbutton(options, text="图片输入", variable=self.image).pack(side="left", padx=8)
        ttk.Checkbutton(options, text="语音输入", variable=self.audio).pack(side="left", padx=8)
        actions = ttk.Frame(form)
        actions.grid(row=5, column=1, sticky="w", pady=(10, 0))
        ttk.Button(actions, text="注册并验证", command=self.register_model).pack(side="left")
        ttk.Button(actions, text="修改注册", command=self.update_model).pack(side="left", padx=12)
        ttk.Label(form, text="总上下文用于实例资源预估。每次调用必须填写最大上下文，按申请额度共享总容量，不按路数平分。",
                  wraplength=650).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(16, 6))
        ttk.Label(toolbar, text="已登记模型（含失败和已取消）").pack(side="left")
        ttk.Button(toolbar, text="删除注册", command=self.unregister_model).pack(side="right", padx=(8, 0))
        ttk.Button(toolbar, text="刷新", command=self.refresh_models).pack(side="right")
        columns = ("id", "state", "capabilities", "context", "revision", "error")
        self.models = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        for key, title, width in (("id", "模型 ID", 220), ("state", "状态", 90),
                                  ("capabilities", "能力", 210), ("context", "总上下文", 90),
                                  ("revision", "修订", 60), ("error", "错误", 230)):
            self.models.heading(key, text=title)
            self.models.column(key, width=width, stretch=key in ("id", "capabilities", "error"))
        self.models.pack(fill="both", expand=True)
        self.models.bind("<<TreeviewSelect>>", self.select_model)

    def browse(self, variable: tk.StringVar) -> None:
        path = filedialog.askopenfilename(parent=self.root, title="选择已有模型文件",
                                          initialdir=str(Path.home()),
                                          filetypes=[("GGUF 模型", "*.gguf"), ("所有文件", "*.*")])
        if path:
            variable.set(path)

    def register_model(self) -> None:
        try:
            context = int(self.context.get().strip())
        except ValueError:
            self.message.set("总上下文必须是整数")
            return
        model_id, model_path, mmproj_path = (self.model_id.get().strip(), self.model_path.get().strip(),
                                             self.mmproj_path.get().strip())
        if not model_id or not model_path:
            self.message.set("请填写模型 ID 和权重文件")
            return
        if (self.image.get() or self.audio.get()) and not mmproj_path:
            self.message.set("图片或语音输入需要多模态投影文件")
            return
        capabilities = ["text_input", "text_output"]
        if self.image.get():
            capabilities.append("image_input")
        if self.audio.get():
            capabilities.append("audio_input")
        payload = {"id": model_id, "model_path": model_path,
                   "mmproj_path": mmproj_path or None, "capabilities": capabilities,
                   "max_context_tokens": context}
        self.message.set("注册已提交，正在验证模型能力…")
        def done(row: dict) -> None:
            self.message.set(f"{row['id']}：{row['state']}")
            self.refresh_models()
            if row["state"] == "VALIDATING":
                self.root.after(3000, self.refresh_models)
        self.background(lambda: self.api("POST", "/admin/models", payload), done)

    def update_model(self) -> None:
        selected = self.models.selection()
        if not selected:
            self.message.set("先选中需要修改的模型")
            return
        model_id = self.models.item(selected[0], "values")[0]
        if self.model_id.get().strip() != model_id:
            self.message.set("修改注册时模型 ID 保持不变；新 ID 请用注册并验证")
            return
        try:
            payload = {"max_context_tokens": int(self.context.get().strip()),
                       "model_path": self.model_path.get().strip(),
                       "mmproj_path": self.mmproj_path.get().strip() or None,
                       "capabilities": ["text_input", "text_output"] +
                                       (["image_input"] if self.image.get() else []) +
                                       (["audio_input"] if self.audio.get() else []),
                       "expected_revision": self.model_rows[model_id]["revision"]}
        except (ValueError, KeyError):
            self.message.set("总上下文必须是整数，请刷新模型列表后重试")
            return
        self.message.set("正在提交配置；已有请求完成后验证新配置…")
        def done(row: dict) -> None:
            self.message.set(f"{row['id']}：{row['state']}；" +
                             (row.get("update_error") or "新配置验证期间暂不接新请求"))
            self.updating_model_id = model_id
            self.selected_model_id = None
            self.refresh_models()
        self.background(lambda: self.api("PATCH", "/admin/models/" + model_id, payload), done)

    def refresh_models(self) -> None:
        def done(result: dict) -> None:
            selected = self.models.selection()
            selected_id = self.models.item(selected[0], "values")[0] if selected else None
            for item in self.models.get_children():
                self.models.delete(item)
            validating = False
            self.model_rows = {row["id"]: row for row in result.get("data", [])}
            updating = self.model_rows.get(self.updating_model_id)
            if updating and updating["state"] != "UPDATING":
                if updating.get("update_error"):
                    self.message.set("修改失败，原配置已保留：" + updating["update_error"])
                else:
                    self.message.set(f"配置已生效：实例总上下文 {updating['max_context_tokens']} token")
                self.updating_model_id = None
                self.selected_model_id = None
            for row in result.get("data", []):
                self.models.insert("", "end", values=(row["id"], row["state"],
                                   ", ".join(row["capabilities"]), row["max_context_tokens"],
                                   row["revision"],
                                   row.get("error") or row.get("update_error") or ""))
                validating |= row["state"] in {"VALIDATING", "UPDATING"}
            if selected_id:
                for item in self.models.get_children():
                    if self.models.item(item, "values")[0] == selected_id:
                        self.models.selection_set(item)
                        break
            if validating:
                self.root.after(3000, self.refresh_models)
        self.background(lambda: self.api("GET", "/admin/models"), done)

    def select_model(self, _event=None) -> None:
        selected = self.models.selection()
        if selected:
            model_id = self.models.item(selected[0], "values")[0]
            if self.selected_model_id == model_id:
                return
            self.selected_model_id = model_id
            self.model_id.set(model_id)
            row = self.model_rows.get(model_id, {})
            settings = (row.get("pending_update") or {}).get("spec", row)
            self.context.set(str(settings.get("max_context_tokens", 8192)))
            self.model_path.set(settings.get("model_path", ""))
            self.mmproj_path.set(settings.get("mmproj_path") or "")
            self.image.set("image_input" in settings.get("capabilities", []))
            self.audio.set("audio_input" in settings.get("capabilities", []))

    def unregister_model(self) -> None:
        selected = self.models.selection()
        if not selected:
            self.message.set("先选中一条模型注册")
            return
        model_id = self.models.item(selected[0], "values")[0]
        if not messagebox.askyesno("删除注册", f"取消 {model_id} 的注册？\n原模型文件和历史请求会保留。",
                                   parent=self.root):
            return
        self.message.set("正在安全排空并取消注册…")
        def done(row: dict) -> None:
            self.message.set(f"{row['id']}：{row['state']}；原模型文件已保留")
            self.refresh_models()
        self.background(lambda: self.api("DELETE", "/admin/models/" + model_id), done)

    def _usage_tab(self, frame: ttk.Frame) -> None:
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="截止日期（北京时间）").pack(side="left")
        self.day = tk.StringVar(value=datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"))
        ttk.Entry(top, textvariable=self.day, width=14).pack(side="left", padx=8)
        ttk.Button(top, text="刷新用量", command=self.refresh_usage).pack(side="left")
        ttk.Label(top, text="柱状图：").pack(side="left", padx=(20, 4))
        self.usage_metric = tk.StringVar(value="调用次数")
        metric = ttk.Combobox(top, textvariable=self.usage_metric, state="readonly", width=12,
                              values=("调用次数", "输入 token", "输出 token", "未计量调用"))
        metric.pack(side="left")
        metric.bind("<<ComboboxSelected>>", lambda _event: self.draw_usage_chart())
        ttk.Label(frame, text="柱状图为近 7 天，下表为截止日明细。来源：业务数据库；本地收据约每分钟同步，未知 token 不估算。",
                  wraplength=round(820 * self.dpi_scale)).pack(anchor="w", pady=(0, 6))
        self.chart = tk.Canvas(frame, background="white", highlightthickness=1,
                               highlightbackground="#d2d9e0", height=round(215 * self.dpi_scale))
        self.chart.pack(fill="x", pady=(0, 12))
        self.chart.bind("<Configure>", lambda _event: self.draw_usage_chart())
        columns = ("source", "caller", "model", "calls", "input", "output", "unknown")
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        self.usage = ttk.Treeview(table, columns=columns, show="headings", height=7)
        for key, title, width in (("source", "来源", 80), ("caller", "调用方/厂商", 200),
                                  ("model", "模型", 210), ("calls", "调用", 70),
                                  ("input", "输入 token", 95), ("output", "输出 token", 95),
                                  ("unknown", "未计量", 75)):
            self.usage.heading(key, text=title)
            self.usage.column(key, width=width, stretch=key in ("caller", "model"))
        self.usage.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.usage.yview)
        self.usage.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

    @staticmethod
    def _usage_totals(cloud: list[dict], local: list[dict]) -> tuple[dict, dict]:
        cloud_totals = {
            "调用次数": sum(row["calls"] for row in cloud),
            "输入 token": sum(row["input_tokens"] or 0 for row in cloud),
            "输出 token": sum(row["output_tokens"] or 0 for row in cloud),
            "未计量调用": sum(row["unknown_calls"] for row in cloud),
        }
        local_totals = {
            "调用次数": sum(row["calls"] for row in local),
            "输入 token": sum(row["input_tokens"] or 0 for row in local),
            "输出 token": sum(row["output_tokens"] or 0 for row in local),
            "未计量调用": sum(row["calls"] - row["usage_known_calls"] for row in local),
        }
        return cloud_totals, local_totals

    def draw_usage_chart(self) -> None:
        chart = self.chart
        chart.delete("all")
        scale = self.dpi_scale
        width, height = chart.winfo_width(), chart.winfo_height()
        if width < 100 or height < 100:
            return
        if not self.usage_series:
            chart.create_text(width / 2, height / 2, text=self.usage_placeholder, fill="#667085")
            return
        metric = self.usage_metric.get()
        left, right = round(70 * scale), round(18 * scale)
        top, bottom = round(42 * scale), round(36 * scale)
        baseline = height - bottom
        plot_height = baseline - top
        maximum = max((max(cloud[metric], local[metric]) for _, cloud, local in self.usage_series),
                      default=0)
        ceiling = max(1, maximum)
        chart.create_text(left, round(17 * scale), anchor="w", text=f"近 7 天 · {metric}",
                          fill="#243447")
        chart.create_rectangle(width - round(145 * scale), round(12 * scale),
                               width - round(134 * scale), round(23 * scale), fill="#3976c6", outline="")
        chart.create_text(width - round(130 * scale), round(18 * scale), anchor="w",
                          text="云端", fill="#344054")
        chart.create_rectangle(width - round(75 * scale), round(12 * scale),
                               width - round(64 * scale), round(23 * scale), fill="#2c9b70", outline="")
        chart.create_text(width - round(60 * scale), round(18 * scale), anchor="w",
                          text="本地", fill="#344054")
        for ratio in ((0, 0.5, 1) if ceiling > 1 else (0, 1)):
            y = baseline - plot_height * ratio
            chart.create_line(left, y, width - right, y, fill="#e4e9ef")
            chart.create_text(left - round(8 * scale), y, anchor="e",
                              text=self._axis_label(round(ceiling * ratio)), fill="#667085")
        span = (width - left - right) / len(self.usage_series)
        bar_width = min(round(23 * scale), span * 0.27)
        for index, (day, cloud, local) in enumerate(self.usage_series):
            center = left + span * (index + 0.5)
            for value, x1, color in ((cloud[metric], center - bar_width - 2, "#3976c6"),
                                     (local[metric], center + 2, "#2c9b70")):
                if value:
                    y = baseline - plot_height * value / ceiling
                    chart.create_rectangle(x1, y, x1 + bar_width, baseline,
                                           fill=color, outline="")
                    chart.create_text(x1 + bar_width / 2, max(top, y - round(8 * scale)),
                                      text=f"{value:,}", fill="#344054")
            chart.create_text(center, baseline + round(15 * scale),
                              text=day[5:], fill="#475467")

    @staticmethod
    def _axis_label(value: int) -> str:
        return f"{value / 10000:.1f}万" if value >= 10000 else f"{value:,}"

    def refresh_usage(self) -> None:
        day = self.day.get().strip()
        try:
            if datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                raise ValueError
        except ValueError:
            self.message.set("日期格式应为 YYYY-MM-DD")
            return
        self.usage_generation += 1
        generation = self.usage_generation
        self.usage_series = []
        self.usage_placeholder = "正在读取业务数据库用量…"
        self.draw_usage_chart()
        for item in self.usage.get_children():
            self.usage.delete(item)
        self.message.set("正在读取业务数据库用量…")
        def work():
            last = datetime.strptime(day, "%Y-%m-%d")
            series = []
            for offset in range(6, -1, -1):
                current_day = (last - timedelta(days=offset)).strftime("%Y-%m-%d")
                rows = self.api("GET", "/admin/usage-dashboard?" + urlencode({"day": current_day}))
                series.append((current_day, rows["cloud"], rows["local"]))
            return series
        def done(series) -> None:
            if generation != self.usage_generation:
                return
            self.usage_series = [(date, *self._usage_totals(cloud, local))
                                 for date, cloud, local in series]
            self.draw_usage_chart()
            for item in self.usage.get_children():
                self.usage.delete(item)
            _, cloud, local = series[-1]
            for row in cloud:
                self.usage.insert("", "end", values=("云端", f"{row['caller']} / {row['provider']}",
                                  row.get("model") or "—", row["calls"],
                                  row["input_tokens"] if row["input_tokens"] is not None else "—",
                                  row["output_tokens"] if row["output_tokens"] is not None else "—",
                                  row["unknown_calls"]))
            for row in local:
                self.usage.insert("", "end", values=("本地", "本地 API", row["model"],
                                  row["calls"], row["input_tokens"] if row["input_tokens"] is not None else "—",
                                  row["output_tokens"] if row["output_tokens"] is not None else "—",
                                  row["calls"] - row["usage_known_calls"]))
            self.message.set(f"{day}：云端 {len(cloud)} 组，本地 {len(local)} 个模型")
        def failed(error: str) -> None:
            if generation == self.usage_generation:
                self.usage_placeholder = "数据库用量读取失败"
                self.draw_usage_chart()
                self.message.set(error)
        self.background(work, done, failed)
