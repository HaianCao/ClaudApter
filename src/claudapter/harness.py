"""Version-scoped CLI compatibility; model-role policy is managed separately."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import warnings
from functools import lru_cache
from pathlib import Path

_ESSENTIAL = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP"}


def parse_version(raw: bytes) -> tuple[int, int, int] | None:
    if len(raw) > 256:
        return None
    match = re.fullmatch(rb"([0-9]{1,5})\.([0-9]{1,5})\.([0-9]{1,5}) \(Claude Code\)\s*", raw)
    return tuple(map(int, match.groups())) if match else None


def detect_version(prefix: list[str]) -> tuple[int, int, int] | None:
    identity = []
    for part in prefix:
        try:
            stat = Path(part).stat()
            identity.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            identity.append(None)
    return _probe(tuple(prefix), tuple(identity))


@lru_cache(maxsize=32)
def _probe(prefix: tuple[str, ...], identity: tuple) -> tuple[int, int, int] | None:
    del identity
    try:
        with tempfile.TemporaryDirectory(prefix="claude-ca-version-") as directory:
            root = Path(directory)
            env = {key: value for key, value in os.environ.items() if key.upper() in _ESSENTIAL}
            for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
                target = root / key
                target.mkdir()
                env[key] = str(target)
            env.update(DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
            with (root / "version").open("w+b") as output:
                result = subprocess.run([*prefix, "--version"], env=env, cwd=root,
                                        stdin=subprocess.DEVNULL, stdout=output,
                                        stderr=subprocess.DEVNULL, timeout=3, check=False)
                output.seek(0)
                return parse_version(output.read(257)) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def cross_protocol_environment(prefix: list[str]) -> dict[str, str]:
    if detect_version(prefix) != (2, 1, 308):
        warnings.warn("Claude Code version is not verified for translated routes; retaining conservative compatibility settings. Subagents remain unverified.", RuntimeWarning, stacklevel=2)
    return {"CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS": "1",
            "DISABLE_PROMPT_CACHING": "1", "ENABLE_TOOL_SEARCH": "false"}
