"""Tests for ``alice_thinking.cli.perissue`` — per-issue entrypoint script.

Sub-issue 3 of the SM v2 pipeline revision (#163). Pin:

- Reads ``<spawn-dir>/prompt.txt`` and resolves Phase from frontmatter.
- ``--mode`` CLI flag overrides the frontmatter (BUILD-phase reuse).
- Dispatches into :class:`PhaseRunner.run` with the body as
  ``injected_content``.
- Malformed prompt.txt (missing file, missing frontmatter, unknown
  phase, empty body) logs to stderr and exits non-zero.
- ``--dry-run`` skips the kernel invocation — used by tests to keep
  the suite hermetic.
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from alice_thinking.cli.perissue import (
    PHASE_BY_NAME,
    PerIssueDispatchError,
    main,
    parse_frontmatter,
    resolve_phase,
)
from alice_thinking.phase import Phase
from alice_thinking.runtime import PhaseRunner


# ---------------------------------------------------------------------------
# parse_frontmatter / resolve_phase units
# ---------------------------------------------------------------------------


def test_parse_frontmatter_splits_yaml_block() -> None:
    text = (
        "---\n"
        "phase: per_issue_design\n"
        "issue: 163\n"
        "---\n"
        "Issue body goes here.\n"
    )
    fm, body = parse_frontmatter(text)
    assert fm == {"phase": "per_issue_design", "issue": "163"}
    assert body.strip() == "Issue body goes here."


def test_parse_frontmatter_strips_quotes() -> None:
    """Tolerate `phase: "per_issue_design"` and `phase: 'per_issue_design'`
    — dispatcher renderers vary on YAML quoting."""
    text = '---\nphase: "per_issue_design"\n---\nbody'
    fm, _ = parse_frontmatter(text)
    assert fm["phase"] == "per_issue_design"


def test_parse_frontmatter_returns_empty_when_absent() -> None:
    text = "Just a body, no frontmatter.\n"
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_resolve_phase_from_frontmatter() -> None:
    text = "---\nphase: per_issue_build\n---\nbody"
    assert resolve_phase(text) is Phase.PER_ISSUE_BUILD


def test_resolve_phase_override_wins() -> None:
    """The --mode CLI flag short-circuits frontmatter — sub-issue 4's
    compaction step recomposes the prompt for BUILD without
    necessarily rewriting frontmatter."""
    text = "---\nphase: per_issue_design\n---\nbody"
    assert (
        resolve_phase(text, override="per_issue_build")
        is Phase.PER_ISSUE_BUILD
    )


def test_resolve_phase_raises_on_missing() -> None:
    with pytest.raises(PerIssueDispatchError) as exc:
        resolve_phase("no frontmatter and no override\n")
    assert "phase:" in str(exc.value)


def test_resolve_phase_raises_on_unknown_phase() -> None:
    text = "---\nphase: sleep_b\n---\nbody"
    with pytest.raises(PerIssueDispatchError) as exc:
        resolve_phase(text)
    # Sleep is a real phase but not a per-issue one — script-level
    # gate keeps the surface narrow.
    assert "sleep_b" in str(exc.value)


def test_resolve_phase_raises_on_unknown_override() -> None:
    with pytest.raises(PerIssueDispatchError):
        resolve_phase("body\n", override="bogus_phase")


def test_phase_by_name_covers_both_per_issue_phases() -> None:
    """Lock the public mapping — adding a new per-issue phase must
    extend ``PHASE_BY_NAME`` here, otherwise the entrypoint silently
    refuses to dispatch into it."""
    assert PHASE_BY_NAME == {
        "per_issue_design": Phase.PER_ISSUE_DESIGN,
        "per_issue_build": Phase.PER_ISSUE_BUILD,
    }


# ---------------------------------------------------------------------------
# main() — end-to-end dispatch with the kernel runner mocked out
# ---------------------------------------------------------------------------


def _write_prompt(spawn_dir: pathlib.Path, phase: str, body: str) -> None:
    spawn_dir.mkdir(parents=True, exist_ok=True)
    (spawn_dir / "prompt.txt").write_text(
        f"---\nphase: {phase}\n---\n{body}\n"
    )


class _FakeRunner:
    """Capture runner.run() args so we can assert dispatch."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        # Use a real PhaseRunner under the hood so the prompt-text +
        # spec shape matches production — assertions can poke at
        # either side.
        self._inner = PhaseRunner()

    def run(self, phase, ctx, *, injected_content=None, mcp_servers=None):
        self.calls.append(
            {
                "phase": phase,
                "ctx": ctx,
                "injected_content": injected_content,
            }
        )
        return self._inner.run(
            phase, ctx, injected_content=injected_content, mcp_servers=mcp_servers
        )


def _kernel_runner_recorder() -> tuple[list[dict[str, Any]], Any]:
    """Build a (sink, callable) pair the script can use as kernel_runner."""
    sink: list[dict[str, Any]] = []

    def _runner(prompt_text, spec, *, log):
        sink.append({"prompt_text": prompt_text, "spec": spec})
        return 0

    return sink, _runner


