"""Dispatcher errors.

:class:`GHCommandError` is raised by every ``gh`` invocation that exits
non-zero. The dispatcher main loop sniffs the error for auth-failure /
rate-limit heuristics so a transient credentials hiccup bails the pass
rather than re-firing audit comments against a stale state file.
"""

from __future__ import annotations


class GHCommandError(RuntimeError):
    """Raised when a ``gh`` invocation exits non-zero.

    Mirrors :class:`watchers.github.GHCommandError` — we keep the
    stderr around so the auth-failure / rate-limit heuristic has
    something to sniff.
    """

    def __init__(self, returncode: int, stderr: str, args: list[str]) -> None:
        super().__init__(f"gh exited {returncode}: {stderr.strip()[:400]}")
        self.returncode = returncode
        self.stderr = stderr
        self.args = args

    @property
    def looks_like_auth_failure(self) -> bool:
        msg = self.stderr.lower()
        return any(
            needle in msg
            for needle in (
                "401",
                "403",
                "bad credentials",
                "requires authentication",
                "must authenticate",
                "auth login",
            )
        )

    @property
    def looks_like_rate_limit(self) -> bool:
        msg = self.stderr.lower()
        return any(
            needle in msg
            for needle in (
                "rate limit",
                "secondary rate limit",
                "api rate limit exceeded",
            )
        )
