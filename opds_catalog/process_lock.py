"""Small cross-process file lock used by updater and scanner commands."""

import os
from pathlib import Path

try:  # POSIX (production/Linux)
    import fcntl
except ImportError:  # Windows development fallback
    fcntl = None
    import importlib

    _msvcrt = importlib.import_module("msvcrt")


class LockBusy(RuntimeError):
    pass


class NonBlockingFileLock:
    def __init__(self, path, label="process"):
        self.path = Path(path)
        self.label = label
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                self.handle.seek(0)
                if self.handle.read(1) == "":
                    self.handle.write("\0")
                    self.handle.flush()
                self.handle.seek(0)
                getattr(_msvcrt, "locking")(
                    self.handle.fileno(), getattr(_msvcrt, "LK_NBLCK"), 1
                )
        except (BlockingIOError, OSError) as exc:
            self.handle.close()
            raise LockBusy(f"another {self.label} owns lock {self.path}") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        else:
            self.handle.seek(0)
            getattr(_msvcrt, "locking")(
                self.handle.fileno(), getattr(_msvcrt, "LK_UNLCK"), 1
            )
        self.handle.close()
