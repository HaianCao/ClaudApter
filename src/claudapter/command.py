"""Resolve supported npm batch shims without invoking a command processor."""

import json
import os
import shutil
import signal
from contextlib import contextmanager
from pathlib import Path


def command_prefix(executable: str, command: str, package: str) -> list[str]:
    path = Path(executable)
    if path.suffix.lower() not in {".cmd", ".bat"}:
        return [executable]
    error = f"Cannot safely run the {command} batch shim. Install the native CLI or its supported npm package."
    try:
        if path.stem.casefold() != command.casefold():
            raise ValueError(error)
        local = path.parent.name.casefold() == ".bin" and path.parent.parent.name.casefold() == "node_modules"
        modules = path.parent.parent if local else path.parent / "node_modules"
        root = (modules / package).resolve()
        with (root / "package.json").open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError(error)
        metadata = json.loads(raw)
        if not isinstance(metadata, dict) or metadata.get("name") != package:
            raise ValueError(error)
        binding = metadata.get("bin")
        entry = binding.get(command) if isinstance(binding, dict) else binding
        if not isinstance(entry, str) or not entry or Path(entry).is_absolute():
            raise ValueError(error)
        target = (root / entry).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError(error)
        if target.suffix.lower() in {".exe", ".com"}:
            return [str(target)]
        if target.suffix.lower() not in {".js", ".mjs", ".cjs"}:
            raise ValueError(error)
        adjacent = path.parent / "node.exe"
        node = str(adjacent) if adjacent.is_file() else shutil.which("node")
        if not node or Path(node).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise ValueError(error)
        if os.name == "nt" and Path(node).suffix.lower() not in {".exe", ".com"}:
            raise ValueError(error)
        return [node, str(target)]
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError(error) from None


@contextmanager
def foreground_child():
    """Let the interactive CLI own Ctrl+C while it runs in the foreground.

    The console delivers Ctrl+C to the whole process group. Without this the
    wrapper raised KeyboardInterrupt, killed the child after a short grace
    period and closed the session gateway mid-request (for example during
    `-p`/`exec`). A no-op Python handler is used instead of SIG_IGN because an
    ignored disposition would be inherited by the child on POSIX.
    """
    try:
        previous = signal.signal(signal.SIGINT, lambda *_: None)
    except ValueError:  # Not the main thread; nothing to adjust.
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
