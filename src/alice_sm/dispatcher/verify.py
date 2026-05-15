"""sm:reviewing → sm:done verification (smoke-test) gate (issue #128).

After CI is green on the merge commit but before the dispatcher relabels
``sm:reviewing → sm:done``, an artifact-specific smoke test runs. CI
catches regressions inside the source tree; this third tier confirms
the actually-running system reflects the change.

v1 (this module's only recipe) probes a configured viewer URL and
asserts a marker substring is present in the body. Everything else is
``skip`` — audited but allowed through. The :func:`default_verifier`
dispatch table makes adding new recipes a one-line change.

:func:`_verify_enabled` is the operator-facing kill-switch read from
``ALICE_VERIFY_ENABLED``; the dispatcher's :func:`run` checks it before
binding the production verifier.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable

from alice_sm.dispatcher.constants import (
    VERIFY_ENABLED_ENV,
    VERIFY_HTTP_TIMEOUT_SECONDS,
    VERIFY_VIEWER_MARKER_DEFAULT,
    VERIFY_VIEWER_MARKER_ENV,
    VERIFY_VIEWER_PATH_PREFIX,
    VERIFY_VIEWER_URL_DEFAULT,
    VERIFY_VIEWER_URL_ENV,
)


def _http_get_body(
    url: str,
    *,
    timeout: float = VERIFY_HTTP_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[int, str]:
    """Issue a GET, return ``(status, body_text)``.

    Wraps :func:`urllib.request.urlopen` so tests can inject a fake
    opener and avoid actual network I/O. Decodes the body as UTF-8 with
    ``errors='replace'`` — the marker check is a substring match so
    mojibake on the boundary won't matter.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "alice-sm-verify/1"})
    with opener(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        raw = resp.read()
    if isinstance(raw, bytes):
        body = raw.decode("utf-8", errors="replace")
    else:
        body = str(raw)
    return status, body


def verify_viewer_route(
    *,
    url: str,
    marker: str,
    http_get: Callable[[str], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Run the viewer-route smoke test, return a verdict dict.

    Verdict keys:
      - ``outcome``: ``"pass"`` or ``"fail"``
      - ``reason``: short human string (populated on fail)
      - ``route``: URL probed (populated on pass; included on fail for
        the audit comment so Jason can replay it manually)

    Failure modes that count as a *fail* (not a transient bail-out):
      - Connection refused / timeout / DNS error
      - Non-2xx HTTP status
      - 2xx but the marker substring isn't in the response body

    The verifier never raises; transport errors are caught and
    reported as ``outcome="fail"`` so the dispatcher can post the
    ``verify-failed`` audit comment and leave the issue at
    ``sm:reviewing`` for a human to inspect.
    """
    getter = http_get or _http_get_body
    try:
        status, body = getter(url)
    except (urllib.error.URLError, OSError) as exc:
        return {
            "outcome": "fail",
            "reason": f"viewer probe failed: {exc.__class__.__name__}: {exc}",
            "route": url,
        }
    except Exception as exc:  # pragma: no cover — defensive
        return {
            "outcome": "fail",
            "reason": f"viewer probe raised {exc.__class__.__name__}: {exc}",
            "route": url,
        }
    if not (200 <= int(status) < 300):
        return {
            "outcome": "fail",
            "reason": f"viewer probe HTTP {status}",
            "route": url,
        }
    if marker not in body:
        return {
            "outcome": "fail",
            "reason": f"marker {marker!r} not found in response body",
            "route": url,
        }
    return {"outcome": "pass", "reason": "viewer marker present", "route": url}


def _touches_viewer(files: Iterable[str]) -> bool:
    return any(p.startswith(VERIFY_VIEWER_PATH_PREFIX) for p in files)


def default_verifier(
    pr_number: int,
    files: list[str],
    *,
    viewer_url: str | None = None,
    viewer_marker: str | None = None,
    http_get: Callable[[str], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Default issue-#128 verification recipe dispatcher.

    Picks a verification recipe based on what the merged PR touched.
    v1 only ships the *viewer-route* recipe; anything else returns
    ``outcome="skip"`` with a recipe-not-matched reason so the
    dispatcher can still close the issue (audit-trail visible) without
    pretending we ran a check we didn't.

    Wired into :func:`run` via the ``verify_pr`` keyword argument so
    tests can inject a recipe stub that doesn't open sockets.
    """
    url = viewer_url or os.environ.get(VERIFY_VIEWER_URL_ENV, VERIFY_VIEWER_URL_DEFAULT)
    marker = viewer_marker or os.environ.get(
        VERIFY_VIEWER_MARKER_ENV, VERIFY_VIEWER_MARKER_DEFAULT
    )
    if _touches_viewer(files):
        return verify_viewer_route(url=url, marker=marker, http_get=http_get)
    # Future recipes (dispatcher --check, speaking enqueue-and-assert,
    # research-note path-exists) extend this branch. Until then,
    # anything outside the viewer touch is treated as "no recipe
    # matched" and allowed through with a verify-skip audit comment.
    return {
        "outcome": "skip",
        "reason": "no verification recipe matched (no src/alice_viewer/ files in PR)",
        "route": None,
    }


def _verify_enabled() -> bool:
    raw = os.environ.get(VERIFY_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")
