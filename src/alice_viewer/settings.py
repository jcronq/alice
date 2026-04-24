"""Viewer runtime paths. Defaults match Alice's host bind-mounts."""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Paths:
    thinking_log: pathlib.Path
    speaking_log: pathlib.Path
    turn_log: pathlib.Path
    mind_dir: pathlib.Path
    state_dir: pathlib.Path

    @property
    def inner(self) -> pathlib.Path:
        return self.mind_dir / "inner"

    @property
    def memory(self) -> pathlib.Path:
        return self.mind_dir / "memory"


def load() -> Paths:
    home = pathlib.Path.home()
    state = pathlib.Path(os.environ.get("ALICE_STATE", str(home / ".local/state/alice/worker")))
    mind = pathlib.Path(os.environ.get("ALICE_MIND", str(home / "alice-mind")))
    return Paths(
        thinking_log=pathlib.Path(
            os.environ.get("ALICE_THINKING_LOG", str(state / "thinking.log"))
        ),
        speaking_log=pathlib.Path(
            os.environ.get("ALICE_SPEAKING_LOG", str(state / "speaking.log"))
        ),
        turn_log=pathlib.Path(
            os.environ.get("ALICE_TURN_LOG", str(mind / "inner/state/speaking-turns.jsonl"))
        ),
        mind_dir=mind,
        state_dir=state,
    )
