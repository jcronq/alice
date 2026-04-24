"""Memory tools — read/write into Alice's permanent knowledge at memory/.

Phase-6 MVP: glob-based read + quick write. Graph traversal via [[wikilinks]]
is sketched in HEMISPHERES.md but deferred — thinking Alice can use Read/Grep
meanwhile, and we'll add a dedicated `read_memory_link` once it's clear what
shape works in practice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ..config import Config


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {text}"}], "isError": True}


def build(cfg: Config) -> list[SdkMcpTool[Any]]:
    memory_dir = cfg.mind_dir / "memory"
    PREVIEW_CAP = 4000

    @tool(
        name="read_memory",
        description=(
            "Read from Alice's permanent memory/ by path or glob. `pattern` is "
            "relative to memory/ (e.g., 'fitness/CURRENT-WEIGHTS.md' or "
            "'*/user_jason.md' or 'cozyhem/**/*.md'). Returns the content of a "
            "single match verbatim, or a listing of first lines for multi-match."
        ),
        input_schema={"pattern": str},
    )
    async def read_memory(args: dict) -> dict:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            return _err("pattern required")
        if not memory_dir.is_dir():
            return _err("memory/ does not exist")
        matches = sorted(memory_dir.glob(pattern))
        if not matches:
            return _ok(f"(no match for {pattern} under memory/)")
        if len(matches) == 1 and matches[0].is_file():
            body = matches[0].read_text()
            return _ok(_truncate(body, PREVIEW_CAP, matches[0]))
        lines: list[str] = []
        for p in matches[:40]:
            if p.is_dir():
                lines.append(f"{p.relative_to(memory_dir)}/  (dir)")
            else:
                lines.append(f"{p.relative_to(memory_dir)}: {_first_nonempty(p)}")
        more = "" if len(matches) <= 40 else f"\n…and {len(matches) - 40} more"
        return _ok("\n".join(lines) + more)

    @tool(
        name="write_memory",
        description=(
            "Write a file under memory/. Path is relative to memory/. Creates "
            "parent directories. Overwrites if present — use read_memory first "
            "if you care about prior content. For quick facts; consolidation/"
            "grooming is thinking Alice's job."
        ),
        input_schema={"path": str, "content": str},
    )
    async def write_memory(args: dict) -> dict:
        rel = (args.get("path") or "").strip().strip("/")
        content = args.get("content")
        if not rel:
            return _err("path required")
        if not isinstance(content, str):
            return _err("content must be a string")
        if ".." in Path(rel).parts:
            return _err("path cannot contain ..")
        dest = memory_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        return _ok(f"memory written: {dest.relative_to(cfg.mind_dir)} ({len(content)} chars)")

    return [read_memory, write_memory]


def _first_nonempty(path: Path, cap: int = 120) -> str:
    try:
        for line in path.read_text().splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:cap]
    except OSError:
        pass
    return "(empty)"


def _truncate(body: str, cap: int, path: Path) -> str:
    if len(body) <= cap:
        return body
    return body[:cap] + f"\n\n…[truncated at {cap}; file is {len(body)} chars; read {path} directly for full]"


__all__ = ["build"]
