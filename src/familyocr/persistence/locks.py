"""One writer per document stage.

WAL and a busy timeout let two processes write the same database without either
one failing. That is what we want when the two are working on *different*
volumes, and exactly what we do not want when they are working on the same one:
two `segment` runs on one document interleave their delete-then-insert, and the
result is a silently doubled set of regions that no foreign key rejects.

So the database lock is not the right granularity — the stage is. A stage takes
an exclusive advisory lock on (document, stage) for as long as it runs, and a
second run refuses to start rather than corrupting the first one's output.

The lock is a file lock, not a table row: it is released by the kernel when the
process dies, so a killed run never leaves the document permanently locked.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
from contextlib import contextmanager
from pathlib import Path


class StageBusy(RuntimeError):
    """Another process holds this document's stage lock."""


def _slug(text: str) -> str:
    """A filename-safe stand-in; document ids are CJK and may contain spaces."""
    return re.sub(r"[^0-9A-Za-z一-鿿_.-]", "_", text) or "_"


def lock_path(artifacts: Path, document_id: str, stage: str) -> Path:
    return artifacts / "locks" / f"{_slug(document_id)}.{_slug(stage)}.lock"


@contextmanager
def stage_lock(artifacts: Path, document_id: str, stage: str):
    """Hold the (document, stage) lock, or raise `StageBusy` immediately.

    Non-blocking on purpose: a second run of the same stage is nearly always a
    mistake — a duplicate launch, or a retry of something still running — and
    waiting for it would only hide that.
    """
    path = lock_path(artifacts, document_id, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise StageBusy(
                    f"another `{stage}` is already running on {document_id}. "
                    f"Wait for it to finish, or stop it before retrying."
                ) from exc
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
