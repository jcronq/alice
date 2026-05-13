"""Tests for ``alice_speaking.runtime`` + ``alice_speaking.cli.perissue``.

Sub-issue 5 of the SM v2 pipeline revision (#185). Pin:

- ``PhaseRunner`` composes a prompt that includes the issue body, the
  approved design note, and the SWE practices baseline, and a
  ``KernelSpec`` carrying the full code-worker tool allowance.
- The CLI entrypoint reads ``prompt.txt``, loads the design note
  pointed to by ``design_note:`` frontmatter, drives the kernel,
  parses the PR URL from the sub-agent's output, and posts
  ``[SM] build-complete pr=<url>``.
- Sub-agent failures (no PR URL, kernel error) post
  ``[SM] build-failed reason=...`` and exit non-zero.
- Malformed prompt.txt (missing ``design_note`` frontmatter, missing
  ``issue`` number, no body) exits non-zero with a log line and
  posts no audit comment.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from alice_speaking.cli.perissue import (
    PHASE_BY_NAME,
    PerIssueDispatchError,
    main,
    parse_frontmatter,
    resolve_phase,
)
from alice_speaking.runtime import (
    BUILD_AGENT_TOOL_ALLOWLIST,
    BuildPromptInputs,
    Phase,
    PhaseRunner,
    SWE_BASELINE_PRACTICES,
    compose_build_prompt,
    parse_pr_url,
    render_build_complete_comment,
    render_build_failed_comment,
)


# ---------------------------------------------------------------------------
# Phase enum / parsing helpers
# ---------------------------------------------------------------------------


def test_phase_enum_includes_per_issue_build() -> None:
    """Lock the public enum surface — adding a new phase should
    extend PHASE_BY_NAME so the entrypoint stays narrow."""
    assert Phase.PER_ISSUE_BUILD.value == "per_issue_build"
    assert PHASE_BY_NAME == {"per_issue_build": Phase.PER_ISSUE_BUILD}


def test_parse_pr_url_finds_canonical_pr_link() -> None:
    text = (
        "Sub-agent done. PR opened at "
        "https://github.com/jcronq/alice/pull/200 — please review."
    )
    assert parse_pr_url(text) == "https://github.com/jcronq/alice/pull/200"


def test_parse_pr_url_returns_none_when_absent() -> None:
    assert parse_pr_url("no PR opened; build failed") is None


def test_parse_pr_url_returns_first_match() -> None:
    """Sub-agents sometimes reference multiple PRs (the new one + a
    parent). Return the first hit — the dispatcher records that as
    the build's deliverable."""
    text = (
        "Stacking on https://github.com/jcronq/alice/pull/100; "
        "opened https://github.com/jcronq/alice/pull/201"
    )
    assert parse_pr_url(text) == "https://github.com/jcronq/alice/pull/100"


def test_render_build_complete_uses_canonical_prefix() -> None:
    body = render_build_complete_comment("https://github.com/jcronq/alice/pull/200")
    assert body == "[SM] build-complete pr=https://github.com/jcronq/alice/pull/200"


def test_render_build_failed_collapses_whitespace_and_caps_length() -> None:
    raw = "kernel raised RuntimeError:\n  oops\n  traceback follows\n  " + "x" * 800
    body = render_build_failed_comment(raw)
    assert body.startswith('[SM] build-failed reason="')
    assert body.endswith('"')
    # Total payload should be bounded — the reason inside the quotes
    # capped at 400 chars; the prefix + quotes adds a deterministic 27
    # chars on top.
    assert len(body) <= 27 + 400 + 1


# ---------------------------------------------------------------------------
# parse_frontmatter / resolve_phase
# ---------------------------------------------------------------------------


def test_parse_frontmatter_strips_issue_hash_prefix() -> None:
    """``issue: #185`` is the dispatcher's natural rendering; the
    parser should hand back ``"185"`` so callers can ``int()`` it
    without re-stripping."""
    text = (
        "---\n"
        "phase: per_issue_build\n"
        "issue: #185\n"
        "design_note: /home/alice/alice-mind/cortex-memory/designs/note.md\n"
        "---\n"
        "Issue body goes here.\n"
    )
    fm, body = parse_frontmatter(text)
    assert fm["issue"] == "185"
    assert fm["phase"] == "per_issue_build"
    assert fm["design_note"] == "/home/alice/alice-mind/cortex-memory/designs/note.md"
    assert body.strip() == "Issue body goes here."


def test_parse_frontmatter_strips_quotes() -> None:
    text = '---\nphase: "per_issue_build"\n---\nbody'
    fm, _ = parse_frontmatter(text)
    assert fm["phase"] == "per_issue_build"


