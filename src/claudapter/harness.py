"""Version-scoped CLI compatibility; model-role policy is managed separately."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import warnings
from functools import lru_cache
from pathlib import Path

# A cold native CLI start (a large executable scanned by antivirus on first
# run) can take several seconds; 3 s silently disabled version-gated features.
VERSION_PROBE_SECONDS = 15
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
                result = subprocess.run(
                    [*prefix, "--version"],
                    env=env,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=VERSION_PROBE_SECONDS,
                    check=False,
                )
                output.seek(0)
                return parse_version(output.read(257)) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


# Releases exercised end to end with the launcher (loopback client smoke, tool
# loops, model picker). Claude Code auto-updates, so newer 2.1.x releases keep
# the same settings with a visible warning instead of a hard failure.
VERIFIED_VERSIONS = frozenset({(2, 1, 308), (2, 1, 318), (2, 1, 319)})
MINIMUM_SCHEMA_VERSION = (2, 1, 308)


def schema_supported(version: tuple[int, int, int] | None) -> bool:
    """True for verified releases and newer releases of the same 2.1 series."""
    return version is not None and version[:2] == MINIMUM_SCHEMA_VERSION[:2] and version >= MINIMUM_SCHEMA_VERSION


def describe(version: tuple[int, int, int] | None) -> str:
    return "an unknown version" if version is None else ".".join(map(str, version))


def require_schema(version: tuple[int, int, int] | None, feature: str, exception: type[Exception]) -> tuple[int, int, int]:
    """Allow verified/newer 2.1.x releases (newer ones warn); reject others."""
    if not schema_supported(version):
        raise exception(
            f"{feature} requires Claude Code {describe(MINIMUM_SCHEMA_VERSION)} or a newer 2.1.x release; found {describe(version)}. Update Claude Code or choose Inherit harness settings."
        )
    if version not in VERIFIED_VERSIONS:
        warnings.warn(
            f"{feature} is verified with Claude Code {', '.join(sorted(map(describe, VERIFIED_VERSIONS)))}; continuing with the same settings on {describe(version)}.",
            RuntimeWarning,
            stacklevel=3,
        )
    return version


def cross_protocol_environment(prefix: list[str]) -> dict[str, str]:
    if detect_version(prefix) not in VERIFIED_VERSIONS:
        warnings.warn(
            "Claude Code version is not verified for translated routes; retaining conservative compatibility settings. Subagents remain unverified.",
            RuntimeWarning,
            stacklevel=2,
        )
    return {
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        "CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS": "1",
        "DISABLE_PROMPT_CACHING": "1",
        "ENABLE_TOOL_SEARCH": "false",
    }
