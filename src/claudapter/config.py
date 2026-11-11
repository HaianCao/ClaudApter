from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

try:
    from protoverta.error_classes import RetryPolicy
except ImportError:  # A broken install must still open the setup page.
    RetryPolicy = None  # type: ignore[assignment,misc]

from .errors import ClaudapterError
from .toml_text import toml_value


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
    capabilities_file: str = ""
    require_verified_capabilities: bool = False
    gateway_limits: dict = field(default_factory=dict)
    gateway_runtime: dict = field(default_factory=dict)
    agent_models: dict = field(default_factory=dict)
    model_limits: dict = field(default_factory=dict)


MODEL_LIMITS = {
    # Claude Code assumes built-in sizes for model IDs it does not recognise.
    "context_window": "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "max_output_tokens": "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
}


def validate_model_limits(value) -> dict:
    if not isinstance(value, dict) or set(value) - set(MODEL_LIMITS):
        raise ClaudapterError("model_limits accepts context_window and max_output_tokens only.")
    for number in value.values():
        if type(number) is not int or not 1 <= number <= 100_000_000:
            raise ClaudapterError("Model limits must be positive whole token counts.")
    if "context_window" in value and value.get("max_output_tokens", 0) >= value["context_window"]:
        raise ClaudapterError("Maximum output tokens must be smaller than the context window.")
    return dict(value)


def model_limit_environment(value) -> dict[str, str]:
    """Session-only Claude Code overrides for a provider's model size."""
    return {MODEL_LIMITS[key]: str(number) for key, number in sorted(validate_model_limits(value).items())}


@dataclass
class Config:
    providers: dict[str, Provider] = field(default_factory=dict)
    default_provider: str = ""
    default_model: str = ""
    default_effort: str = "default"
    path: Path | None = None
    # The [retry] table as written; see retry_policy(). Empty means defaults.
    retry: dict = field(default_factory=dict)


def retry_policy(config: Config):
    """The session's error-class retry policy (Protoverta ``RetryPolicy``)."""
    if RetryPolicy is None:
        raise ClaudapterError(
            "ClaudApter's Protoverta component is missing or broken. Run the installer command again to repair it."
        )
    try:
        return RetryPolicy.from_mapping(config.retry)
    except ValueError as exc:
        raise ClaudapterError(f"Invalid [retry] settings: {exc}") from None


EFFORTS = {"default", "low", "medium", "high", "xhigh", "max"}


def validate_capability_settings(path, required, mode):
    if not isinstance(path, str) or "\0" in path or not isinstance(required, bool):
        raise ClaudapterError(
            "Capability registry path must be text and require_verified_capabilities must be boolean."
        )
    path = path.strip()
    if (path or required) and mode != "gateway":
        raise ClaudapterError("Capability checks require gateway mode.")
    if required and not path:
        raise ClaudapterError("Choose a capability registry before requiring verified capabilities.")
    return path, required


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
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        raise ClaudapterError("Provider ID must contain lowercase letters, digits, _ or -.")
    return value


def secret_names(provider_id: str) -> tuple[str, str]:
    """Keep adapter credentials out of Claude's native authentication namespace."""
    legacy = re.sub(r"[^A-Za-z0-9]", "_", validate_provider_id(provider_id)).upper() + "_TOKEN"
    needs_namespace = legacy[0].isdigit() or legacy.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
    name = "CLAUDE_CA_PROVIDER_" + legacy if needs_namespace else legacy
    return name, legacy


def secret_var(provider_id: str) -> str:
    return secret_names(provider_id)[0]


def validate_base_url(value: str, protocol: str = "messages") -> str:
    _text(value, "base_url", required=True)
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ClaudapterError("Base URL must not contain whitespace or control characters.")
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ClaudapterError("Base URL has an invalid host or port.") from exc
    local_http = parsed.scheme == "http" and hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        re.search(r"\s", value)
        or (parsed.scheme != "https" and not local_http)
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and port < 1)
    ):
        raise ClaudapterError("Base URL must use HTTPS (or loopback HTTP) without credentials, query or fragment.")
    if parsed.path.endswith(("/messages", "/responses", "/chat/completions")):
        raise ClaudapterError(
            "Enter the base URL, for example https://provider.example/v1, without the endpoint suffix."
        )
    return value


def claude_base_url(value: str) -> str:
    """Accept the API prefix users copy; Claude appends /v1/messages itself."""
    value = validate_base_url(value)
    return value[:-3] if urlsplit(value).path.endswith("/v1") else value


def _text(value, field_name: str, *, required: bool = False) -> None:
    if not isinstance(value, str) or "\0" in value or (required and not value.strip()):
        raise ClaudapterError(f"{field_name} must be {'non-empty ' if required else ''}text without NUL characters.")


