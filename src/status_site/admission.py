"""Admission control + realpath confinement (port of agentry-dashboard's scratchguard).

Mistake-proofing boundary checked once, before a status-site render or
write touches a managed tree:

* ``admit(root, production=...)`` canonicalises ``root`` with
  ``os.path.realpath`` exactly once and returns that spelling; callers
  use it for every later effect and never re-derive a path.
* Production vs scratch: a run must opt into production explicitly, or
  name a scratch root via ``$STATUS_SCRATCH``. With a scratch root set,
  ``root`` must resolve strictly under it.
* The managed tree must hold no symlink (root, listed subdirs, and every
  direct entry), no FIFO/socket/device, and no multi-hard-linked regular
  file (writing through it would change a file elsewhere).
* ``open_nofollow`` / ``read_nofollow`` open fixed-name leaves with
  ``O_NOFOLLOW | O_NONBLOCK`` plus an fstat regular-file check, so a link
  that appears after admission fails the open instead of being followed.
* ``freeze_sha`` / ``verify_freeze`` pin a staged page to a SHA-256 in a
  ``.FREEZE`` file.

Concurrent mutation of the tree during a render is outside the threat
model, as in the original.
"""

from __future__ import annotations

import hashlib
import os
import stat
from typing import IO

SCRATCH_ENV = "STATUS_SCRATCH"
FREEZE_NAME = ".FREEZE"

ADMIT_OK = 0
ADMIT_NO_OPT_IN = 1
ADMIT_REFUSED = 3


class NotRegularFile(OSError):
    """The leaf is a FIFO, socket, device or directory, not a regular file."""


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def check_scratch(path: str, scratch: str | None = None) -> str:
    """'' when ``path`` is an allowed scratch location, else the refusal reason."""
    scratch = os.environ.get(SCRATCH_ENV) if scratch is None else scratch
    if not scratch:
        return f"{SCRATCH_ENV} is not set"
    s = os.path.realpath(scratch)
    p = os.path.realpath(path)
    if s in (os.sep, os.path.realpath(os.path.expanduser("~"))):
        return f"refused: scratch root {scratch} is too broad"
    if p == s or not _under(p, s):
        return f"refused: {path} resolves to {p}, not under the scratch root {scratch}"
    return ""


def tree_problem(root: str, managed: tuple[str, ...] = ()) -> str:
    """'' when the managed tree holds no link / special file, else the reason.

    ``root`` itself and each ``managed`` subdirectory (relative) are
    lstat'd; every direct entry inside a managed directory is checked too.
    Missing paths are fine (created later as plain directories).
    """
    for rel in ("",) + tuple(managed):
        p = os.path.join(root, rel) if rel else root
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            return f"refused: {p} is a symbolic link"
        if not stat.S_ISDIR(st.st_mode):
            return f"refused: {p} is not a directory"
        if not rel:
            continue
        with os.scandir(p) as it:
            for e in it:
                est = e.stat(follow_symlinks=False)
                if stat.S_ISLNK(est.st_mode):
                    return f"refused: {e.path} is a symbolic link"
                if stat.S_ISREG(est.st_mode):
                    if est.st_nlink > 1:
                        return f"refused: {e.path} has {est.st_nlink} hard links"
                elif not stat.S_ISDIR(est.st_mode):
                    return (
                        f"refused: {e.path} is not a regular file or directory "
                        "(FIFO, socket or device)"
                    )
    return ""


def admit(
    root: str,
    production: bool = False,
    managed: tuple[str, ...] = (),
) -> tuple[int, str, str | None]:
    """Return ``(status, reason, canonical_root)``.

    status 0: proceed, and ``canonical_root`` is the one spelling to use.
    status 1: neither production opt-in nor scratch root set.
    status 3: confinement or link refusal.
    Reads metadata only.
    """
    scratch = os.environ.get(SCRATCH_ENV)
    if not production and not scratch:
        return (
            ADMIT_NO_OPT_IN,
            f"refused: no production opt-in and no {SCRATCH_ENV}; nothing read or written",
            None,
        )
    if os.path.islink(root):
        return ADMIT_REFUSED, f"refused: the root {root} is a symbolic link", None
    canon = os.path.realpath(root)
    if scratch:
        for p in (canon,) + tuple(os.path.join(canon, d) for d in managed):
            why = check_scratch(p, scratch)
            if why:
                return ADMIT_REFUSED, why, None
    why = tree_problem(canon, managed)
    if why:
        return ADMIT_REFUSED, why, None
    return ADMIT_OK, "", canon


def _regular(fd: int, path: str) -> int:
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(f"refused: {path} is not a regular file")
        os.set_blocking(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_nofollow(path: str, mode: str = "w", exclusive: bool = False) -> IO:
    """Open ``path`` for writing without following a leaf symlink or blocking on a FIFO.

    ``exclusive=True`` (fixed-name temp files) unlinks any stale entry
    first, then creates with ``O_EXCL``. Otherwise the file is truncated
    only after the regular-file check.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    if exclusive:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        flags |= os.O_EXCL
    fd = _regular(os.open(path, flags, 0o644), path)
    if not exclusive:
        os.ftruncate(fd, 0)
    return os.fdopen(fd, mode)


def read_nofollow(path: str, mode: str = "r") -> IO:
    """Open ``path`` for reading; a link, FIFO, socket or device is refused at once."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    return os.fdopen(_regular(fd, path), mode)


def freeze_sha(page_path: str) -> str:
    """SHA-256 hex of the staged page, read without following links."""
    h = hashlib.sha256()
    with read_nofollow(page_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_freeze(dir_path: str, page_name: str) -> str:
    """Pin ``dir_path/page_name`` by writing its SHA to ``dir_path/.FREEZE``."""
    sha = freeze_sha(os.path.join(dir_path, page_name))
    with open_nofollow(os.path.join(dir_path, FREEZE_NAME)) as f:
        f.write(f"{sha}  {page_name}\n")
    return sha


def verify_freeze(dir_path: str, page_name: str) -> str:
    """'' when no ``.FREEZE`` exists or the page matches it, else the refusal reason."""
    fpath = os.path.join(dir_path, FREEZE_NAME)
    try:
        with read_nofollow(fpath) as f:
            pinned = f.read().split()
    except FileNotFoundError:
        return ""
    except OSError as e:
        return f"refused: cannot read {fpath}: {e}"
    if not pinned:
        return f"refused: {fpath} is empty"
    actual = freeze_sha(os.path.join(dir_path, page_name))
    if pinned[0] != actual:
        return f"refused: {page_name} does not match the SHA pinned in {FREEZE_NAME}"
    return ""
