"""Explicit session policy for Claude auxiliary and subagent models."""

from .errors import ClaudapterError
from .harness import detect_version

ROLES = ("haiku", "sonnet", "opus", "fable")
_ALIASES = {"default", "inherit", "haiku", "sonnet", "opus", "fable", "opusplan"}


def _model(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or value.lower().removesuffix("[1m]") in _ALIASES or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ClaudapterError("Agent model must be an exact model ID (1–512 characters), not a Claude family alias or whitespace.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ClaudapterError("Agent model must contain valid Unicode.") from None
    return value


def validate_agent_models(value):
    if not isinstance(value, dict) or set(value) - {"policy", "default", *ROLES}:
        raise ClaudapterError("agent_models accepts policy, default, haiku, sonnet, opus and fable only; agent-only effort is not available here.")
    policy = value.get("policy", "inherit")
    if policy not in ("inherit", "selected", "custom"):
        raise ClaudapterError("Agent model policy must be inherit, selected or custom.")
    models = set(value) - {"policy"}
    if policy != "custom" and models:
        raise ClaudapterError("Model-role overrides require the custom agent model policy.")
    if policy == "custom" and not models:
        raise ClaudapterError("Enter at least one exact model ID for custom agent models.")
    for key in models:
        _model(value[key])
    return dict(value)


def agent_environment(prefix, options, model):
    options = validate_agent_models(options)
    policy = options.get("policy", "inherit")
    if policy == "inherit":
        return {}
    if detect_version(prefix) != (2, 1, 308):
        raise ClaudapterError("Agent model overrides are verified with Claude Code 2.1.308 only. Choose Inherit harness settings for another version.")
    selected = _model(model)
    managed = {f"ANTHROPIC_DEFAULT_{role.upper()}_MODEL": options.get(role, selected) for role in ROLES}
    managed["CLAUDE_CODE_SUBAGENT_MODEL"] = options.get("default", selected)
    # Selected explicitly overrides custom agent model definitions. Custom uses
    # the CLI's normal invocation/frontmatter/default precedence instead.
    managed["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] = "1" if policy == "selected" else "0"
    return managed


def agent_models_toml(options):
    import json

    return "{ " + ", ".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in sorted(validate_agent_models(options).items())) + " }"