def validate(config: Config) -> None:
    if not isinstance(config, Config) or not isinstance(config.providers, dict):
        raise ClaudapterError("Configuration providers must be a mapping of provider IDs to providers.")
    for field_name in ("default_provider", "default_model", "default_effort"):
        _text(getattr(config, field_name), field_name)
    if config.default_effort not in EFFORTS:
        raise ClaudapterError("Invalid effort level.")
    used_secret_names: set[str] = set()
    for provider_id, provider in config.providers.items():
        validate_provider_id(provider_id)
        if not isinstance(provider, Provider):
            raise ClaudapterError("Each configured provider must be a Provider object.")
        for field_name in (
            "id",
            "display_name",
            "base_url",
            "auth_mode",
            "default_model",
            "execution_mode",
            "protocol",
            "upstream_reasoning",
        ):
            _text(getattr(provider, field_name), "provider." + field_name, required=field_name != "default_model")
        from .gateway_options import validate_gateway_limits

        validate_gateway_limits(provider.gateway_limits, provider.execution_mode)
        from .agent_models import validate_agent_models

        validate_agent_models(provider.agent_models)
        validate_model_limits(provider.model_limits)
        validate_capability_settings(
            provider.capabilities_file, provider.require_verified_capabilities, provider.execution_mode
        )
        if provider.id != provider_id or not provider.display_name.strip():
            raise ClaudapterError(f"Invalid provider: {provider_id}")
        if provider.protocol not in {"messages", "chat", "responses"}:
            raise ClaudapterError(f"Invalid protocol for {provider_id}.")
        validate_base_url(provider.base_url, provider.protocol)
        from .gateway_options import validate_gateway_runtime

        validate_gateway_runtime(provider.gateway_runtime, provider.execution_mode)
        if provider.auth_mode not in {"bearer", "api_key"}:
            raise ClaudapterError(f"Invalid auth mode for {provider_id}.")
        if provider.execution_mode not in {"direct", "gateway"}:
            raise ClaudapterError(f"Invalid execution mode for {provider_id}.")
        if provider.upstream_reasoning not in ("auto", "provider_default", "disabled", "openrouter_disabled") or (
            provider.execution_mode != "gateway" and provider.upstream_reasoning not in ("auto", "provider_default")
        ):
            raise ClaudapterError("An explicit upstream reasoning policy requires gateway mode.")
        if provider.protocol != "messages" and provider.execution_mode != "gateway":
            raise ClaudapterError("Chat and Responses providers require local Protoverta gateway mode.")
        name = secret_var(provider_id)
        if name in used_secret_names:
            raise ClaudapterError(f"Provider IDs collide on the same secret name: {name}.")
        used_secret_names.add(name)
    if config.default_provider and config.default_provider not in config.providers:
        raise ClaudapterError("Default provider does not exist.")
    if not isinstance(config.retry, dict):
        raise ClaudapterError("[retry] must be a TOML table.")
    if RetryPolicy is not None:  # Without the core, launching reports the repair step.
        retry_policy(config)


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config(path=path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
            raise ClaudapterError("Unsupported config schema version.")
        defaults = raw.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ClaudapterError("defaults must be a TOML table.")
        items = raw.get("providers", [])
        if not isinstance(items, list):
            raise ClaudapterError("providers must be an array of TOML tables ([[providers]]).")
        providers = {}
        for item in items:
            if not isinstance(item, dict):
                raise ClaudapterError("Each providers entry must be a TOML table.")
            provider = Provider(**{"upstream_reasoning": "auto", **item})
            validate_provider_id(provider.id)
            if provider.id in providers:
                raise ClaudapterError(f"Duplicate provider: {provider.id}")
            providers[provider.id] = provider
        retry = raw.get("retry", {})
        if not isinstance(retry, dict):
            raise ClaudapterError("[retry] must be a TOML table.")
        config = Config(
            providers,
            defaults.get("provider", ""),
            defaults.get("model", ""),
            defaults.get("effort", "default"),
            path,
            retry,
        )
        validate(config)
        return config
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, TypeError, KeyError, ValueError) as exc:
        raise ClaudapterError(f"Cannot load config: {exc}") from exc


def save(config: Config) -> None:
    validate(config)
    path = config.path or config_path()
    lines = [
        "schema_version = 1",
        "",
        "[defaults]",
        f"provider = {toml_value(config.default_provider, ensure_ascii=True)}",
        f"model = {toml_value(config.default_model, ensure_ascii=True)}",
        f"effort = {toml_value(config.default_effort, ensure_ascii=True)}",
    ]
    if config.retry:
        # Written as the user set it (0.1.x wrote this table too).
        lines += ["", "[retry]"] + [f"{key} = {toml_value(config.retry[key])}" for key in sorted(config.retry)]
    for provider in config.providers.values():
        lines += ["", "[[providers]]"]
        for name in (
            "id",
            "display_name",
            "base_url",
            "auth_mode",
            "default_model",
            "execution_mode",
            "protocol",
            "upstream_reasoning",
        ):
            lines.append(f"{name} = {toml_value(getattr(provider, name))}")
        registry_path, required = validate_capability_settings(
            provider.capabilities_file, provider.require_verified_capabilities, provider.execution_mode
        )
        if registry_path:
            lines.extend(
                [
                    f"capabilities_file = {toml_value(registry_path)}",
                    f"require_verified_capabilities = {str(required).lower()}",
                ]
            )
        if provider.gateway_limits:
            from .gateway_options import limits_toml

            lines.append("gateway_limits = " + limits_toml(provider.gateway_limits))
        if provider.gateway_runtime:
            from .gateway_options import runtime_toml

            lines.append("gateway_runtime = " + runtime_toml(provider.gateway_runtime))
        if provider.agent_models:
            from .agent_models import agent_models_toml

            lines.append("agent_models = " + agent_models_toml(provider.agent_models))
        if provider.model_limits:
            limits = validate_model_limits(provider.model_limits)
            lines.append("model_limits = { " + ", ".join(f"{key} = {limits[key]}" for key in sorted(limits)) + " }")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".config-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    config.path = path