def test_parse_frontmatter_returns_empty_when_absent() -> None:
    text = "Just a body, no frontmatter.\n"
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_resolve_phase_from_frontmatter() -> None:
    text = "---\nphase: per_issue_build\n---\nbody"
    assert resolve_phase(text) is Phase.PER_ISSUE_BUILD


def test_resolve_phase_raises_on_unknown_phase() -> None:
    text = "---\nphase: per_issue_design\n---\nbody"
    with pytest.raises(PerIssueDispatchError) as exc:
        resolve_phase(text)
    # per_issue_design is a real thinking-side phase but not a
    # valid speaking-side phase; the gate is narrower on purpose.
    assert "per_issue_design" in str(exc.value)


def test_resolve_phase_override_wins() -> None:
    text = "---\nphase: per_issue_design\n---\nbody"
    assert resolve_phase(text, override="per_issue_build") is Phase.PER_ISSUE_BUILD


def test_resolve_phase_raises_on_missing() -> None:
    with pytest.raises(PerIssueDispatchError):
        resolve_phase("body, no header\n")


# ---------------------------------------------------------------------------
# compose_build_prompt / PhaseRunner
# ---------------------------------------------------------------------------


def _inputs(**kw) -> BuildPromptInputs:
    base = dict(
        issue_number=185,
        issue_body="Implement PhaseRunner.PER_ISSUE_BUILD for speaking.",
        design_note_text="## Design\n\nUse PhaseRunner shape; inline SWE baseline.",
        repo="jcronq/alice",
    )
    base.update(kw)
    return BuildPromptInputs(**base)


def test_compose_build_prompt_includes_issue_body_and_design() -> None:
    text = compose_build_prompt(_inputs())
    assert "Implement PhaseRunner.PER_ISSUE_BUILD for speaking." in text
    assert "Use PhaseRunner shape; inline SWE baseline." in text
    assert "issue #185" in text
    assert "jcronq/alice" in text
    # Final-line directive steers the sub-agent toward a parseable
    # status sentence.
    assert "PR opened at" in text


def test_compose_build_prompt_includes_swe_baseline_by_default() -> None:
    text = compose_build_prompt(_inputs())
    # Pin three of the spec's required practice bullets — the rest are
    # asserted as a unit so the test doesn't flake on minor wording
    # tweaks.
    assert "Push, don't stash." in text
    assert "No `--no-verify`." in text
    assert "Draft PRs only." in text


def test_compose_build_prompt_honors_swe_baseline_override() -> None:
    """A caller can swap the SWE baseline (e.g., for the future
    base-worker-prompt.md extraction) without rewriting the
    composer."""
    text = compose_build_prompt(_inputs(), swe_baseline="## Custom baseline")
    assert "## Custom baseline" in text
    # Default baseline must NOT appear when overridden.
    assert "Push, don't stash." not in text


def test_phase_runner_returns_prompt_and_spec(tmp_path) -> None:
    runner = PhaseRunner()
    prompt, spec = runner.run(
        Phase.PER_ISSUE_BUILD,
        _inputs(),
        model="claude-opus-test",
        cwd=tmp_path,
    )
    assert isinstance(prompt, str) and prompt.strip()
    assert "Implement PhaseRunner.PER_ISSUE_BUILD for speaking." in prompt
    assert "Use PhaseRunner shape; inline SWE baseline." in prompt
    assert spec.model == "claude-opus-test"
    assert spec.allowed_tools == list(BUILD_AGENT_TOOL_ALLOWLIST)
    assert spec.cwd == tmp_path
    # Unbounded by default — per-issue builds are long-lived.
    assert spec.max_seconds == 0


def test_phase_runner_rejects_unknown_phase() -> None:
    runner = PhaseRunner()
    with pytest.raises(ValueError):
        runner.run(
            "not a Phase",  # type: ignore[arg-type]
            _inputs(),
            model="m",
            cwd=pathlib.Path("/tmp"),
        )


def test_swe_baseline_constant_carries_required_practices() -> None:
    """Regression guard: the inlined baseline must keep covering the
    practices the issue spec calls out. When the baseline migrates to
    cortex-memory/reference/base-worker-prompt.md, the equivalent
    assertion moves to that file's loader."""
    for needle in (
        "Push, don't stash.",
        "No `--no-verify`.",
        "Draft PRs only.",
        "Tests must be written.",
        "No half-finished implementations.",
        "Closes #",
    ):
        assert needle in SWE_BASELINE_PRACTICES


# ---------------------------------------------------------------------------
# main() — end-to-end dispatch with kernel + gh mocked out
# ---------------------------------------------------------------------------


