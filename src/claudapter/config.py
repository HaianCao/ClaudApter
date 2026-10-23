from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ClaudapterError


@dataclass
class Provider:
    id: str
    display_name: str
    base_url: str
    auth_mode: str = "bearer"
    default_model: str = ""
    execution_mode: str = "direct"
    protocol: str = "messages"
    upstream_reasoning: str = "provider_default"


@dataclass
class Config:
    providers: dict[str, Provider] = field(default_factory=dict)
    default_provider: str = ""
    default_model: str = ""
    default_effort: str = "default"
    path: Path | None = None


EFFORTS = {"default", "low", "medium", "high", "xhigh", "max"}


def config_path() -> Path:
    override = os.environ.get("CLAUDE_CA_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    root = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    return root / "claudapter" / "config.toml"


def cache_path(config: Config | None = None) -> Path:
    override = os.environ.get("CLAUDE_CA_CACHE")
    if override:
        return Path(override).expanduser().resolve()
    if config and config.path and config.path != config_path():
        return config.path.with_name("models-cache.json")
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache"))
    return root / "claudapter" / "models.json"


def validate_provider_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        raise ClaudapterError("Provider ID must contain lowercase letters, digits, _ or -.")
    return value


def secret_var(provider_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", validate_provider_id(provider_id)).upper() + "_TOKEN"


def validate_base_url(value: str, protocol: str = "messages") -> str:
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ClaudapterError("Base URL has an invalid host or port.") from exc
    local_http = parsed.scheme == "http" and hostname in {"127.0.0.1", "localhost", "::1"}
    if re.search(r"\s", value) or (parsed.scheme != "https" and not local_http) or not hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or (port is not None and port < 1):
        raise ClaudapterError("Base URL must use HTTPS (or loopback HTTP) without credentials, query or fragment.")
    if parsed.path.endswith(("/messages", "/responses", "/chat/completions")):
        raise ClaudapterError("Enter the base URL without the endpoint suffix.")
    if protocol == "messages" and parsed.path.endswith("/v1"):
        raise ClaudapterError("Enter the gateway base URL before /v1; Claude Code appends /v1/messages itself.")
    return value


def validate(config: Config) -> None:
    if config.default_effort not in EFFORTS:
        raise ClaudapterError("Invalid effort level.")
    used_secret_names: set[str] = set()
    for provider_id, provider in config.providers.items():
        validate_provider_id(provider_id)
        if provider.id != provider_id or not provider.display_name.strip():
            raise ClaudapterError(f"Invalid provider: {provider_id}")
        if provider.protocol not in {"messages", "chat", "responses"}:
            raise ClaudapterError(f"Invalid protocol for {provider_id}.")
        validate_base_url(provider.base_url, provider.protocol)
        if provider.auth_mode not in {"bearer", "api_key"}:
            raise ClaudapterError(f"Invalid auth mode for {provider_id}.")
        if provider.execution_mode not in {"direct", "gateway"}:
            raise ClaudapterError(f"Invalid execution mode for {provider_id}.")
        if provider.upstream_reasoning not in ("provider_default", "disabled", "openrouter_disabled") or (provider.execution_mode != "gateway" and provider.upstream_reasoning != "provider_default"):
            raise ClaudapterError("An explicit upstream reasoning policy requires gateway mode.")
        if provider.protocol != "messages" and provider.execution_mode != "gateway":
            raise ClaudapterError("Chat and Responses providers require local Protoverta gateway mode.")
        name = secret_var(provider_id)
        if name in used_secret_names:
            raise ClaudapterError(f"Provider IDs collide on the same secret name: {name}.")
        used_secret_names.add(name)
    if config.default_provider and config.default_provider not in config.providers:
        raise ClaudapterError("Default provider does not exist.")


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config(path=path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1:
            raise ClaudapterError("Unsupported config schema version.")
        defaults = raw.get("defaults", {})
        providers = {}
        for item in raw.get("providers", []):
            provider = Provider(**item)
            if provider.id in providers:
                raise ClaudapterError(f"Duplicate provider: {provider.id}")
            providers[provider.id] = provider
        config = Config(providers, defaults.get("provider", ""), defaults.get("model", ""), defaults.get("effort", "default"), path)
        validate(config)
        return config
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError, KeyError, ValueError) as exc:
        raise ClaudapterError(f"Cannot load config: {exc}") from exc


def save(config: Config) -> None:
    validate(config)
    path = config.path or config_path()
    lines = ["schema_version = 1", "", "[defaults]", f"provider = {json.dumps(config.default_provider)}", f"model = {json.dumps(config.default_model)}", f"effort = {json.dumps(config.default_effort)}"]
    for provider in config.providers.values():
        lines += ["", "[[providers]]"]
        for name in ("id", "display_name", "base_url", "auth_mode", "default_model", "execution_mode", "protocol", "upstream_reasoning"):
            lines.append(f"{name} = {json.dumps(getattr(provider, name), ensure_ascii=False)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".config-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    config.path = path
