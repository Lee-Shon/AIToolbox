from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


_ID = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True)
class Provider:
    id: str
    base_url: str
    auth: str
    models: tuple[str, ...]
    credential_id: str
    static_headers: tuple[tuple[str, str], ...]


def load_providers(path: str | Path) -> dict[str, Provider]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != "aitoolbox.v9.cloud-relay/1":
        raise ValueError("unsupported_cloud_relay_config")
    providers: dict[str, Provider] = {}
    for item in raw.get("providers", []):
        name = item["id"]
        if not isinstance(name, str) or not _ID.fullmatch(name) or name in providers:
            raise ValueError("invalid_or_duplicate_provider_id")
        base = item["base_url"].rstrip("/")
        parsed = urlsplit(base)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback)) or not parsed.hostname:
            raise ValueError(f"provider_url_must_be_https: {name}")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"provider_url_contains_credentials_or_query: {name}")
        auth = item.get("auth", "bearer")
        if auth not in {"bearer", "x-api-key", "api-key", "doubao-speech"}:
            raise ValueError(f"unsupported_provider_auth: {name}")
        credential_id = item.get("credential_id", name)
        if not isinstance(credential_id, str) or not _ID.fullmatch(credential_id):
            raise ValueError(f"invalid_credential_id: {name}")
        raw_headers = item.get("static_headers", {})
        if not isinstance(raw_headers, dict) or any(
            not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9-]+", k)
            or k.lower() in {"authorization", "x-api-key", "api-key", "host"}
            or not isinstance(v, str) or "\r" in v or "\n" in v
            for k, v in raw_headers.items()
        ):
            raise ValueError(f"invalid_static_headers: {name}")
        models = item.get("models", [])
        if not isinstance(models, list) or any(not isinstance(m, str) or not m for m in models):
            raise ValueError(f"invalid_provider_models: {name}")
        if len(models) != len(set(models)):
            raise ValueError(f"duplicate_provider_models: {name}")
        providers[name] = Provider(name, base, auth, tuple(models), credential_id,
                                   tuple(raw_headers.items()))
    return providers
