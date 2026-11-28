import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tjentic.errors import TjenticError


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Release the cross-process lock on exit, including after a process crash."""
    with path.open("a+b") as lock:
        try:
            if sys.platform == "win32":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise TjenticError(f"Another operation holds {path}.") from error
        yield
