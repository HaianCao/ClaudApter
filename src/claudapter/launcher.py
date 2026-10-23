from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

from .config import Config, EFFORTS, Provider, secret_var, config_path, validate_capability_settings
from .credentials import get_token
from .errors import ClaudapterError


def build_invocation(config: Config, provider: Provider, model: str, effort: str, extra: list[str], mode: str = "run", *, gateway_url: str | None = None, gateway_token: str | None = None) -> tuple[list[str], dict[str, str]]:
    validate_capability_settings(provider.capabilities_file, provider.require_verified_capabilities, provider.execution_mode)
    if provider.upstream_reasoning not in ("provider_default", "disabled", "openrouter_disabled") or (provider.execution_mode != "gateway" and provider.upstream_reasoning != "provider_default"):
        raise ClaudapterError("An explicit upstream reasoning policy requires gateway mode.")
    if provider.execution_mode not in {"direct", "gateway"}:
        raise ClaudapterError("Unsupported execution mode.")
    if provider.protocol not in {"messages", "chat", "responses"}:
        raise ClaudapterError("Unsupported provider protocol.")
    cross_protocol = provider.protocol != "messages"
    if cross_protocol and provider.execution_mode != "gateway":
        raise ClaudapterError("Chat and Responses providers require gateway mode.")
    if cross_protocol and effort not in {"default", "low", "medium", "high"}:
        raise ClaudapterError("Cross-protocol gateway supports default, low, medium and high effort only.")
    executable = shutil.which("claude")
    if not executable:
        raise ClaudapterError("Claude Code CLI was not found on PATH. Install it and verify with: claude --version")
    token = get_token(config, provider.id)
    if not token:
        raise ClaudapterError(f"No API key is saved for {provider.display_name}.")
    if not model.strip():
        raise ClaudapterError("Choose a model or enter its exact model ID.")
    if effort not in EFFORTS:
        raise ClaudapterError("Invalid effort level.")
    env = os.environ.copy()
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_MODEL", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_AWS"):
        env.pop(name, None)
    if provider.execution_mode == "gateway":
        if not gateway_url or not gateway_token:
            raise ClaudapterError("Gateway mode requires a running local Protoverta gateway.")
        env["ANTHROPIC_BASE_URL"] = gateway_url
        env["ANTHROPIC_AUTH_TOKEN"] = gateway_token
        env.pop(secret_var(provider.id), None)
    else:
        env["ANTHROPIC_BASE_URL"] = provider.base_url
        env["ANTHROPIC_AUTH_TOKEN" if provider.auth_mode == "bearer" else "ANTHROPIC_API_KEY"] = token
    args = [executable, "--model", model]
    if cross_protocol:
        # Scope compatibility settings to this child process, never user files.
        env.update({"CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS": "1",
                    "DISABLE_PROMPT_CACHING": "1", "ENABLE_TOOL_SEARCH": "false"})
    if cross_protocol or provider.upstream_reasoning != "provider_default":
        env.update({"MAX_THINKING_TOKENS": "0", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1"})
        args += ["--settings", '{"alwaysThinkingEnabled":false}']
    if effort != "default":
        args += ["--effort", effort]
    if mode == "print":
        args.append("-p")
    elif mode == "continue":
        args.append("--continue")
    elif mode == "resume":
        args.append("--resume")
    elif mode != "run":
        raise ClaudapterError("Unsupported launch mode.")
    args += extra
    return args, env


def _gateway(config: Config, provider: Provider):
    try:
        from protoverta.core import Protocol
        from protoverta.server import GatewayConfig, gateway_session
    except ImportError as exc:
        raise ClaudapterError("Gateway mode needs a compatible Protoverta build installed in this Python environment.") from exc
    try:
        upstream = Protocol(provider.protocol)
        base_url = provider.base_url.rstrip("/") + ("/v1" if upstream == Protocol.MESSAGES else "")
        options = {} if upstream == Protocol.MESSAGES else {"client_profile": "claude", "messages_usage_policy": "deferred"}
        capabilities_file, required = validate_capability_settings(provider.capabilities_file, provider.require_verified_capabilities, provider.execution_mode)
        if capabilities_file:
            from protoverta.capabilities import CapabilityRegistry

            path = Path(capabilities_file).expanduser()
            if not path.is_absolute():
                path = (config.path or config_path()).resolve().parent / path
            options.update(capabilities=CapabilityRegistry.load(path), require_verified_capabilities=required)
        gateway = GatewayConfig(upstream, base_url, get_token(config, provider.id), secrets.token_urlsafe(32), "x-api-key" if provider.auth_mode == "api_key" else "bearer", upstream_reasoning=provider.upstream_reasoning, **options)
    except (ImportError, OSError, TypeError, ValueError) as exc:
        raise ClaudapterError(f"Invalid or incompatible Protoverta gateway configuration: {exc}") from exc
    return gateway, gateway_session


def _verify_capabilities(gateway, model: str, effort: str) -> None:
    registry = getattr(gateway, "capabilities", None)
    if registry is None:
        return
    features = {"text", "streaming"}
    if gateway.upstream_reasoning == "provider_default" and effort != "default":
        features.add("reasoning_effort")
    try:
        report = registry.assess(gateway.upstream_base_url, gateway.upstream_protocol, model, features)
        report.require(verified=gateway.require_verified_capabilities)
    except ValueError as exc:
        raise ClaudapterError(f"Model compatibility check failed: {exc}") from exc


def preflight(config: Config, provider: Provider, model: str, effort: str) -> None:
    if provider.execution_mode == "gateway":
        gateway, _ = _gateway(config, provider)
        _verify_capabilities(gateway, model, effort)
        build_invocation(config, provider, model, effort, [], gateway_url="http://127.0.0.1:0", gateway_token="preflight")
    else:
        build_invocation(config, provider, model, effort, [])


def launch(config: Config, provider: Provider, model: str, effort: str, extra: list[str], mode: str = "run") -> int:
    try:
        if provider.execution_mode == "gateway":
            gateway, gateway_session = _gateway(config, provider)
            _verify_capabilities(gateway, model, effort)
            with gateway_session(gateway) as origin:
                args, env = build_invocation(config, provider, model, effort, extra, mode, gateway_url=origin, gateway_token=gateway.inbound_token)
                return subprocess.run(args, env=env, check=False).returncode
        args, env = build_invocation(config, provider, model, effort, extra, mode)
        return subprocess.run(args, env=env, check=False).returncode
    except OSError as exc:
        raise ClaudapterError(f"Could not start Claude Code: {exc}") from exc
