"""request_host_claude — Speaking ↔ host-side Claude CLI bridge.

The worker container has no `claude` binary and no host filesystem access,
so Speaking can't shell out to the Claude CLI directly. Instead, she writes
a markdown task spec into a shared spool (`/state/worker/host-claude/inbox/`),
a host-side daemon (`sandbox/host-claude-watcher/`) picks it up, runs
`claude --print` against the body, and drops the captured stdout/stderr
into `outbox/<same-id>.md`.

This module provides:

- ``request_host_claude_from_args`` — the core implementation. Writes the
  inbox file atomically and optionally polls the outbox until the daemon
  finishes (or the per-request ``timeout_seconds + 60`` budget elapses).
- ``build`` — MCP tool factory matching the ``alice_speaking.tools`` pattern
  (closes over Config, returns a list of ``SdkMcpTool``).
- ``main`` — small CLI wrapper (``alice-host-claude-request``) for shell-
  side debugging without spinning up the full MCP server.

The on-disk file shape is the load-bearing contract here, not the Python
function signatures. Both ends (this tool, the bash daemon) must keep
the frontmatter keys and the ``# Stdout`` / ``# Stderr`` section headers
in sync — see ``sandbox/host-claude-watcher/README.md``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import uuid
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from alice_core.config.personae import Personae, placeholder as placeholder_personae

from ..infra.config import Config


# Shared bind-mount dir between the worker container and the host daemon.
# Override via env for tests / alternate deployments.
DEFAULT_HOST_CLAUDE_ROOT = pathlib.Path(
    os.environ.get("ALICE_HOST_CLAUDE_ROOT", "/state/worker/host-claude")
)

# Outbox poll cadence — 2s matches the responsiveness target in the task
# spec while keeping the worker's syscall rate reasonable on the shared
# volume.
POLL_INTERVAL_SECONDS = 2.0

# Slack budget added on top of the request's ``timeout_seconds`` when
# polling the outbox. Covers daemon scheduling latency + write fsync,
# both well under a minute on a healthy host.
WAIT_SLACK_SECONDS = 60


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {text}"}], "isError": True}


def _slugify(s: str, max_len: int = 40) -> str:
    """Strip a prompt down to its first-six-words slug.

    Matches the speaking-tools convention (see inner._slugify) — lowercased,
    non-alphanumeric collapsed to hyphens, trimmed. We grab the first six
    words rather than all of them because the filename has to stay short
    enough to read at a glance in `ls`.
    """
    words = re.findall(r"[A-Za-z0-9]+", s)[:6]
    slug = "-".join(w.lower() for w in words)
    return (slug or "untitled")[:max_len]


def _utc_stamp() -> str:
    """ISO-ish UTC stamp safe for filenames (colons → dashes)."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H-%M-%SZ")
    )


