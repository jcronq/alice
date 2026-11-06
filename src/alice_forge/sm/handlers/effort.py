"""Amendment handler for the ``[SM] effort=<level>`` verb (issue #642).

The effort dial is an amendment, not a state transition — posting
``[SM] effort=max`` on an issue doesn't move it between SM states, it
just tunes the rules block the next dispatcher spawn injects into the
worker prompt. Because of that, the verb is deliberately absent from
:data:`alice_forge.sm.transitions.TRANSITIONS` and this module does
not expose the standard ``handle(issue, services)`` signature the
per-state handlers use.

Slice-1 scope: the primary read path is
:func:`alice_forge.dispatcher.effort.read_effort`, which scans the
issue's labels + comments directly and returns the effective level.
This module adds a small on-disk marker so operators (and later
slices) can see at a glance what the dispatcher last observed for a
given issue — "did the amendment land yet?" — without re-running the
label/comment scan.

The marker is a plain text file at
``<state_dir>/effort/<repo>/<issue_number>`` containing one line:
the effort level. The parent dirs are created on first write; the
file is fully rewritten (not appended) so the latest level is
always the one on disk. Missing / unreadable marker files are not
errors; the authoritative source stays the issue itself.
"""

from __future__ import annotations

import pathlib

from alice_forge.dispatcher.constants import DEFAULT_STATE_DIR
from alice_forge.dispatcher.effort import Effort, LEVELS


#: On-disk root for the per-issue effort markers. Scoped under the
#: dispatcher's existing ``/state/worker`` directory so container
#: rebuilds that preserve ``/state/worker`` survive. Importable
#: separately so tests can point it at a tmp_path.
EFFORT_MARKER_ROOT: pathlib.Path = DEFAULT_STATE_DIR / "effort"


def _sanitize_repo(repo: str) -> str:
    """Flatten ``owner/name`` into a filesystem-safe segment.

    GitHub's ``owner/repo`` embeds a ``/`` that would otherwise create
    an extra directory level. We'd rather keep each repo as a single
    segment so an operator can ``ls /state/worker/effort/`` and see
    the whole set at once.
    """
    return repo.replace("/", "__")


def marker_path(
    repo: str,
    issue_number: int,
    *,
    root: pathlib.Path = EFFORT_MARKER_ROOT,
) -> pathlib.Path:
    """Return the on-disk marker path for ``repo#issue_number``."""
    return root / _sanitize_repo(repo) / str(issue_number)


def ack_effort_amendment(
    repo: str,
    issue_number: int,
    level: Effort,
    *,
    root: pathlib.Path = EFFORT_MARKER_ROOT,
) -> pathlib.Path:
    """Record that a ``[SM] effort=<level>`` amendment has been observed.

    Writes the level to the per-issue marker file, creating parent
    directories as needed. Returns the marker path so callers can
    assert on it in tests.

    Raises :class:`ValueError` on an unknown level — the dispatcher
    should never call this with a non-:data:`LEVELS` value, so a
    bad level is a bug in the caller rather than a user-facing issue.
    """
    if level not in LEVELS:
        raise ValueError(
            f"effort level {level!r} not in {LEVELS!r} — "
            f"caller passed an unvalidated token"
        )
    path = marker_path(repo, issue_number, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{level}\n")
    return path


def read_marker(
    repo: str,
    issue_number: int,
    *,
    root: pathlib.Path = EFFORT_MARKER_ROOT,
) -> Effort | None:
    """Return the level recorded by the last ack, or ``None`` if unset.

    Not an authoritative read — the dispatcher's primary source is
    :func:`alice_forge.dispatcher.effort.read_effort`. This helper
    exists for operators and for later slices (status-site pill, worker
    self-report) that want the last-observed level without re-running
    the gh calls.
    """
    path = marker_path(repo, issue_number, root=root)
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if text in LEVELS:
        return text  # type: ignore[return-value]
    return None
