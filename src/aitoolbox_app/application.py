"""Own the complete application lifetime and its private data directory."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
from urllib.parse import urlsplit

from aitoolbox_data import Catalog
from aitoolbox_data.cloud_adapter import cloud_config_id
from aitoolbox_data_service import DataServiceClient, DataServiceServer
from cloud_data_capture import Caller, CandidateServer, ManagedFiles, Provider
from cloud_data_capture.proxy import CandidateHandler
from cloud_relay.config import load_providers
from cloud_relay.state import State
from local_product.service import Server, save_json
from local_product.v11 import LocalAIClient, LocalAIProduct, V11Handler
from . import __version__

PROJECT = "aitoolbox"
SOURCE = "v10-cloud-capture"


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def default_data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AIToolbox"


class CloudHandler(CandidateHandler):
    def _dispatch(self):
        app = self.server.app
        with app.requests:
            app.active += 1
        try:
            path = urlsplit(self.path).path
            if self.command == "GET" and path.startswith("/requests/"):
                caller = app.authenticate(self._token())
                if caller is None:
                    return self._json(401, "invalid_gateway_key")
                tail = path[len("/requests/"):].split("/")
                if len(tail) > 2 or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", tail[0]):
                    return self._json(400, "invalid_request_id")
                chain = app.admin.get_chain(PROJECT, tail[0], caller_id=caller.caller_id)
                if chain is None:
                    return self._json(404, "request_not_found")
                if len(tail) == 1:
                    return self._write_json(200, chain)
                if tail[1] != "result":
                    return self._json(404, "unknown_route")
                attempts = chain.get("attempts", [])
                artifact_id = attempts[-1].get("result_artifact_id") if attempts else None
                artifact = next((a for a in chain.get("artifacts", [])
                                 if a["artifact_id"] == artifact_id), None)
                if artifact is None:
                    return self._json(409, "result_not_complete", tail[0])
                file = Path(artifact["uri"]).resolve(strict=True)
                if not file.is_relative_to(app.files.root.resolve()):
                    return self._json(500, "invalid_result_location")
                self.send_response_only(200)
                self.send_header("Content-Type", artifact.get("media_type") or "application/octet-stream")
                self.send_header("Content-Length", str(file.stat().st_size))
                self.send_header("Connection", "close")
                self.end_headers()
                with file.open("rb") as stream:
                    while block := stream.read(65536):
                        self.wfile.write(block)
                self.close_connection = True
                return
            return super()._dispatch()
        finally:
            with app.requests:
                app.active -= 1
                app.requests.notify_all()


class LocalHandler(V11Handler):
    def _handle(self):
        app = self.server.app
        with app.requests:
            app.active += 1
        try:
            return super()._handle()
        finally:
            with app.requests:
                app.active -= 1
                app.requests.notify_all()


class Application:
    def __init__(self, data_root: Path, *, cloud_port: int | None = None,
                 local_port: int | None = None):
        self.root = data_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.servers = []
        self.threads = []
        self.catalog = None
        self.product = None
        self.active = 0
        self.requests = threading.Condition()
        self.settings_lock = threading.RLock()
        self._closed = False
        self._owner = (self.root / "application.lock").open("a+b")
        self._owner.seek(0, os.SEEK_END)
        if self._owner.tell() == 0:
            self._owner.write(b"0")
            self._owner.flush()
        self._owner.seek(0)
        try:
            import msvcrt
            msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self._owner.close()
            raise RuntimeError("AIToolbox 已在使用这个数据目录，请回到已打开的窗口。") from exc
        try:
            self._start(cloud_port, local_port)
        except BaseException:
            self.close()
            raise

    def _serve(self, server):
        self.servers.append(server)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        self.threads.append(thread)
        thread.start()
        return server

    def _token(self, name: str) -> str:
        path = self.security / name
        if not path.exists():
            with path.open("x", encoding="ascii") as stream:
                stream.write(secrets.token_urlsafe(40))
        value = path.read_text(encoding="ascii").strip()
        if len(value) < 32:
            raise ValueError("调用凭据文件不完整：" + name)
        return value

    def _start(self, cloud_port, local_port):
        runtime = self.root / "runtime"
        self.security = runtime / "security"
        self.security.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "providers.json"
        if not self.config_path.exists():
            save_json(self.config_path, {"schema": "aitoolbox.v9.cloud-relay/1", "providers": []})
        self.settings_path = self.root / "settings.json"
        if not self.settings_path.exists():
            save_json(self.settings_path, {"cloud_port": 49777, "local_port": 49778})
        settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        cloud_port = settings["cloud_port"] if cloud_port is None else cloud_port
        local_port = settings["local_port"] if local_port is None else local_port
        for port in (cloud_port, local_port):
            if type(port) is not int or not 0 <= port <= 65535:
                raise ValueError("端口必须在 1 至 65535 之间。")
        self.state = State(runtime / "credentials.sqlite3")
        caller_path = self.security / "cloud-caller.token"
        if not caller_path.exists():
            if any(c["id"] == "client" for c in self.state.callers()):
                raise RuntimeError("云端调用凭据文件缺失，请从自己的备份恢复数据目录。")
            caller_path.write_text(self.state.add_caller("client"), encoding="ascii")
        self.cloud_token_path = caller_path
        runtime_token, admin_token = self._token("data-runtime.token"), self._token("data-admin.token")
        self.catalog = Catalog(runtime / "catalog" / "business.sqlite3", migrate=True)
        self.files = ManagedFiles(runtime / "files")
        self.data_server = DataServiceServer(("127.0.0.1", 0), catalog=self.catalog,
            file_root=self.files.root, runtime_token=runtime_token, admin_token=admin_token,
            local_product_source=runtime / "local-product", sync_interval_seconds=5)
        self._serve(self.data_server)
        data_url = "http://127.0.0.1:" + str(self.data_server.server_port)
        self.admin = DataServiceClient(data_url, admin_token)
        self.writer = DataServiceClient(data_url, runtime_token)
        self.admin.register_project(PROJECT, "AIToolbox")
        self.admin.register_source(SOURCE, "cloud", "AIToolbox managed capture", str(self.files.root), "single-writer catalog")
        for caller in self.state.callers():
            if not caller["revoked_at"]:
                self.admin.register_caller(caller["id"], PROJECT, "credentials:callers/" + caller["id"])
        providers = self._prepare_providers(self.config_path)
        self.cloud = CandidateServer(("127.0.0.1", cloud_port), providers=providers,
            authenticate=self.authenticate, resolve_credential=self.credential,
            catalog=self.writer, files=self.files, max_request_bytes=32 * 1024 * 1024)
        self.cloud.app = self
        self.cloud.RequestHandlerClass = CloudHandler
        self._serve(self.cloud)
        # Use the same V11 service as the main deployment, with this user's
        # own backend, key, asset mounts and data service.
        self._token("localai-api.key")
        assets = self.root / "models"
        assets.mkdir(exist_ok=True)
        backend = settings.get("localai", {})
        mounts = backend.get("asset_mounts", [{"host": str(assets), "target": "/models/assets-user"}])
        client = LocalAIClient(backend.get("url", "http://127.0.0.1:49779"),
                               self.security / "localai-api.key")
        self.product = LocalAIProduct(runtime / "local-product", data_url,
            self.security / "data-admin.token", client,
            [(Path(item["host"]), item["target"]) for item in mounts])
        self.local = Server(("127.0.0.1", local_port), self.product, LocalHandler)
        self.local.app = self
        self.local.RequestHandlerClass = LocalHandler
        self._serve(self.local)
        self.data_server.start_product_sync()
        self.cloud_url = "http://127.0.0.1:" + str(self.cloud.server_port)
        self.local_url = "http://127.0.0.1:" + str(self.local.server_port)
        save_json(self.root / "endpoints.json", {"cloud": self.cloud_url, "local": self.local_url,
                  "version": __version__, "pid": os.getpid()})

    def authenticate(self, token):
        caller = self.state.caller_for(token)
        return Caller(caller, PROJECT, caller == "admin") if caller else None

    def credential(self, name):
        if name.endswith(":speech"):
            return {"app-id": self.state.provider_key(name + ":app-id"),
                    "access-token": self.state.provider_key(name + ":access-token")}
        return self.state.provider_key(name)

    def _prepare_providers(self, path):
        parsed = load_providers(path)
        raw = path.read_bytes()
        revision = "config-" + hashlib.sha256(raw).hexdigest()
        writer = self.files.begin(PROJECT)
        try:
            writer.write(raw)
            ref = writer.publish()
        except BaseException:
            writer.abort()
            raise
        artifact_id = "cloud-config:" + ref.sha256
        self.admin.register_artifact(PROJECT, artifact_id, role="config_snapshot", media_type="application/json",
            uri=ref.path, sha256=ref.sha256, bytes=ref.size, source_id=SOURCE, recovery_state="managed")
        result = {}
        for name, provider in parsed.items():
            header, prefix = {"bearer": ("Authorization", "Bearer "), "x-api-key": ("x-api-key", ""),
                "api-key": ("api-key", ""), "doubao-speech": ("doubao-speech", "")}[provider.auth]
            result[name] = Provider(name, provider.base_url, provider.credential_id, revision,
                                    header, prefix, provider.models, provider.static_headers)
            self.admin.register_config(PROJECT, cloud_config_id(name, revision), model="*", software_version=__version__,
                revision=revision, sha256=ref.sha256, snapshot_artifact_id=artifact_id, source_id=SOURCE)
        return result

    def provider_rows(self):
        with self.settings_lock:
            return json.loads(self.config_path.read_text(encoding="utf-8"))["providers"]

    def save_provider(self, item, key="", *, delete=False):
        with self.settings_lock:
            rows = self.provider_rows()
            rows = [r for r in rows if r["id"] != item["id"]]
            if not delete:
                item = dict(item, credential_id=item["id"])
                rows.append(item)
            temporary = self.config_path.with_suffix(".pending.json")
            try:
                save_json(temporary, {"schema": "aitoolbox.v9.cloud-relay/1", "providers": rows})
                load_providers(temporary)
                providers = self._prepare_providers(temporary)
                if key and not delete:
                    self.state.set_provider_key(item["id"], key)
                os.replace(temporary, self.config_path)
                self.cloud.providers = providers
                if delete:
                    self.state.delete_provider_key(item["id"])
            finally:
                temporary.unlink(missing_ok=True)

    def close(self):
        if self._closed:
            return
        self._closed = True
        # Stop ingress first; keep the data service alive until all requests drain.
        for server in reversed(self.servers[1:]):
            server.shutdown()
        with self.requests:
            while self.active:
                self.requests.wait(timeout=.2)
        if self.product is not None:
            self.product.close()
        for server in reversed(self.servers[1:]):
            server.server_close()
        if self.servers:
            self.servers[0].shutdown()
            self.servers[0].server_close()
        for thread in self.threads:
            thread.join(timeout=5)
        if self.catalog is not None:
            self.catalog.close()
        if not self._owner.closed:
            import msvcrt
            self._owner.seek(0)
            msvcrt.locking(self._owner.fileno(), msvcrt.LK_UNLCK, 1)
            self._owner.close()
