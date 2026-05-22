"""Golden-file tests for :func:`compose_spawn_prompt`.

Phase 4 of #194 (#321) made the v1 worker-pool prompt body a function
of the registered :class:`core.agent_library.AgentSpec`'s
:meth:`assembled_system_prompt`. The previous Phase 3 assertions
covered "the row carries the right ``agent_spec`` name" but not
"editing the spec's behavioral rules will silently change every
worker's prompt body."

These goldens pin the fully-rendered prompt for each v1 worker-pool
SPAWN_MAP row. They serve two purposes:

1. **Drift detection.** A change to ``_CONFIG_WORKER_RULES`` or
   ``_RESEARCH_WRITER_RULES`` in
   :mod:`core.agent_library.agents` updates the worker prompt; the
   matching golden must be regenerated in the same PR. The diff in
   review surfaces the prompt change for human inspection.
2. **Operator-readable documentation.** Anyone reading
   ``tests/fixtures/spawn_prompts/`` sees the exact text the live
   workers receive at spawn time.

The reviewer row (``(sm:reviewing, art:code)``) and the SDK lanes
(``(sm:selected, art:code)``, ``(sm:designed, art:code)``) do not go
through :func:`compose_spawn_prompt` so they are not covered here —
the reviewer dispatches via :func:`core.agent_library.run_agent` and
the SDK lanes use their own composers (:func:`compose_thinking_spawn_prompt`
/ :func:`compose_speaking_spawn_prompt`).

Regenerating goldens
====================

When a behavioral-rule edit intentionally changes the worker prompt::

    cd ~/alice && uv run python -m tests.regen_spawn_prompts

That entrypoint doesn't exist yet — until it does, regenerate with
this one-liner (which the golden fixtures were originally produced
with)::

    uv run python -c "$(cat <<'PY'
    from pathlib import Path
    from alice_forge.dispatcher import constants as sm
    from alice_forge.dispatcher.spawn import compose_spawn_prompt
    from tests.test_spawn_prompts_golden import (
        CANONICAL_ISSUE,
        WORKER_ROWS,
        FIXTURE_DIR,
    )
    for row_key, fname in WORKER_ROWS:
        row = sm.SPAWN_MAP[row_key]
        issue = dict(CANONICAL_ISSUE, labels=[{"name": row_key[1]}])
        Path(FIXTURE_DIR / fname).write_text(
            compose_spawn_prompt(issue, row)
        )
    PY
    )"
"""

from __future__ import annotations

import pathlib

import pytest

from alice_forge.dispatcher import constants as sm_constants
from alice_forge.dispatcher.spawn import compose_spawn_prompt


# Canonical issue payload used for every golden — matches the
# ``gh issue view --json`` shape (``author`` rather than ``user``)
# so :func:`alice_forge.dispatcher.trust._author_login` resolves the
# login and the ``Source: source:<login>`` line renders identically
# to a live run.
CANONICAL_ISSUE: dict = {
    "number": 42,
    "title": "Example task title",
    "body": "Example issue body that the worker reads.",
    "labels": [{"name": "art:placeholder"}],  # overridden per-row
    "author": {"login": "jcronq"},
}


# Only persona=="worker" SPAWN_MAP rows are covered — those are the
# rows whose prompt body :func:`compose_spawn_prompt` actually
# renders. The reviewer / thinking / speaking lanes use other
# composers (see module docstring).
WORKER_ROWS: list[tuple[tuple[str, str], str]] = [
    (("sm:selected", "art:config_change"), "sm_selected__art_config_change.txt"),
    (("sm:selected", "art:research_note"), "sm_selected__art_research_note.txt"),
    (("sm:selected", "art:experiment"), "sm_selected__art_experiment.txt"),
]


FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "spawn_prompts"


@pytest.mark.parametrize("row_key,fixture_name", WORKER_ROWS)
def test_compose_spawn_prompt_matches_golden(
    row_key: tuple[str, str], fixture_name: str
) -> None:
    """The rendered prompt for each v1 worker-pool row matches the
    pinned golden fixture. A failure here means either the SPAWN_MAP
    row changed, the registered AgentSpec's behavioral rules
    changed, or :func:`compose_spawn_prompt`'s framing logic
    changed — any of which should be reviewed alongside a regenerated
    fixture."""
    row = sm_constants.SPAWN_MAP[row_key]
    issue = dict(CANONICAL_ISSUE, labels=[{"name": row_key[1]}])
    actual = compose_spawn_prompt(issue, row)
    expected = (FIXTURE_DIR / fixture_name).read_text()
    assert actual == expected, (
        f"Rendered spawn prompt for {row_key!r} does not match "
        f"golden {fixture_name!r}. If the change is intentional, "
        f"regenerate the fixture (see module docstring)."
    )


def test_every_worker_row_has_a_golden() -> None:
    """A future SPAWN_MAP edit that adds a new persona=='worker' row
    must also add a matching golden fixture. Catches the case where
    a contributor adds a row but forgets to pin its rendered prompt."""
    worker_keys = {
        key
        for key, row in sm_constants.SPAWN_MAP.items()
        if row.get("persona") == "worker"
    }
    covered_keys = {key for key, _ in WORKER_ROWS}
    missing = worker_keys - covered_keys
    assert not missing, (
        f"SPAWN_MAP rows with persona='worker' lack golden coverage: "
        f"{sorted(missing)!r}. Add an entry to WORKER_ROWS and "
        f"create the fixture under tests/fixtures/spawn_prompts/."
    )
