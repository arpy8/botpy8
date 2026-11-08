"""A single-instance lock for the pipeline.

Two cycles running at once would share state.json and the project folder, so
the orchestrator holds this for the whole run. It is an OS-level lock on an
open file (flock on Linux, msvcrt on Windows): the OS drops it when the
process exits, so a crash or kill -9 never leaves a stale lock behind.
"""

import os
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AlreadyRunning(RuntimeError):
    pass


class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def __enter__(self) -> "RunLock":
        self._file = open(self.path, "a+")
        try:
            if os.name == "nt":
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            self._file = None
            raise AlreadyRunning(f"another pipeline run holds {self.path}") from None
        self._file.seek(0)
        self._file.truncate()
        self._file.write(f"{os.getpid()}\n")
        self._file.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self._file is None:
            return
        if os.name == "nt":
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None
