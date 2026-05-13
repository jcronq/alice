"""Per-issue speaking-agent entrypoint shim — placeholder for the build phase.

The SM v2 pipeline (per the post-amendment to
``[[2026-05-13-sm-v2-pipeline-revision]]``, Jason 2026-05-13 09:51 EDT)
spawns a stimulus-style speaking-instance for each ``(sm:designed,
art:code)`` issue to own the build phase. The dispatcher
(:func:`alice_sm.dispatcher.spawn_speaking_agent`) writes the prompt to
``<spawn_dir>/prompt.txt`` and launches this module via
``python -m alice_sm.speaking_shim --spawn-dir <dir> --session-id <uuid> --mode build``.

The follow-up sub-issue replaces this stub with the real entrypoint:
read ``prompt.txt``, resolve :class:`alice_thinking.phase.Phase.PER_ISSUE_BUILD`
from the frontmatter, load the approved design note pointed to by the
frontmatter, and dispatch the Task / Agent tool with the design +
relevant context, instructing the sub-agent to implement and open a
draft PR. The shim then posts ``[SM] build-complete pr=<url>`` (or an
error variant) and exits.

For now the shim exits cleanly so the dispatcher path can be exercised
end-to-end (spawn dir layout, audit comment, pidfile, reap) without
pulling in the speaking runtime — mirrors :mod:`alice_sm.thinking_shim`
which played the same role for #156 before the real entrypoint landed
at :mod:`alice_thinking.cli.perissue`.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Iterable


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Per-issue speaking-agent entrypoint (placeholder — see #184)."
        )
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
        default="build",
        choices=("build",),
        help="per-issue phase the agent is entering (only build is valid here)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    spawn_dir = pathlib.Path(args.spawn_dir)
    prompt_path = spawn_dir / "prompt.txt"
    if not prompt_path.is_file():
        print(
            f"[sm-speaking-shim] no prompt.txt at {prompt_path} — exiting",
            file=sys.stderr,
        )
        return 1
    print(
        f"[sm-speaking-shim] placeholder for spawn_dir={spawn_dir} "
        f"session_id={args.session_id} mode={args.mode} — exiting cleanly "
        f"(real entrypoint lands in the speaking PhaseRunner sub-issue)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
