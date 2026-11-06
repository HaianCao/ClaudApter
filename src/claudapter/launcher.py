from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from .agent_models import agent_environment
from .command import command_prefix
from .config import (
    EFFORTS,
    Config,
    Provider,
    claude_base_url,
    config_path,
    secret_var,
    validate_capability_settings,
)
from .credentials import get_token, validate_token
from .errors import ClaudapterError
from .gateway_options import runtime_options, session_options, validate_gateway_limits
from .harness import cross_protocol_environment
from .mcp import environment_references, load_servers, session_arguments


def _bypass_loopback_proxy(env: dict[str, str]) -> None:
    entries = [value.strip() for key in ("NO_PROXY", "no_proxy") for value in env.get(key, "").split(",") if value.strip()]
    entries.extend(("127.0.0.1", "localhost", "::1"))
    merged = ",".join({value.casefold(): value for value in entries}.values())
    env.update(NO_PROXY=merged, no_proxy=merged)


def build_invocation(
    config: Config,
    provider: Provider,
    model: str,
    effort: str,
    extra: list[str],
    mode: str = "run",
    *,
    gateway_url: str | None = None,
    gateway_token: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    validate_capability_settings(provider.capabilities_file, provider.require_verified_capabilities, provider.execution_mode)
    if provider.upstream_reasoning not in ("auto", "provider_default", "disabled", "openrouter_disabled") or (
        provider.execution_mode != "gateway" and provider.upstream_reasoning not in ("auto", "provider_default")
    ):
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
    try:
        prefix = command_prefix(executable, "claude", "@anthropic-ai/claude-code")
    except ValueError as exc:
        raise ClaudapterError(str(exc)) from None
    token = get_token(config, provider.id)
    if not token:
        raise ClaudapterError(f"No API key is saved for {provider.display_name}.")
    token = validate_token(token)
    if not model.strip():
        raise ClaudapterError("Choose a model or enter its exact model ID.")
    if effort not in EFFORTS:
        raise ClaudapterError("Invalid effort level.")
    env = os.environ.copy()
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_SETUP_TOKEN",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_AWS",
    ):
        env.pop(name, None)
    if provider.execution_mode == "gateway":
        if not gateway_url or not gateway_token:
            raise ClaudapterError("Gateway mode requires a running local Protoverta gateway.")
        env.pop(secret_var(provider.id), None)
        env["ANTHROPIC_BASE_URL"] = gateway_url
        env["ANTHROPIC_AUTH_TOKEN"] = gateway_token
        _bypass_loopback_proxy(env)
    else:
        env["ANTHROPIC_BASE_URL"] = claude_base_url(provider.base_url)
        env["ANTHROPIC_AUTH_TOKEN" if provider.auth_mode == "bearer" else "ANTHROPIC_API_KEY"] = token
    args = [*prefix, "--model", model]
    managed_agents = agent_environment(prefix, provider.agent_models, model)
    env.update(managed_agents)
    settings = {"env": managed_agents} if managed_agents else {}
    if provider.execution_mode == "gateway":
        from .harness import detect_version, schema_supported

        if schema_supported(detect_version(prefix)):
            from .catalog import cached_model_names

            identifiers = tuple(dict.fromkeys((model,) + tuple(value for key, value in provider.agent_models.items() if key != "policy")))
            names = cached_model_names(config, provider, identifiers, token=token)
            settings["modelPicker"] = {
                "replaceBuiltInOptions": True,
                "options": [
                    {"model": mid, "label": names.get(mid, mid), "description": "Session model; capabilities are checked separately."} for mid in identifiers
                ],
            }
    if cross_protocol:
        # Scope compatibility settings to this child process, never user files.
        env.update(cross_protocol_environment(prefix))
    if cross_protocol or provider.upstream_reasoning not in ("auto", "provider_default"):
        env.update({"MAX_THINKING_TOKENS": "0", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1"})
        settings["alwaysThinkingEnabled"] = False
    if settings:
        args += ["--settings", json.dumps(settings)]
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
    if any(not isinstance(arg, str) or "\0" in arg for arg in args):
        raise ClaudapterError("CLI arguments must be strings without NUL characters.")
    try:
        environment_references(load_servers(os.environ.get("CLAUDE_CA_MCP_CONFIG")), env)
    except ValueError as exc:
        raise ClaudapterError(str(exc)) from None
    return args, env


def _gateway(config: Config, provider: Provider, model: str | None = None):
    try:
        from protoverta.core import Protocol
        from protoverta.server import GatewayConfig, gateway_session
    except ImportError as exc:
        raise ClaudapterError("Gateway mode needs a compatible Protoverta build installed in this Python environment.") from exc
    try:
        upstream = Protocol(provider.protocol)
        base_url = claude_base_url(provider.base_url) + "/v1" if upstream == Protocol.MESSAGES else provider.base_url.rstrip("/")
        options = {} if upstream == Protocol.MESSAGES else {"client_profile": "claude", "messages_usage_policy": "deferred"}
        if model is not None:
            from protoverta.model_snapshot import ModelSnapshot, SnapshotModel

            from .agent_models import validate_agent_models
            from .catalog import cached_model_names

            policy = validate_agent_models(provider.agent_models)
            identifiers = (model,) + tuple(value for key, value in policy.items() if key != "policy")
            names = cached_model_names(config, provider, identifiers)
            options["model_snapshot"] = ModelSnapshot(base_url, upstream, tuple(SnapshotModel(mid, names.get(mid, mid)) for mid in dict.fromkeys(identifiers)))
        options.update(runtime_options(provider.gateway_runtime, provider.execution_mode, config.path or config_path()))
        options.update(validate_gateway_limits(provider.gateway_limits, provider.execution_mode))
        capabilities_file, required = validate_capability_settings(provider.capabilities_file, provider.require_verified_capabilities, provider.execution_mode)
        if capabilities_file:
            from protoverta.capabilities import CapabilityRegistry

            path = Path(capabilities_file).expanduser()
            if not path.is_absolute():
                path = (config.path or config_path()).resolve().parent / path
            options.update(capabilities=CapabilityRegistry.load(path), require_verified_capabilities=required)
        reasoning_policy = provider.upstream_reasoning
        if reasoning_policy == "auto":
            from protoverta.automatic_reasoning import automatic_reasoning

            reasoning_policy, _ = automatic_reasoning("messages", upstream, base_url)
        gateway = GatewayConfig(
            upstream,
            base_url,
            validate_token(get_token(config, provider.id)),
            secrets.token_urlsafe(32),
            "x-api-key" if provider.auth_mode == "api_key" else "bearer",
            upstream_reasoning=reasoning_policy,
            **options,
        )
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
        from protoverta.capabilities import route_policies
        from protoverta.evidence_identity import evidence_options

        registry.assess_route(
            gateway.upstream_base_url,
            gateway.upstream_protocol,
            model,
            features,
            source_protocol="messages",
            streaming=True,
            client_profile=gateway.client_profile,
            policies=route_policies(gateway),
            **evidence_options(gateway),
        ).require(verified=gateway.require_verified_routes or gateway.require_current_evidence or gateway.evidence_max_age_days is not None)
    except ValueError as exc:
        raise ClaudapterError(f"Model compatibility check failed: {exc}") from exc


def preflight(config: Config, provider: Provider, model: str, effort: str) -> None:
    if provider.execution_mode == "gateway":
        gateway, _ = _gateway(config, provider, model)
        _verify_capabilities(gateway, model, effort)
        build_invocation(config, provider, model, effort, [], gateway_url="http://127.0.0.1:0", gateway_token="preflight")
    else:
        build_invocation(config, provider, model, effort, [])


def launch(config: Config, provider: Provider, model: str, effort: str, extra: list[str], mode: str = "run") -> int:
    def run(args, env):
        try:
            with session_arguments(os.environ.get("CLAUDE_CA_MCP_CONFIG"), env) as options:
                # Keep a possible [node.exe, cli.js] prefix intact. --model is
                # our first Claude option, before user passthrough arguments.
                index = args.index("--model")
                return subprocess.run([*args[:index], *options, *args[index:]], env=env, check=False).returncode
        except ValueError as exc:
            raise ClaudapterError(str(exc)) from None

    try:
        if provider.execution_mode == "gateway":
            gateway, gateway_session = _gateway(config, provider, model)
            _verify_capabilities(gateway, model, effort)
            with session_options(config.path or config_path(), provider) as observer_options, gateway_session(gateway, **observer_options) as origin:
                args, env = build_invocation(config, provider, model, effort, extra, mode, gateway_url=origin, gateway_token=gateway.inbound_token)
                return run(args, env)
        args, env = build_invocation(config, provider, model, effort, extra, mode)
        return run(args, env)
    except OSError as exc:
        raise ClaudapterError(f"Could not start Claude Code: {exc}") from exc
