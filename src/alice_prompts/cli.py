"""Inventory + render + validate CLI for the prompts package.

Plan 04 Phase 8 — useful for plan 05/07 review (and for anyone
who wants to see what a prompt actually looks like with realistic
context). Three subcommands:

- ``list`` — print every known prompt name in dotted form, one
  per line. Exits 0 unless the loader can't reach its defaults
  directory.
- ``render <name> [--context-file PATH]`` — render the named
  template with context loaded from a YAML or JSON file. Without
  ``--context-file`` the package's persona-placeholder defaults
  apply, which is enough for many templates.
- ``validate`` — every template parses (Jinja syntax check). Exits
  non-zero if any template has a syntax error.

Invoke as ``python -m alice_prompts.cli <subcommand> ...`` or via
the ``bin/alice-prompts`` shell wrapper.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import jinja2

from . import default_loader, list_prompts


def _load_context_file(path: pathlib.Path) -> dict[str, Any]:
    """Load a render context from YAML or JSON. Decides by extension."""
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise SystemExit(
            f"context file {path} must contain a top-level mapping; got "
            f"{type(data).__name__}"
        )
    return data


def cmd_list(_args: argparse.Namespace) -> int:
    for name in list_prompts():
        print(name)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    context: dict[str, Any] = {}
    if args.context_file:
        context = _load_context_file(pathlib.Path(args.context_file))
    loader = default_loader()
    print(loader.load(args.name, **context))
    return 0


def cmd_validate(_args: argparse.Namespace) -> int:
    """Parse every template through Jinja's environment. Returns
    non-zero on the first syntax error and prints the offender;
    silent + exit 0 when every template is well-formed."""
    loader = default_loader()
    # Direct access to the env so we get TemplateSyntaxError rather
    # than the loader's PromptNotFound.
    env: jinja2.Environment = loader._env  # noqa: SLF001
    failures: list[tuple[str, str]] = []
    for name in list_prompts():
        try:
            # _name_to_path lives in loader.py; re-use via the
            # private interface — we're in-package, this is fine.
            from .loader import _name_to_path

            env.get_template(_name_to_path(name))
        except jinja2.TemplateSyntaxError as exc:
            failures.append((name, f"line {exc.lineno}: {exc.message}"))
        except jinja2.TemplateError as exc:
            failures.append((name, str(exc)))

    if failures:
        for name, msg in failures:
            print(f"FAIL {name}: {msg}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alice-prompts",
        description="Inventory and inspection for alice_prompts templates.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="print every known prompt name")

    p_render = sub.add_parser(
        "render",
        help="render a template with optional context",
    )
    p_render.add_argument("name", help="dotted prompt name (e.g. speaking.compact)")
    p_render.add_argument(
        "--context-file",
        default=None,
        help="YAML or JSON file with the render context",
    )

    sub.add_parser(
        "validate",
        help="parse every template (Jinja syntax check)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "list": cmd_list,
        "render": cmd_render,
        "validate": cmd_validate,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
