"""Memory tools — READ-ONLY for speaking Alice.

Speaking does not write memory directly. When she learns something durable,
she writes a note to `inner/notes/` via the `append_note` tool and thinking
Alice processes the note into memory on her next wake. This includes dated
daily logs, meal logs, workout logs, feedback, and all structured facts.

This module exposes ``read_memory`` that glob-searches across both
``memory/`` (legacy stream) and ``cortex-memory/`` (groomed wiki) and bumps
``last_accessed`` + ``access_count`` frontmatter fields whenever it returns
a single file — so thinking Alice has signal about which notes are actively
load-bearing vs drifting toward irrelevance.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from alice_core.config.personae import Personae, placeholder as placeholder_personae

from ..infra.config import Config


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {text}"}], "isError": True}


_ROOTS = ("cortex-memory", "memory")


def build(
    cfg: Config, *, personae: Personae | None = None
) -> list[SdkMcpTool[Any]]:
    p = personae or placeholder_personae()
    agent = p.agent.name
    mind_dir = cfg.mind_dir
    PREVIEW_CAP = 4000

    @tool(
        name="read_memory",
        description=(
            f"Read {agent}'s memory. Pattern is glob-style, searched across both "
            f"`cortex-memory/` (groomed wiki) and `memory/` (legacy stream). "
            f"Patterns can be plain filenames, dated entries like "
            f"'2026-04-24.md', folder-scoped ('people/*'), or globs across "
            f"subtrees ('*/<keyword>*'). Single-match returns verbatim "
            f"content and bumps last_accessed + access_count. Multi-match "
            f"returns a listing of first lines."
        ),
        input_schema={"pattern": str},
    )
    async def read_memory(args: dict) -> dict:
        pattern = (args.get("pattern") or "").strip()
        if not pattern:
            return _err("pattern required")

        matches: list[Path] = []
        for root_name in _ROOTS:
            root = mind_dir / root_name
            if not root.is_dir():
                continue
            matches.extend(sorted(root.glob(pattern)))
            # Also allow the pattern to be prefixed with the root name itself.
            if pattern.startswith(f"{root_name}/"):
                matches.extend(sorted(mind_dir.glob(pattern)))

        # Dedup while preserving order.
        seen: set[str] = set()
        unique: list[Path] = []
        for p in matches:
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            unique.append(p)
        matches = unique

        if not matches:
            return _ok(f"(no match for {pattern} under cortex-memory/ or memory/)")

        if len(matches) == 1 and matches[0].is_file():
            body = matches[0].read_text()
            _bump_access(matches[0])
            return _ok(_truncate(body, PREVIEW_CAP, matches[0]))

        lines: list[str] = []
        for p in matches[:40]:
            if p.is_dir():
                lines.append(f"{p.relative_to(mind_dir)}/  (dir)")
            else:
                lines.append(f"{p.relative_to(mind_dir)}: {_first_nonempty(p)}")
        more = "" if len(matches) <= 40 else f"\n…and {len(matches) - 40} more"
        return _ok("\n".join(lines) + more)

    return [read_memory]


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


_FRONTMATTER_RE = re.compile(r"^(---\n)(.*?)(\n---\n)", re.DOTALL)


def _bump_access(path: Path) -> None:
    """Best-effort update of last_accessed + access_count in frontmatter.

    No-op if:
    - The file has no YAML frontmatter (e.g., legacy memory files).
    - We can't write back (permissions). Reading is not meant to fail.
    """
    try:
        text = path.read_text()
    except OSError:
        return
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return
    body_fm = m.group(2)
    today = datetime.date.today().isoformat()
    new_fm = _update_fm_field(body_fm, "last_accessed", today)
    # Increment access_count.
    cur_count = 0
    for line in new_fm.splitlines():
        if line.strip().startswith("access_count:"):
            try:
                cur_count = int(line.split(":", 1)[1].strip())
            except ValueError:
                cur_count = 0
            break
    new_fm = _update_fm_field(new_fm, "access_count", str(cur_count + 1))
    new_text = m.group(1) + new_fm + m.group(3) + text[m.end():]
    try:
        path.write_text(new_text)
    except OSError:
        pass


def _update_fm_field(fm: str, key: str, value: str) -> str:
    lines = fm.splitlines()
    new_line = f"{key}: {value}"
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}:"):
            lines[i] = new_line
            return "\n".join(lines)
    # Not present — append.
    lines.append(new_line)
    return "\n".join(lines)


__all__ = ["build"]