def _write_prompt(
    spawn: pathlib.Path,
    *,
    design_path: pathlib.Path,
    design_text: str = "## Approved design\n\nDo the thing.",
    issue: str = "#185",
    phase: str = "per_issue_build",
    body: str = "Implement the feature described in #185.",
) -> None:
    spawn.mkdir(parents=True, exist_ok=True)
    design_path.parent.mkdir(parents=True, exist_ok=True)
    design_path.write_text(design_text)
    (spawn / "prompt.txt").write_text(
        f"---\n"
        f"phase: {phase}\n"
        f"issue: {issue}\n"
        f"design_note: {design_path}\n"
        f"art: art:code\n"
        f"---\n"
        f"{body}\n"
    )


class _FakeRunner:
    """Capture runner.run() args so tests can assert dispatch."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._inner = PhaseRunner()

    def run(self, phase, inputs, **kw):
        self.calls.append({"phase": phase, "inputs": inputs, "kw": kw})
        return self._inner.run(phase, inputs, **kw)


def _kernel_runner_returning(rc: int, text: str):
    """Build a kernel_runner that records the (prompt_text, spec) it
    was handed and returns the configured (rc, text)."""
    sink: list[dict[str, Any]] = []

    def _runner(prompt_text, spec, *, log):
        sink.append({"prompt_text": prompt_text, "spec": spec})
        return rc, text

    return sink, _runner


def _record_comments():
    """Return ``(sink_list, recorder_callable)`` for ``post_comment``."""
    sink: list[tuple[str, int, str]] = []

    def _recorder(repo: str, number: int, body: str) -> None:
        sink.append((repo, number, body))

    return sink, _recorder


def test_main_happy_path_posts_build_complete(tmp_path) -> None:
    """Sub-agent returns ``PR opened at <url>`` — assert the
    build-complete audit comment is posted with that URL."""
    spawn = tmp_path / "spawn-185-1"
    design = tmp_path / "designs/note.md"
    _write_prompt(spawn, design_path=design)
    fake_runner = _FakeRunner()
    pr_url = "https://github.com/jcronq/alice/pull/300"
    _, kernel_runner = _kernel_runner_returning(
        0, f"Done. PR opened at {pr_url}\n"
    )
    comments, post_comment = _record_comments()
    logs: list[str] = []

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--session-id",
            "session-uuid",
            "--cwd",
            str(tmp_path / "repo"),
        ],
        runner_factory=lambda: fake_runner,
        kernel_runner=kernel_runner,
        post_comment=post_comment,
        log=logs.append,
    )

    assert rc == 0
    # Runner saw PER_ISSUE_BUILD with design + issue body wired in.
    assert len(fake_runner.calls) == 1
    inputs = fake_runner.calls[0]["inputs"]
    assert inputs.issue_number == 185
    assert "Implement the feature described in #185." in inputs.issue_body
    assert "Do the thing." in inputs.design_note_text
    # Audit comment posted with the build-complete prefix and the URL.
    assert comments == [
        ("jcronq/alice", 185, f"[SM] build-complete pr={pr_url}"),
    ]


def test_main_subagent_failure_posts_build_failed(tmp_path) -> None:
    """Sub-agent returned an error string with no PR URL — the
    entrypoint posts ``[SM] build-failed reason=...`` and exits
    non-zero."""
    spawn = tmp_path / "spawn-185-2"
    design = tmp_path / "designs/note.md"
    _write_prompt(spawn, design_path=design)
    fake_runner = _FakeRunner()
    error_text = "build failed: hooks rejected the commit"
    _, kernel_runner = _kernel_runner_returning(0, error_text)
    comments, post_comment = _record_comments()

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--cwd",
            str(tmp_path / "repo"),
        ],
        runner_factory=lambda: fake_runner,
        kernel_runner=kernel_runner,
        post_comment=post_comment,
        log=lambda msg: None,
    )

    assert rc == 1
    assert len(comments) == 1
    repo, number, body = comments[0]
    assert repo == "jcronq/alice"
    assert number == 185
    assert body.startswith("[SM] build-failed")
    assert "hooks rejected the commit" in body


def test_main_kernel_error_posts_build_failed(tmp_path) -> None:
    """Kernel-side failure (rc=1) surfaces as build-failed too."""
    spawn = tmp_path / "spawn-185-3"
    design = tmp_path / "designs/note.md"
    _write_prompt(spawn, design_path=design)
    _, kernel_runner = _kernel_runner_returning(1, "kernel crashed")
    comments, post_comment = _record_comments()

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--cwd",
            str(tmp_path / "repo"),
        ],
        kernel_runner=kernel_runner,
        post_comment=post_comment,
        log=lambda msg: None,
    )

    assert rc == 1
    assert len(comments) == 1
    _, _, body = comments[0]
    assert body.startswith("[SM] build-failed")
    assert "kernel crashed" in body


def test_main_missing_design_note_frontmatter_returns_1(tmp_path) -> None:
    """Bad prompt.txt (missing design_note frontmatter) — exit
    non-zero with a log line, no audit comment posted."""
    spawn = tmp_path / "spawn-185-4"
    spawn.mkdir()
    (spawn / "prompt.txt").write_text(
        "---\n"
        "phase: per_issue_build\n"
        "issue: #185\n"
        "art: art:code\n"
        "---\n"
        "Implement the feature.\n"
    )
    comments, post_comment = _record_comments()
    logs: list[str] = []

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--cwd",
            str(tmp_path / "repo"),
        ],
        kernel_runner=lambda *a, **kw: (0, ""),
        post_comment=post_comment,
        log=logs.append,
    )

    assert rc == 1
    assert comments == []
    assert any("design_note" in line for line in logs)


def test_main_missing_design_note_file_returns_1(tmp_path) -> None:
    """The design_note path is referenced but the file doesn't exist
    on disk — exit non-zero with a log line, no audit comment."""
    spawn = tmp_path / "spawn-185-5"
    spawn.mkdir()
    (spawn / "prompt.txt").write_text(
        "---\n"
        "phase: per_issue_build\n"
        "issue: #185\n"
        "design_note: /tmp/does-not-exist-xyz.md\n"
        "---\n"
        "body\n"
    )
    comments, post_comment = _record_comments()
    logs: list[str] = []

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--cwd",
            str(tmp_path / "repo"),
        ],
        kernel_runner=lambda *a, **kw: (0, ""),
        post_comment=post_comment,
        log=logs.append,
    )

    assert rc == 1
    assert comments == []
    assert any("does not exist" in line for line in logs)


def test_main_missing_issue_frontmatter_returns_1(tmp_path) -> None:
    """No ``issue:`` frontmatter — can't post any audit comment,
    so exit non-zero before kernel dispatch."""
    spawn = tmp_path / "spawn-185-6"
    design = tmp_path / "designs/note.md"
    design.parent.mkdir(parents=True)
    design.write_text("design")
    spawn.mkdir()
    (spawn / "prompt.txt").write_text(
        "---\n"
        "phase: per_issue_build\n"
        f"design_note: {design}\n"
        "---\n"
        "body\n"
    )
    comments, post_comment = _record_comments()

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--cwd",
            str(tmp_path / "repo"),
        ],
        kernel_runner=lambda *a, **kw: (0, ""),
        post_comment=post_comment,
        log=lambda msg: None,
    )

    assert rc == 1
    assert comments == []


def test_main_missing_prompt_txt_returns_1(tmp_path) -> None:
    spawn = tmp_path / "spawn-empty"
    spawn.mkdir()
    comments, post_comment = _record_comments()
    logs: list[str] = []

    rc = main(
        ["--spawn-dir", str(spawn)],
        kernel_runner=lambda *a, **kw: (0, ""),
        post_comment=post_comment,
        log=logs.append,
    )

    assert rc == 1
    assert comments == []
    assert any("missing prompt.txt" in line for line in logs)


def test_main_dry_run_skips_kernel_and_comment(tmp_path) -> None:
    """``--dry-run`` resolves phase + composes the prompt but skips
    both the kernel run and the audit comment — handy for the
    dispatcher's pre-launch probe."""
    spawn = tmp_path / "spawn-185-dry"
    design = tmp_path / "designs/note.md"
    _write_prompt(spawn, design_path=design)
    kernel_calls: list[Any] = []
    comments, post_comment = _record_comments()

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--dry-run",
            "--cwd",
            str(tmp_path / "repo"),
        ],
        kernel_runner=lambda *a, **kw: kernel_calls.append("called") or (0, ""),
        post_comment=post_comment,
        log=lambda msg: None,
    )

    assert rc == 0
    assert kernel_calls == []
    assert comments == []


def test_main_propagates_kernel_timeout(tmp_path) -> None:
    """Kernel timeout (rc=124) propagates back to the caller AND
    posts a build-failed comment so the dispatcher sees both signals."""
    spawn = tmp_path / "spawn-185-to"
    design = tmp_path / "designs/note.md"
    _write_prompt(spawn, design_path=design)
    _, kernel_runner = _kernel_runner_returning(124, "")
    comments, post_comment = _record_comments()

    rc = main(
        [
            "--spawn-dir",
            str(spawn),
            "--cwd",
            str(tmp_path / "repo"),
        ],
        kernel_runner=kernel_runner,
        post_comment=post_comment,
        log=lambda msg: None,
    )

    assert rc == 124
    assert len(comments) == 1
    _, _, body = comments[0]
    assert body.startswith("[SM] build-failed")
