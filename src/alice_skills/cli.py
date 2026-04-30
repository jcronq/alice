"""``alice-skills`` CLI — inventory + validation over the registry.

Plan 07 Phase 2. Three subcommands:

- ``alice-skills list`` — name + scope + description per skill.
- ``alice-skills show <name>`` — full body of one skill.
- ``alice-skills validate`` — every SKILL.md parses; structural
  rules (every description has a closing punctuation, scope is
  valid) hold.

Defaults to a mind at ``$ALICE_MIND`` or ``~/alice-mind``; pass
``--mind`` to point elsewhere. The CLI lives in :mod:`alice_core`-
adjacent territory but is shipped from :mod:`alice_skills` because
the work is registry-side.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Optional, Sequence

from .registry import SkillRegistry
from .skill import Skill, SkillError


def _default_mind_path() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("ALICE_MIND") or pathlib.Path.home() / "alice-mind"
    )


def _build_registry(mind: pathlib.Path) -> SkillRegistry:
    return SkillRegistry.from_mind(mind, include_defaults=True)


def _list(args: argparse.Namespace) -> int:
    registry = _build_registry(pathlib.Path(args.mind))
    skills = registry.all()
    if not skills:
        print("(no skills found)")
        return 0
    width = max(len(s.name) for s in skills)
    for s in skills:
        line = (
            f"{s.name:<{width}}  scope={s.scope:<8}  "
            f"{_summarize(s.description_template)}"
        )
        print(line)
        if args.ops and s.ops:
            for op in s.ops:
                print(
                    f"  └── {op.name:<{width - 4}}  "
                    f"{_summarize(op.description_template)}"
                )
    return 0


def _show(args: argparse.Namespace) -> int:
    registry = _build_registry(pathlib.Path(args.mind))
    skill = registry.find(args.name)
    if skill is None:
        print(f"alice-skills: no skill named {args.name!r}", file=sys.stderr)
        return 2
    print(f"# {skill.name}")
    print(f"# scope: {skill.scope}")
    print(f"# source: {skill.source_path}")
    print()
    print(skill.description_template)
    print()
    print("---")
    print(skill.body.strip())
    if skill.ops:
        print()
        print("## ops")
        for op in skill.ops:
            print(f"  - {op.name}: {_summarize(op.description_template)}")
    return 0


def _validate(args: argparse.Namespace) -> int:
    """Reload the registry surfaces every parse error; we then run
    a few structural rules over the skills it accepted."""
    mind = pathlib.Path(args.mind)
    rc = 0
    try:
        registry = _build_registry(mind)
    except SkillError as exc:
        print(f"alice-skills: {exc}", file=sys.stderr)
        return 1
    for skill in registry.all():
        for problem in _structural_problems(skill):
            print(f"{skill.source_path}: {problem}", file=sys.stderr)
            rc = 1
    if rc == 0:
        print(f"alice-skills: validated {len(registry.all())} skill(s) OK")
    return rc


def _structural_problems(skill: Skill) -> list[str]:
    """Soft lint over a parsed skill. Mostly catches descriptions
    that won't render well to the LLM (truncated mid-sentence) or
    Jinja markers that never resolve to a real personae field."""
    out: list[str] = []
    desc = skill.description_template.strip()
    if not desc.endswith((".", "!", "?")):
        out.append("description should end with a closing punctuation mark")
    # Find unclosed Jinja {{ ... — bare ``{{`` outside of an actual
    # template expression is almost certainly a mistake.
    if desc.count("{{") != desc.count("}}"):
        out.append("description has unbalanced Jinja braces")
    return out


def _summarize(text: str, *, cap: int = 100) -> str:
    """Single-line, capped preview of a description."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= cap else line[: cap - 1] + "…"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alice-skills",
        description="Inventory + validate skills the runtime sees.",
    )
    parser.add_argument(
        "--mind",
        default=str(_default_mind_path()),
        help="alice-mind path (default: $ALICE_MIND or ~/alice-mind)",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)
    sub_list = sub.add_parser("list", help="List every skill, with scope")
    sub_list.add_argument(
        "--ops",
        action="store_true",
        help="Also list nested ops under composite skills (e.g. cortex-memory)",
    )
    sub_show = sub.add_parser("show", help="Print the body of one skill")
    sub_show.add_argument("name", help="skill name (directory name on disk)")
    sub.add_parser("validate", help="Run structural checks across every SKILL.md")

    args = parser.parse_args(argv)
    if args.cmd == "list":
        return _list(args)
    if args.cmd == "show":
        return _show(args)
    if args.cmd == "validate":
        return _validate(args)
    parser.error("unreachable")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
