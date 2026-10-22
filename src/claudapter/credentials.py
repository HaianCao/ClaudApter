from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .config import Config, config_path, secret_var
from .errors import ClaudapterError


def env_path(config: Config | None = None) -> Path:
    override = os.environ.get("CLAUDE_CA_ENV_FILE")
    if override:
        return Path(override).expanduser().resolve()
    return (config.path if config and config.path else config_path()).with_name(".env")


def _read(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, raw = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ClaudapterError("Invalid local .env file.")
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ClaudapterError("Invalid local .env value.") from exc
        else:
            value = raw.strip()
        if not isinstance(value, str):
            raise ClaudapterError("Local .env values must be strings.")
        values[name] = value
    return values


def get_token(config: Config, provider_id: str) -> str:
    name = secret_var(provider_id)
    return _read(env_path(config)).get(name) or os.environ.get(name, "")


def _write(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".secrets-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write("# ClaudApter secrets. Do not commit or share this file.\n")
            stream.writelines(f"{name}={json.dumps(value)}\n" for name, value in sorted(values.items()))
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def store_token(config: Config, provider_id: str, token: str) -> None:
    token = token.strip()
    if not token or "\n" in token or "\r" in token:
        raise ClaudapterError("API key cannot be empty or contain line breaks.")
    path = env_path(config)
    values = _read(path)
    values[secret_var(provider_id)] = token
    _write(path, values)


def remove_token(config: Config, provider_id: str) -> None:
    path = env_path(config)
    values = _read(path)
    values.pop(secret_var(provider_id), None)
    if path.exists():
        _write(path, values)