def _utc_iso() -> str:
    """RFC3339-ish UTC stamp for frontmatter values."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _build_inbox_markdown(
    *,
    request_id: str,
    prompt: str,
    urgency: str,
    timeout_seconds: int,
    allow_destructive: bool,
) -> str:
    """Render the inbox file body. Frontmatter first, then `# Task` body."""
    return (
        "---\n"
        f"id: {request_id}\n"
        "requested_by: speaking\n"
        f"created_at: {_utc_iso()}\n"
        f"urgency: {urgency}\n"
        f"timeout_seconds: {int(timeout_seconds)}\n"
        f"allow_destructive: {str(bool(allow_destructive)).lower()}\n"
        "---\n"
        "# Task\n"
        "\n"
        f"{prompt.rstrip()}\n"
    )


def _atomic_write(path: pathlib.Path, content: str) -> None:
    """tempfile+rename in the same dir → atomic on POSIX. The daemon's
    inotify watcher fires on close_write/moved_to, so it never sees a
    half-written file with this pattern."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile in the target directory, delete=False so we own
    # the rename. tempfile uses mkstemp under the hood — fd is closed
    # cleanly before rename.
    fd, tmp_str = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = pathlib.Path(tmp_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        # On failure, clear out the staging file so we don't leak crumbs.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _parse_outbox(path: pathlib.Path) -> dict[str, Any]:
    """Parse the daemon's outbox markdown.

    Shape:
      ---
      id: ...
      status: success|failure|timeout
      started_at: ...
      finished_at: ...
      exit_code: <int>
      ---
      # Stdout

      <captured stdout>

      # Stderr

      <captured stderr>

    Returns a dict with at least: ``id``, ``status``, ``exit_code``,
    ``stdout``, ``stderr``, ``started_at``, ``finished_at``. Unknown
    frontmatter keys pass through untouched.
    """
    text = path.read_text()
    fm: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        close = text.find("\n---\n", 4)
        if close != -1:
            for line in text[4:close].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip()
            body = text[close + 5 :]

    # Split body on the `# Stdout` and `# Stderr` headers. We tolerate
    # extra blank lines between sections.
    stdout = ""
    stderr = ""
    parts = re.split(r"(?m)^# (Stdout|Stderr)\s*$", body)
    # re.split with one capture group yields: [prefix, "Stdout", content,
    # "Stderr", content, ...]. The prefix is normally empty / whitespace.
    i = 1
    while i + 1 < len(parts):
        section = parts[i]
        content = parts[i + 1].strip("\n")
        if section == "Stdout":
            stdout = content
        elif section == "Stderr":
            stderr = content
        i += 2

    exit_code_raw = fm.get("exit_code", "")
    try:
        exit_code: int | None = int(exit_code_raw)
    except (TypeError, ValueError):
        exit_code = None

    return {
        "id": fm.get("id", ""),
        "status": fm.get("status", "unknown"),
        "started_at": fm.get("started_at", ""),
        "finished_at": fm.get("finished_at", ""),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def request_host_claude_from_args(
    args: dict,
    *,
    root: pathlib.Path | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    now: callable | None = None,
) -> dict[str, Any]:
    """Core implementation. Used by both the MCP tool and the CLI wrapper.

    Parameters
    ----------
    args
        ``prompt`` (required), ``urgency``, ``timeout_seconds``,
        ``allow_destructive``, ``wait`` — same shape as the MCP tool.
    root
        Override the spool location (defaults to ``ALICE_HOST_CLAUDE_ROOT``
        env / ``/state/worker/host-claude``). Used by tests.
    poll_interval
        Outbox poll cadence in seconds. Tests can drop this near zero.
    now
        Optional ``() -> float`` clock injection so tests can fake elapsed
        time without sleeping. Defaults to ``time.monotonic``.

    Returns
    -------
    A plain dict shaped for the MCP content payload. Keys:
      - ``request_id`` (always)
      - ``inbox_path`` (always)
      - if ``wait=True``: ``status``, ``stdout``, ``stderr``, ``exit_code``,
        ``started_at``, ``finished_at``, ``outbox_path``
      - on timeout-while-waiting: ``status="timeout"`` with empty
        stdout/stderr
    """
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    urgency_raw = (args.get("urgency") or "normal").strip().lower()
    if urgency_raw not in ("normal", "high"):
        raise ValueError(
            f"urgency must be 'normal' or 'high', got {urgency_raw!r}"
        )
    timeout_seconds_raw = args.get("timeout_seconds", 600)
    try:
        timeout_seconds = int(timeout_seconds_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"timeout_seconds must be an integer, got {timeout_seconds_raw!r}"
        ) from exc
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    allow_destructive = bool(args.get("allow_destructive", False))
    wait = bool(args.get("wait", True))

    base = pathlib.Path(root) if root is not None else DEFAULT_HOST_CLAUDE_ROOT
    inbox_dir = base / "inbox"
    outbox_dir = base / "outbox"

    # request_id = UTC stamp + slug + short uuid suffix. The uuid suffix
    # avoids same-second collisions between two near-simultaneous requests
    # that happen to share a slug.
    request_id = f"{_utc_stamp()}-{_slugify(prompt)}-{uuid.uuid4().hex[:6]}"
    inbox_path = inbox_dir / f"{request_id}.md"
    body = _build_inbox_markdown(
        request_id=request_id,
        prompt=prompt,
        urgency=urgency_raw,
        timeout_seconds=timeout_seconds,
        allow_destructive=allow_destructive,
    )
    _atomic_write(inbox_path, body)

    result: dict[str, Any] = {
        "request_id": request_id,
        "inbox_path": str(inbox_path),
    }
    if not wait:
        return result

    clock = now or time.monotonic
    outbox_path = outbox_dir / f"{request_id}.md"
    deadline = clock() + float(timeout_seconds + WAIT_SLACK_SECONDS)
    while clock() < deadline:
        if outbox_path.is_file():
            parsed = _parse_outbox(outbox_path)
            result.update(parsed)
            result["outbox_path"] = str(outbox_path)
            return result
        time.sleep(poll_interval)

    # Timed out waiting. The daemon may still be working — the inbox file
    # is still queued. Return a synthetic timeout result so the caller can
    # decide whether to retry / surface.
    result.update(
        {
            "status": "timeout",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "started_at": "",
            "finished_at": "",
            "outbox_path": str(outbox_path),
        }
    )
    return result


# JSON Schema for the MCP tool. Longhand form because the SDK shorthand
# treats every key as required; we want defaults on everything except
# ``prompt``.
_REQUEST_HOST_CLAUDE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": (
                "The task body to feed Claude on the host. Markdown is "
                "fine. This is the literal text Claude will see as its "
                "first user turn (with --max-turns 50)."
            ),
        },
        "urgency": {
            "type": "string",
            "enum": ["normal", "high"],
            "description": (
                "Routing hint for the host daemon. 'high' is reserved for "
                "future priority-queue handling; today it's logged but the "
                "daemon serves FIFO."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "description": (
                "Wall-clock cap for the host-side claude invocation. The "
                "daemon enforces this with timeout(1). The tool also waits "
                "up to timeout_seconds + 60 for the outbox file when "
                "wait=True. Default 600 (10 minutes)."
            ),
        },
        "allow_destructive": {
            "type": "boolean",
            "description": (
                "Hint to the host-side prompt that destructive operations "
                "(git push, rm -rf, etc.) are permitted for this task. "
                "Currently advisory — the daemon does not enforce it. "
                "Default false."
            ),
        },
        "wait": {
            "type": "boolean",
            "description": (
                "If true (default), block on the outbox until the daemon "
                "writes the result or timeout_seconds + 60s elapses. If "
                "false, return immediately with just the request_id and "
                "inbox_path; the caller is responsible for polling the "
                "outbox later."
            ),
        },
    },
    "required": ["prompt"],
}


