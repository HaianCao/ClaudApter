"""Conservative setup status from local evidence; no discovery or gateway."""

from pathlib import Path

from .config import claude_base_url, config_path


def model_status(config, provider, model):
    unknown = {"status": "unverified", "message": "Unverified route. Model discovery is not capability certification."}
    if not model or not provider.capabilities_file:
        return unknown
    try:
        from protoverta.automatic_reasoning import automatic_reasoning
        from protoverta.capabilities import CapabilityRegistry
        from protoverta.selection_status import selection_status

        base = claude_base_url(provider.base_url) + "/v1" if provider.protocol == "messages" else provider.base_url.rstrip("/")
        path = Path(provider.capabilities_file).expanduser()
        if not path.is_absolute():
            path = (config.path or config_path()).resolve().parent / path
        reasoning = automatic_reasoning("messages", provider.protocol, base)[0] if provider.upstream_reasoning == "auto" else provider.upstream_reasoning
        options = {**provider.gateway_runtime, "upstream_reasoning": reasoning,
                   "messages_usage_policy": "strict" if provider.protocol == "messages" else "deferred"}
        return selection_status(CapabilityRegistry.load(path), base, provider.protocol, model, source_protocol="messages",
                                client_profile="strict" if provider.protocol == "messages" else "claude", overrides=options,
                                freshness_required=bool(provider.gateway_runtime.get("require_current_evidence") or provider.gateway_runtime.get("evidence_max_age_days") is not None))
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return unknown
