from __future__ import annotations

import os
import shutil
import subprocess

from .config import EFFORTS, Config, Provider, claude_base_url
from .credentials import get_token
from .errors import ClaudapterError


def build_invocation(config: Config, provider: Provider, model: str, effort: str, extra: list[str], mode: str = "run") -> tuple[list[str], dict[str, str]]:
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
    env["ANTHROPIC_BASE_URL"] = claude_base_url(provider.base_url)
    env["ANTHROPIC_AUTH_TOKEN" if provider.auth_mode == "bearer" else "ANTHROPIC_API_KEY"] = token
    args = [executable, "--model", model]
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


def launch(config: Config, provider: Provider, model: str, effort: str, extra: list[str], mode: str = "run") -> int:
    args, env = build_invocation(config, provider, model, effort, extra, mode)
    try:
        return subprocess.run(args, env=env, check=False).returncode
    except OSError as exc:
        raise ClaudapterError(f"Could not start Claude Code: {exc}") from exc
