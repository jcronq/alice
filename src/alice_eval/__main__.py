"""CLI entry point — ``python -m alice_eval <subcommand>``.

Subcommands:

- ``sample``  — :mod:`alice_eval.sampling`
- ``replay``  — :mod:`alice_eval.replay`
- ``ui``      — :mod:`alice_eval.rating_ui`

Each subcommand re-uses the subcommand module's own ``main(argv)``
so behaviour matches running the module directly with
``python -m alice_eval.sampling``.
"""

from __future__ import annotations

import argparse
import sys

from alice_eval import rating_ui, replay, sampling


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="python -m alice_eval",
        description="Speaking-quality eval — sample, replay, rating UI.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sample", help="Stratified sample extraction", add_help=False)
    sub.add_parser("replay", help="Run candidates over a sample", add_help=False)
    sub.add_parser("ui", help="Generate the rating UI HTML", add_help=False)

    if not argv:
        parser.print_help()
        return 2

    cmd, *rest = argv
    if cmd == "sample":
        return sampling.main(rest)
    if cmd == "replay":
        return replay.main(rest)
    if cmd == "ui":
        return rating_ui.main(rest)

    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
