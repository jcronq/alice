"""Tools for Alice's inner/ comms layer.

Semantic affordances so Alice thinks in terms of "the directive" and "my notes"
rather than filesystem paths. Each tool closes over the Config at build time.
"""

from __future__ import annotations

import datetime
import re
import shutil
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ..config import Config


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {text}"}], "isError": True}


def _slugify(s: str, max_len: int = 40) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return (s or "untitled")[:max_len]


def _stamp_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d-%H%M%S")


def build(cfg: Config) -> list[SdkMcpTool[Any]]:
    inner_dir = cfg.mind_dir / "inner"
    directive_path = inner_dir / "directive.md"
    notes_dir = inner_dir / "notes"
    thoughts_dir = inner_dir / "thoughts"
    surface_dir = inner_dir / "surface"

    @tool(
        name="read_directive",
        description=(
            "Read the current directive.md — thinking's standing orders. "
            "Use this when deciding whether to leave a note or rewrite the directive."
        ),
        input_schema={},
    )
    async def read_directive(args: dict) -> dict:
        if not directive_path.is_file():
            return _ok("(directive.md not yet created)")
        return _ok(directive_path.read_text())

    @tool(
        name="write_directive",
        description=(
            "Rewrite directive.md with new content. Use sparingly — this "
            "redirects thinking Alice's focus. Prefer append_note for one-offs."
        ),
        input_schema={"content": str},
    )
    async def write_directive(args: dict) -> dict:
        content = args.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return _err("content must be a non-empty string")
        directive_path.parent.mkdir(parents=True, exist_ok=True)
        directive_path.write_text(content)
        return _ok(f"directive rewritten ({len(content)} chars)")

    @tool(
        name="append_note",
        description=(
            "Drop a fleeting note into inner/notes/ for thinking to consume on "
            "her next wake. For observations, questions, or things worth "
            "remembering that don't yet belong in permanent memory."
        ),
        input_schema={"content": str, "tag": str},
    )
    async def append_note(args: dict) -> dict:
        content = args.get("content", "")
        tag = args.get("tag", "").strip()
        if not isinstance(content, str) or not content.strip():
            return _err("content must be a non-empty string")
        slug = _slugify(tag or content.split("\n", 1)[0])
        notes_dir.mkdir(parents=True, exist_ok=True)
        path = notes_dir / f"{_stamp_utc()}-{slug}.md"
        header = f"# note — {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        if tag:
            header += f"tag: {tag}\n"
        path.write_text(header + "\n" + content.rstrip() + "\n")
        return _ok(f"note written: {path.relative_to(cfg.mind_dir)}")

    @tool(
        name="read_notes",
        description=(
            "List unconsumed fleeting notes (what thinking will see on her next "
            "wake). Returns paths + first line of each. Optional since: ISO date."
        ),
        input_schema={"since": str, "limit": int},
    )
    async def read_notes(args: dict) -> dict:
        since = _parse_since(args.get("since"))
        limit = int(args.get("limit") or 20)
        if not notes_dir.is_dir():
            return _ok("(no notes/ dir)")
        entries = sorted(
            (p for p in notes_dir.glob("*.md") if since is None or p.stat().st_mtime >= since),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        if not entries:
            return _ok("(no notes)")
        lines = [f"{p.relative_to(cfg.mind_dir)}: {_first_nonempty(p)}" for p in entries]
        return _ok("\n".join(lines))

    @tool(
        name="read_thoughts",
        description=(
            "List recent items from inner/thoughts/ — what thinking has "
            "produced. Returns paths and first line of each. Reading here is "
            "how speaking Alice 'recalls what she's been thinking about'."
        ),
        input_schema={"since": str, "limit": int},
    )
    async def read_thoughts(args: dict) -> dict:
        since = _parse_since(args.get("since"))
        limit = int(args.get("limit") or 20)
        if not thoughts_dir.is_dir():
            return _ok("(no thoughts/ yet)")
        entries: list[Path] = [
            p
            for p in thoughts_dir.rglob("*.md")
            if since is None or p.stat().st_mtime >= since
        ]
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        entries = entries[:limit]
        if not entries:
            return _ok("(no thoughts match)")
        lines = [f"{p.relative_to(cfg.mind_dir)}: {_first_nonempty(p)}" for p in entries]
        return _ok("\n".join(lines))

    @tool(
        name="resolve_surface",
        description=(
            "Conclude a surface turn. Moves the surface file into "
            "inner/surface/.handled/ with your verdict + action appended, "
            "keeping a record of the hemispheres' dialogue. `id` is the "
            "surface filename (not full path)."
        ),
        input_schema={"id": str, "verdict": str, "action_taken": str},
    )
    async def resolve_surface(args: dict) -> dict:
        sid = (args.get("id") or "").strip()
        verdict = (args.get("verdict") or "").strip()
        action = (args.get("action_taken") or "").strip()
        if not sid:
            return _err("id required (surface filename)")
        src = surface_dir / sid
        if not src.is_file():
            return _err(f"surface not found: {sid}")
        today = datetime.date.today().isoformat()
        dest_dir = surface_dir / ".handled" / today
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / sid
        body = src.read_text()
        trailer = (
            "\n\n---\nresolved: "
            + datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            + f"\nverdict: {verdict or '(none)'}\naction_taken: {action or '(none)'}\n"
        )
        dest.write_text(body + trailer)
        src.unlink()
        return _ok(f"surface resolved → {dest.relative_to(cfg.mind_dir)}")

    return [
        read_directive,
        write_directive,
        append_note,
        read_notes,
        read_thoughts,
        resolve_surface,
    ]


# -- helpers ------------------------------------------------------------------


def _parse_since(value: Any) -> float | None:
    if not value:
        return None
    try:
        return time.mktime(datetime.datetime.fromisoformat(str(value)).timetuple())
    except (ValueError, TypeError):
        return None


def _first_nonempty(path: Path, cap: int = 120) -> str:
    try:
        for line in path.read_text().splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:cap]
    except OSError:
        pass
    return "(empty)"


__all__ = ["build"]
