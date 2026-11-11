from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .agent_models import agent_environment
from .command import command_prefix, foreground_child
from .config import (
    EFFORTS,
    Config,
    Provider,
    cache_path,
    claude_base_url,
    config_path,
    model_limit_environment,
    retry_policy,
    secret_var,
    validate_capability_settings,
    validate_model_limits,
)
from .credentials import get_token, validate_token
from .errors import ClaudapterError
from .gateway_options import runtime_options, session_options, validate_gateway_limits
from .harness import cross_protocol_environment
from .mcp import environment_references, load_servers, session_arguments
from .provider_url import bypass_loopback_proxy


def session_model_limits(config: Config, provider: Provider, model: str, token=None) -> dict:
    """Model limits for this launch.

    Limits set on the provider win. Any limit left empty comes from the
    selected model's maximum as the provider reports it in its model list
    (cached by setup or ``models list``); providers that report nothing keep
    Claude Code's own defaults.
    """
    from .catalog import cached_model_limits, launch_limits

    manual = dict(provider.model_limits)
    try:
        reported = cached_model_limits(config, provider, model, token=token)
    except (OSError, RuntimeError, ValueError):
        reported = {}  # Never fail a launch over the optional catalog cache.
    limits = {**launch_limits(reported), **manual}
    context, output = limits.get("context_window"), limits.get("max_output_tokens")
    if context and output and "max_output_tokens" not in manual:
        # A reported output maximum keeps half of the session's window for input.
        limits["max_output_tokens"] = min(output, context // 2)
    try:
        validate_model_limits(limits)
    except ClaudapterError:
        return manual
    return limits


# Global model choices Claude Code reads for background tasks and subagents.
GLOBAL_MODEL_VARIABLES = (
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


def model_override_notice(provider: Provider, environ=None) -> str | None:
    """Explain inherited global model variables instead of silently dropping them.

    With the inherit agent policy ClaudApter leaves these user settings alone;
    they may name models another provider serves.
    """
    environ = os.environ if environ is None else environ
    if provider.agent_models.get("policy", "inherit") != "inherit":
        return None
    names = [name for name in GLOBAL_MODEL_VARIABLES if environ.get(name)]
    if not names:
        return None
    return (
        f"Note: {', '.join(names)} {'is' if len(names) == 1 else 'are'} set in your environment, so Claude Code uses "
        f"{'it' if len(names) == 1 else 'them'} for background tasks and subagents on {provider.display_name}. "
        "If those models are not available there, unset them or choose agent models in claude-ca setup."
    )


def _retry_timeout_ms(policy) -> int:
    """Claude Code's request timeout must outlast the retry waits (cap 30 min)."""
    waits = min(policy.max_total_seconds, policy.max_retries * policy.delay_seconds * (1 + policy.jitter))
    return int(min((waits + 600) * 1000, 1_800_000))


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
    relay_url: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    validate_capability_settings(
        provider.capabilities_file, provider.require_verified_capabilities, provider.execution_mode
    )
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
        bypass_loopback_proxy(env)
    else:
        env["ANTHROPIC_BASE_URL"] = relay_url or claude_base_url(provider.base_url)
        env["ANTHROPIC_AUTH_TOKEN" if provider.auth_mode == "bearer" else "ANTHROPIC_API_KEY"] = token
    policy = retry_policy(config)
    if policy.enabled and policy.max_retries and (relay_url or provider.execution_mode == "gateway"):
        # An explicit user value is respected.
        env.setdefault("API_TIMEOUT_MS", str(_retry_timeout_ms(policy)))
    # Claude Code 2.1.312+ may switch to a built-in Claude model when a request
    # is flagged; provider/gateway model IDs have no such fallback. An explicit
    # user value is respected.
    env.setdefault("CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK", "1")
    env.update(model_limit_environment(session_model_limits(config, provider, model, token)))
    args = [*prefix, "--model", model]
    managed_agents = agent_environment(prefix, provider.agent_models, model)
    env.update(managed_agents)
    settings = {"env": managed_agents} if managed_agents else {}
    if provider.execution_mode == "gateway":
        from .harness import detect_version, schema_supported

        if schema_supported(detect_version(prefix)):
            from .catalog import cached_model_names

            identifiers = tuple(
                dict.fromkeys(
                    (model,) + tuple(value for key, value in provider.agent_models.items() if key != "policy")
                )
            )
            names = cached_model_names(config, provider, identifiers, token=token)
            settings["modelPicker"] = {
                "replaceBuiltInOptions": True,
                "options": [
                    {
                        "model": mid,
                        "label": names.get(mid, mid),
                        "description": "Session model; capabilities are checked separately.",
                    }
                    for mid in identifiers
                ],
            }
    if cross_protocol:
        # Scope compatibility settings to this child process, never user files.
        env.update(cross_protocol_environment(prefix))
        # Translated routes carry low/medium/high only; Claude Code 2.1.319
        # otherwise sends its own default "xhigh". Claude Code clamps every
        # effort source (default, /effort, --effort) to this cap itself.
        settings["maxEffortLevel"] = "high"
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


def _automatic_reasoning(upstream) -> tuple[str, str | None]:
    """Resolve ``auto`` with the core's route policy.

    Returns (upstream_reasoning, reasoning_output or None).
    """
    from protoverta.automatic_reasoning import automatic_route_reasoning

    policy, _, output = automatic_route_reasoning("messages", upstream.value)
    return policy, None if output == "reject" else output


def _gateway(config: Config, provider: Provider, model: str | None = None):
    try:
        from protoverta.core import Protocol
        from protoverta.server import GatewayConfig, gateway_session
    except ImportError as exc:
        raise ClaudapterError(
            "Gateway mode needs a compatible Protoverta build installed in this Python environment."
        ) from exc
    try:
        upstream = Protocol(provider.protocol)
        base_url = (
            claude_base_url(provider.base_url) + "/v1"
            if upstream == Protocol.MESSAGES
            else provider.base_url.rstrip("/")
        )
        options: dict[str, Any] = (
            {} if upstream == Protocol.MESSAGES else {"client_profile": "claude", "messages_usage_policy": "deferred"}
        )
        if model is not None:
            from protoverta.model_snapshot import ModelSnapshot, SnapshotModel

            from .agent_models import validate_agent_models
            from .catalog import cached_model_names

            policy = validate_agent_models(provider.agent_models)
            identifiers = (model,) + tuple(value for key, value in policy.items() if key != "policy")
            names = cached_model_names(config, provider, identifiers)
            options["model_snapshot"] = ModelSnapshot(
                base_url, upstream, tuple(SnapshotModel(mid, names.get(mid, mid)) for mid in dict.fromkeys(identifiers))
            )
        options.update(runtime_options(provider.gateway_runtime, provider.execution_mode, config.path or config_path()))
        options.update(validate_gateway_limits(provider.gateway_limits, provider.execution_mode))
        capabilities_file, required = validate_capability_settings(
            provider.capabilities_file, provider.require_verified_capabilities, provider.execution_mode
        )
        if capabilities_file:
            from protoverta.capabilities import CapabilityRegistry

            path = Path(capabilities_file).expanduser()
            if not path.is_absolute():
                path = (config.path or config_path()).resolve().parent / path
            options.update(capabilities=CapabilityRegistry.load(path), require_verified_capabilities=required)
        reasoning_policy = provider.upstream_reasoning
        if reasoning_policy == "auto":
            reasoning_policy, reasoning_output = _automatic_reasoning(upstream)
            if reasoning_output is not None:
                options["reasoning_output"] = reasoning_output
        policy = retry_policy(config)
        if policy.enabled and policy.max_retries:
            from .retry_log import RetryLog

            options.update(retry_policy=policy, retry_log=RetryLog(cache_path(config).parent / "retry.log"))
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
        ).require(
            verified=gateway.require_verified_routes
            or gateway.require_current_evidence
            or gateway.evidence_max_age_days is not None
        )
    except ValueError as exc:
        raise ClaudapterError(f"Model compatibility check failed: {exc}") from exc


def preflight(config: Config, provider: Provider, model: str, effort: str) -> None:
    if provider.execution_mode == "gateway":
        gateway, _ = _gateway(config, provider, model)
        _verify_capabilities(gateway, model, effort)
        build_invocation(
            config, provider, model, effort, [], gateway_url="http://127.0.0.1:0", gateway_token="preflight"
        )
    else:
        build_invocation(config, provider, model, effort, [])


def launch(config: Config, provider: Provider, model: str, effort: str, extra: list[str], mode: str = "run") -> int:
    def run(args, env):
        try:
            with session_arguments(os.environ.get("CLAUDE_CA_MCP_CONFIG"), env) as options:
                # Keep a possible [node.exe, cli.js] prefix intact. --model is
                # our first Claude option, before user passthrough arguments.
                index = args.index("--model")
                with foreground_child():
                    return subprocess.run([*args[:index], *options, *args[index:]], env=env, check=False).returncode
        except ValueError as exc:
            raise ClaudapterError(str(exc)) from None

    notice = model_override_notice(provider)
    if notice:
        print(notice, file=sys.stderr)
    try:
        if provider.execution_mode == "gateway":
            gateway, gateway_session = _gateway(config, provider, model)
            _verify_capabilities(gateway, model, effort)
            with (
                session_options(config.path or config_path(), provider) as observer_options,
                gateway_session(gateway, **observer_options) as origin,
            ):
                args, env = build_invocation(
                    config,
                    provider,
                    model,
                    effort,
                    extra,
                    mode,
                    gateway_url=origin,
                    gateway_token=gateway.inbound_token,
                )
                return run(args, env)
        policy = retry_policy(config)
        if not policy.enabled or not policy.max_retries:
            return run(*build_invocation(config, provider, model, effort, extra, mode))
        from .retry_log import RetryLog
        from .retry_relay import RetryRelay

        log = RetryLog(cache_path(config).parent / "retry.log")
        with RetryRelay(claude_base_url(provider.base_url), policy, "messages", log) as relay:
            return run(*build_invocation(config, provider, model, effort, extra, mode, relay_url=relay.base_url))
    except OSError as exc:
        raise ClaudapterError(f"Could not start Claude Code: {exc}") from exc
