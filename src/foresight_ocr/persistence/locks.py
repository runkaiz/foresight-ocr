"""One pipeline writer per document.

WAL and a busy timeout let two processes write the same database without either
one failing. That is what we want when the two are working on *different*
volumes, and exactly what we do not want when they are working on the same one:
two `segment` runs on one document interleave their delete-then-insert, and the
result is a silently doubled set of regions that no foreign key rejects.

So the database lock is not the right granularity — the document pipeline is.
Every mutating stage takes one exclusive advisory lock for its document, and a
second stage on that document refuses to start rather than reading or replacing
half-written output. Different documents remain fully parallel.

The lock is a file lock, not a table row: it is released by the kernel when the
process dies, so a killed run never leaves the document permanently locked.
"""

from __future__ import annotations

import errno
import importlib
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

native_locks: Any = importlib.import_module("msvcrt" if os.name == "nt" else "fcntl")


class StageBusy(RuntimeError):
    """Another process holds this document's pipeline lock."""


def _slug(text: str) -> str:
    """A filename-safe stand-in; document ids are CJK and may contain spaces."""
    return re.sub(r"[^0-9A-Za-z一-鿿_.-]", "_", text) or "_"


def lock_path(artifacts: Path, document_id: str) -> Path:
    return artifacts / "locks" / f"{_slug(document_id)}.pipeline.lock"


def _acquire(fd: int) -> None:
    """Take one non-blocking byte-range lock using the host's native API."""
    if os.name == "nt":
        # Windows cannot lock an empty byte range. Creating the sentinel before
        # the attempt is safe: the lock, not the file contents, is authoritative.
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        native_locks.locking(fd, native_locks.LK_NBLCK, 1)
    else:
        native_locks.flock(fd, native_locks.LOCK_EX | native_locks.LOCK_NB)


def _release(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        native_locks.locking(fd, native_locks.LK_UNLCK, 1)
    else:
        native_locks.flock(fd, native_locks.LOCK_UN)


@contextmanager
def stage_lock(artifacts: Path, document_id: str, stage: str):
    """Hold the document's pipeline lock, or raise `StageBusy` immediately.

    Non-blocking on purpose: overlapping dependent stages can consume or replace
    half-written output, and waiting would hide an accidental duplicate launch.
    """
    path = lock_path(artifacts, document_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            _acquire(fd)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise StageBusy(
                    f"another pipeline stage is already writing {document_id}; "
                    f"cannot start `{stage}`. Wait for it to finish, or stop it "
                    "before retrying."
                ) from exc
            raise
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"pid={os.getpid()} stage={stage}\n".encode())
        try:
            yield
        finally:
            _release(fd)
    finally:
        os.close(fd)
