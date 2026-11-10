"""Explicit, per-launch external MCP configuration; never global registration."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


def load_servers(value: str | None) -> dict:
    if not value:
        return {}
    try:
        with Path(value).expanduser().open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        document = json.loads(raw)
        if not isinstance(document, dict) or set(document) != {"mcpServers"}:
            raise ValueError
        servers = document["mcpServers"]
        if not isinstance(servers, dict) or not servers or len(servers) > 16:
            raise ValueError
        for name, server in servers.items():
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name) or not isinstance(server, dict):
                raise ValueError
            if set(server) - {"command", "args", "env_vars"}:
                raise ValueError
            command = server.get("command")
            if not isinstance(command, str) or "\0" in command or not Path(command).is_absolute() or not Path(command).is_file():
                raise ValueError
            args = server.get("args", [])
            names = server.get("env_vars", [])
            if not isinstance(args, list) or len(args) > 128 or any(not isinstance(arg, str) or "\0" in arg for arg in args):
                raise ValueError
            if (
                not isinstance(names, list)
                or len(names) > 64
                or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in names)
            ):
                raise ValueError
        return servers
    except (OSError, ValueError, TypeError, RecursionError, RuntimeError):
        raise ValueError(
            "Invalid session MCP config: use mcpServers with absolute executable command, args and optional env_vars names (maximum 64 KiB). Inline env values and remote URLs are not supported."
        ) from None


def environment_references(servers: dict, env: dict[str, str], *, case_sensitive: bool | None = None) -> dict:
    """Validate prerequisites without files/processes; keep only variable names.

    A copy of os.environ is a normal case-sensitive dict, even on Windows.
    Resolve references using the child's OS rules and its actual key spelling.
    """
    if case_sensitive is None:
        case_sensitive = os.name != "nt"
    available = {key if case_sensitive else key.casefold(): key for key in env}
    references = {}
    for name, server in servers.items():
        names = server.get("env_vars", [])
        if any((key if case_sensitive else key.casefold()) not in available for key in names):
            raise ValueError("A session MCP env_vars name is missing from the child environment.")
        references[name] = {key: "${" + available[key if case_sensitive else key.casefold()] + "}" for key in names}
    return references


@contextmanager
def session_arguments(value: str | None, env: dict[str, str]):
    servers = load_servers(value)
    references = environment_references(servers, env)
    if not servers:
        yield []
        return
    mapped = {}
    for name, server in servers.items():
        mapped[name] = {"command": server["command"], "args": server.get("args", []), "env": references[name]}
    with tempfile.TemporaryDirectory(prefix="claudapter-mcp-") as root:
        path = Path(root) / "servers.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"mcpServers": mapped}, stream)
        yield ["--strict-mcp-config", "--mcp-config", str(path)]