def build(
    cfg: Config,
    *,
    personae: Personae | None = None,
    root: pathlib.Path | None = None,
) -> list[SdkMcpTool[Any]]:
    """Build the host-claude tool list.

    ``cfg`` is accepted for parity with the rest of ``alice_speaking.tools``
    even though this tool doesn't read it directly — the spool location
    is environment / argument driven, not Config-driven (the worker and
    daemon must agree on the same hard-coded path on both sides).
    """
    del cfg  # parity with sibling builders
    p = personae or placeholder_personae()
    agent = p.agent.name
    spool_root = root

    @tool(
        name="request_host_claude",
        description=(
            f"Dispatch a task to a Claude CLI running on the host. Writes a "
            f"markdown spec into /state/worker/host-claude/inbox/; a "
            f"host-side daemon picks it up, runs `claude --print` against "
            f"the body, and drops stdout+stderr in outbox/<id>.md. Use this "
            f"when {agent} needs Claude to do something that requires the "
            f"host filesystem, host PATH, or fresh-context isolation from "
            f"her own conversation. Default wait=true blocks up to "
            f"timeout_seconds + 60 for the result. Returns status / "
            f"stdout / stderr / exit_code on success; status=\"timeout\" if "
            f"the daemon doesn't write an outbox file in time."
        ),
        input_schema=_REQUEST_HOST_CLAUDE_SCHEMA,
    )
    async def request_host_claude(args: dict) -> dict:
        try:
            result = request_host_claude_from_args(args, root=spool_root)
        except ValueError as exc:
            return _err(str(exc))
        except OSError as exc:
            return _err(f"disk write failure: {exc}")
        # Return the structured result both as a single JSON content block
        # (for the agent to read) and via the dict shape — the SDK only
        # serializes the `content` field, so we stash the JSON there.
        return _ok(json.dumps(result, indent=2))

    return [request_host_claude]


# ---------------------------------------------------------------------------
# CLI wrapper. Exposed via pyproject.scripts as `alice-host-claude-request`
# for shell-side debugging without going through the MCP server.
# ---------------------------------------------------------------------------


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alice-host-claude-request",
        description=(
            "Drop a task spec into /state/worker/host-claude/inbox/ for the "
            "host-side Claude daemon to pick up. Same on-disk shape as the "
            "request_host_claude MCP tool."
        ),
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help=(
            "The task body. Use '-' to read from stdin. Required (or pass "
            "via stdin)."
        ),
    )
    parser.add_argument(
        "--urgency",
        choices=["normal", "high"],
        default="normal",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="timeout_seconds (default 600).",
    )
    parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Set allow_destructive=true in the frontmatter.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Block on the outbox; print the parsed result as JSON.",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=None,
        help=(
            "Override the spool root (default: $ALICE_HOST_CLAUDE_ROOT or "
            "/state/worker/host-claude)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli_parser()
    ns = parser.parse_args(argv)

    if ns.prompt is None or ns.prompt == "-":
        prompt_text = sys.stdin.read()
    else:
        prompt_text = ns.prompt
    if not prompt_text.strip():
        parser.error("prompt is empty (pass an argument or pipe text on stdin)")

    try:
        result = request_host_claude_from_args(
            {
                "prompt": prompt_text,
                "urgency": ns.urgency,
                "timeout_seconds": ns.timeout,
                "allow_destructive": ns.allow_destructive,
                "wait": ns.wait,
            },
            root=ns.root,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: disk write failure: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


__all__ = [
    "build",
    "main",
    "request_host_claude_from_args",
    "DEFAULT_HOST_CLAUDE_ROOT",
]
