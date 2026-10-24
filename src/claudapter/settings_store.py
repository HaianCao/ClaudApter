"""Atomic writes and optimistic, cross-process setup transactions."""

import hashlib
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".settings-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def revision(path: Path):
    try:
        return hashlib.sha256(path.read_bytes()).digest()
    except FileNotFoundError:
        return None


class SettingsGuard:
    """Serialize setup writers and reject stale snapshots; never merge blindly."""

    def __init__(self, path: Path):
        self.path = path
        self.expected = revision(path)

    @contextmanager
    def edit(self):
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            deadline = time.monotonic() + 5
            acquired = False
            try:
                while not acquired:
                    try:
                        stream.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Settings are busy. Try again after the other save finishes.") from None
                        time.sleep(0.02)
                if revision(self.path) != self.expected:
                    raise ValueError("Settings changed in another window or process. Reopen setup before saving.")
                yield
                self.expected = revision(self.path)
            finally:
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
