"""Per-issue thinking-agent entrypoint shim — placeholder for sub-issue 3.

The SM v2 pipeline (per ``cortex-memory/research/2026-05-13-sm-v2-pipeline-revision.md``
§3 Q1) spawns a long-lived thinking-agent container for each
``(sm:selected, art:code|art:experiment)`` issue. The dispatcher
(:func:`sm.dispatcher.spawn_thinking_agent`) writes the prompt to
``<spawn_dir>/prompt.txt`` and launches this module via
``python -m sm.thinking_shim --spawn-dir <dir> --session-id <uuid>``.

Sub-issue 3 will replace this stub with the real entrypoint: read
``prompt.txt`` and dispatch into :class:`alice_thinking.runtime.PhaseRunner`
with ``Phase.PER_ISSUE_DESIGN`` / ``Phase.PER_ISSUE_BUILD``.

For now the shim exits cleanly so the dispatcher path can be exercised
end-to-end (spawn dir layout, audit comment, pidfile, reap) without
pulling in the thinking runtime.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Iterable


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-issue thinking-agent entrypoint (placeholder — see #156)."
    )
    parser.add_argument(
        "--spawn-dir",
        required=True,
        help="path to the per-issue spawn dir containing prompt.txt",
    )
    parser.add_argument(
        "--session-id",
        required=True,
        help="claude-agent-sdk session id pre-minted by the dispatcher",
    )
    parser.add_argument(
        "--mode",
        default="design",
        choices=("design", "build"),
        help="per-issue phase the agent is entering (sub-issue 3 will consume)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    spawn_dir = pathlib.Path(args.spawn_dir)
    prompt_path = spawn_dir / "prompt.txt"
    if not prompt_path.is_file():
        print(
            f"[sm-thinking-shim] no prompt.txt at {prompt_path} — exiting",
            file=sys.stderr,
        )
        return 1
    print(
        f"[sm-thinking-shim] placeholder for spawn_dir={spawn_dir} "
        f"session_id={args.session_id} mode={args.mode} — exiting cleanly "
        f"(real entrypoint lands in sub-issue 3 of #149)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