def test_main_dispatches_design_phase(tmp_path) -> None:
    """Happy path — DESIGN-mode prompt.txt routes through PhaseRunner
    with Phase.PER_ISSUE_DESIGN and the body as injected_content.
    """
    spawn = tmp_path / "spawn-163-1"
    _write_prompt(spawn, "per_issue_design", "Implement Phase.PER_ISSUE_*.")
    fake_runner = _FakeRunner()
    kernel_calls, kernel_runner = _kernel_runner_recorder()
    logs: list[str] = []

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--session-id",
            "session-uuid",
            "--mind",
            str(tmp_path / "mind"),
            "--cwd",
            str(tmp_path / "repo"),
        ],
        runner_factory=lambda: fake_runner,
        kernel_runner=kernel_runner,
        log=logs.append,
    )

    assert rc == 0
    assert len(fake_runner.calls) == 1
    call = fake_runner.calls[0]
    assert call["phase"] is Phase.PER_ISSUE_DESIGN
    assert "Implement Phase.PER_ISSUE_*." in call["injected_content"]
    # Kernel was invoked with the composed prompt.
    assert len(kernel_calls) == 1
    assert "Implement Phase.PER_ISSUE_*." in kernel_calls[0]["prompt_text"]
    # Resolution log appears for the operator's stderr scrub.
    assert any("phase=per_issue_design" in line for line in logs)


def test_main_dispatches_build_phase_via_mode_override(tmp_path) -> None:
    """``--mode per_issue_build`` overrides the frontmatter — the
    compaction step recomposes prompt.txt with the approved design
    note as body; the --mode flag flips the dispatch tier."""
    spawn = tmp_path / "spawn-163-2"
    # Note: frontmatter still says design (it's what DESIGN wrote);
    # the --mode override flips it.
    _write_prompt(
        spawn,
        "per_issue_design",
        "## Approved design\n\nImplement the new phases.",
    )
    fake_runner = _FakeRunner()
    _, kernel_runner = _kernel_runner_recorder()

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--session-id",
            "uuid",
            "--mode",
            "per_issue_build",
            "--mind",
            str(tmp_path / "mind"),
            "--cwd",
            str(tmp_path / "repo"),
        ],
        runner_factory=lambda: fake_runner,
        kernel_runner=kernel_runner,
        log=lambda msg: None,
    )

    assert rc == 0
    assert fake_runner.calls[0]["phase"] is Phase.PER_ISSUE_BUILD


def test_main_missing_prompt_txt_returns_1(tmp_path) -> None:
    """Spawn dir without prompt.txt is an error from the operator's
    POV — dispatcher pre-writes it before exec, so a missing file
    means something went wrong in the spawn pipeline."""
    spawn = tmp_path / "spawn-empty"
    spawn.mkdir()
    logs: list[str] = []

    rc = main(
        ["--spawn-dir", str(spawn)],
        kernel_runner=lambda *a, **kw: 0,
        log=logs.append,
    )

    assert rc == 1
    assert any("missing prompt.txt" in line for line in logs)


def test_main_missing_frontmatter_returns_1(tmp_path) -> None:
    """prompt.txt without a phase: frontmatter + no --mode override
    is malformed for this entrypoint. Don't dispatch into a phase
    we can't identify."""
    spawn = tmp_path / "spawn-noframe"
    spawn.mkdir()
    (spawn / "prompt.txt").write_text("Just an issue body, no header.\n")
    logs: list[str] = []

    rc = main(
        ["--spawn-dir", str(spawn)],
        kernel_runner=lambda *a, **kw: 0,
        log=logs.append,
    )

    assert rc == 1
    assert any("no `phase:` frontmatter" in line for line in logs)


def test_main_unknown_phase_returns_1(tmp_path) -> None:
    """Frontmatter that names a non-per-issue phase (or a typo) is
    rejected — the script's gate is narrower than the full Phase
    enum on purpose."""
    spawn = tmp_path / "spawn-bad-phase"
    spawn.mkdir()
    (spawn / "prompt.txt").write_text(
        "---\nphase: sleep_d\n---\nbody\n"
    )
    logs: list[str] = []

    rc = main(
        ["--spawn-dir", str(spawn)],
        kernel_runner=lambda *a, **kw: 0,
        log=logs.append,
    )

    assert rc == 1
    assert any("sleep_d" in line for line in logs)


def test_main_empty_body_returns_1(tmp_path) -> None:
    """A prompt.txt that's only frontmatter has nothing to inject —
    refuse rather than fire a degenerate kernel call."""
    spawn = tmp_path / "spawn-empty-body"
    spawn.mkdir()
    (spawn / "prompt.txt").write_text(
        "---\nphase: per_issue_design\n---\n\n"
    )
    logs: list[str] = []

    rc = main(
        ["--spawn-dir", str(spawn)],
        kernel_runner=lambda *a, **kw: 0,
        log=logs.append,
    )

    assert rc == 1
    assert any("no body" in line for line in logs)


def test_main_dry_run_skips_kernel(tmp_path) -> None:
    """``--dry-run`` resolves phase + composes the prompt but skips
    kernel invocation. Used by integration tests + a probe before
    full launch."""
    spawn = tmp_path / "spawn-dry"
    _write_prompt(spawn, "per_issue_design", "body")
    kernel_called = MagicMock()

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--dry-run",
            "--mind",
            str(tmp_path / "mind"),
            "--cwd",
            str(tmp_path / "repo"),
        ],
        kernel_runner=kernel_called,
        log=lambda msg: None,
    )

    assert rc == 0
    kernel_called.assert_not_called()


def test_main_propagates_kernel_exit_code(tmp_path) -> None:
    """Kernel timeout (124) / failure (1) bubbles back to the caller
    so the dispatcher's pidfile-reap path sees the right exit code."""
    spawn = tmp_path / "spawn-rc"
    _write_prompt(spawn, "per_issue_design", "body")

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--mind",
            str(tmp_path / "mind"),
            "--cwd",
            str(tmp_path / "repo"),
        ],
        runner_factory=PhaseRunner,
        kernel_runner=lambda prompt_text, spec, *, log: 124,
        log=lambda msg: None,
    )
    assert rc == 124
