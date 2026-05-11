"""Tests for the run_experiment MCP tool + ExperimentRunner pipeline.

Covers (per the commission's H. Tests section):

- experiment_id format
- permission-rules file generation (deny list correct)
- card writer well-formed frontmatter + sections
- failed-stub card writer when subagent doesn't call submit_result
- end-to-end happy path with a mocked claude subprocess

The mocked subprocess (``_FakeProcess``) replays a deterministic
stdout/stderr stream so the runner exercises its full pipeline — write
permission rules → spawn → stream → poll status file → emit side effects.
The fake also writes the card + status file itself, simulating what the
real submit_result MCP server would do in production.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
from typing import Any, Optional

import pytest

from alice_core.events import CapturingEmitter
from alice_thinking.experiments import (
    CardContent,
    ExperimentDispatchError,
    ExperimentRunner,
    new_experiment_id,
    write_card,
    write_failed_stub_card,
)
from alice_thinking.experiments.permissions import (
    READ_ONLY_PATH_PREFIXES,
    generate_permission_rules,
    render_rules_dict,
)
from alice_thinking.experiments.submit_result import write_status_file


# ---------------------------------------------------------------------------
# experiment_id format


def test_new_experiment_id_format_matches_spec() -> None:
    """``exp-YYYY-MM-DD-HHMMSS-<6-char-hex>`` — pin the shape."""
    now = datetime.datetime(2026, 5, 11, 14, 32, 45)
    eid = new_experiment_id(now)
    # Anchored regex: prefix + ISO-ish date + time + dash + 6 hex chars.
    assert re.match(
        r"^exp-2026-05-11-143245-[0-9a-f]{6}$", eid
    ), f"unexpected experiment_id: {eid!r}"


def test_new_experiment_id_is_unique_per_call() -> None:
    """Two calls with the same now should produce different ids (hex suffix)."""
    now = datetime.datetime(2026, 5, 11, 14, 32, 45)
    a = new_experiment_id(now)
    b = new_experiment_id(now)
    assert a != b
    assert a.split("-")[:5] == b.split("-")[:5]


# ---------------------------------------------------------------------------
# permission-rules file


def test_permission_rules_deny_list_is_correct() -> None:
    """All seven Bash prefixes the spec names must appear in the deny list."""
    rules = render_rules_dict()
    deny_set = set(rules["deny"])
    # Every spec'd prefix should be present.
    for prefix in (
        "claude",
        "git push",
        "signal-cli",
        "curl",
        "docker",
        "sudo",
        "ssh",
    ):
        assert f"Bash({prefix}:*)" in deny_set, f"missing Bash deny for {prefix}"
    # Real repo should be deny-write.
    for prefix in READ_ONLY_PATH_PREFIXES:
        assert f"Write({prefix}**)" in deny_set
        assert f"Edit({prefix}**)" in deny_set


def test_permission_rules_recursion_guardrail() -> None:
    """The ``claude`` deny is the structural recursion fix (Concern 3)."""
    rules = render_rules_dict()
    assert any("Bash(claude:" in r for r in rules["deny"])


def test_permission_rules_writable_copy_grants_only_when_set(tmp_path: pathlib.Path) -> None:
    """Without ``writable_repo_copy`` the rules don't reference /tmp/alice-copy."""
    rules_no_copy = render_rules_dict()
    flat = json.dumps(rules_no_copy)
    assert "alice-copy" not in flat

    copy_path = tmp_path / "alice-copy-test"
    rules_with_copy = render_rules_dict(writable_repo_copy=copy_path)
    flat = json.dumps(rules_with_copy)
    assert str(copy_path) in flat
    # Allow rule for Write to the copy must exist.
    assert any(
        r.startswith("Write(") and str(copy_path) in r
        for r in rules_with_copy["allow"]
    )


def test_generate_permission_rules_writes_settings_envelope(tmp_path: pathlib.Path) -> None:
    """The CLI loads --settings JSON; file must be {"permissions": {...}}."""
    target = tmp_path / "settings.json"
    generate_permission_rules(target)
    blob = json.loads(target.read_text())
    assert "permissions" in blob
    perms = blob["permissions"]
    assert isinstance(perms["allow"], list) and perms["allow"]
    assert isinstance(perms["deny"], list) and perms["deny"]


# ---------------------------------------------------------------------------
# card writer


def _frontmatter(text: str) -> dict[str, str]:
    """Crude YAML frontmatter parser sufficient for the test's needs.

    The card writer's frontmatter is hand-rolled (no PyYAML dep), so the
    test parses it line-by-line. Values come back as raw strings; the
    caller asserts on substrings.
    """
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    assert end != -1, "frontmatter not terminated"
    body = text[4:end]
    out: dict[str, str] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def test_write_card_produces_six_sections(tmp_path: pathlib.Path) -> None:
    """The card must carry Abstract/Hypothesis/Method/Results/Discussion/Conclusion."""
    target = tmp_path / "exp-2026-05-11-000000-aaa111.md"
    content = CardContent(
        title="A test experiment",
        abstract="We tested X and found Y.",
        hypothesis="X holds.",
        method="Compute Y from X.",
        results="Y = 42.",
        discussion="Interesting because.",
        conclusion="X confirmed.",
        result_paths=["/tmp/output.json"],
    )
    dispatched = datetime.datetime(2026, 5, 11, 7, 32, 45).astimezone()
    completed = dispatched + datetime.timedelta(minutes=18, seconds=33)
    write_card(
        target,
        experiment_id="exp-2026-05-11-000000-aaa111",
        content=content,
        dispatched_at=dispatched,
        completed_at=completed,
        duration_seconds=(completed - dispatched).total_seconds(),
        transcript_path="/state/exp/transcript.jsonl",
        repo_under_test="alice",
        tool_calls_made=47,
    )
    body = target.read_text()
    fm = _frontmatter(body)
    assert "exp-2026-05-11-000000-aaa111" in fm["experiment_id"]
    assert fm["status"] == "complete"
    assert "alice" in fm["repo_under_test"]
    assert "47" in fm["tool_calls_made"]
    # Has-transcript flag must be true.
    assert fm["has_transcript"] == "true"
    # Each section is present.
    for heading in (
        "## Abstract",
        "## Hypothesis",
        "## Method",
        "## Results",
        "## Discussion",
        "## Conclusion",
        "## Cross-references",
    ):
        assert heading in body, f"missing section {heading}"
    # Title appears as H1.
    assert "# A test experiment\n" in body


def test_write_card_result_paths_in_frontmatter(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "card.md"
    content = CardContent(
        title="t",
        abstract="a",
        hypothesis="h",
        method="m",
        results="r",
        discussion="d",
        conclusion="c",
        result_paths=["/tmp/plot.png", "/tmp/metrics.json"],
    )
    now = datetime.datetime.now().astimezone()
    write_card(
        target,
        experiment_id="exp-test",
        content=content,
        dispatched_at=now,
        completed_at=now,
        duration_seconds=1.0,
        transcript_path="/tmp/transcript.jsonl",
    )
    blob = target.read_text()
    assert "/tmp/plot.png" in blob
    assert "/tmp/metrics.json" in blob


def test_failed_stub_card_has_failed_status_and_failure_reason(tmp_path: pathlib.Path) -> None:
    """The stub writer's contract: status=failed + failure_reason in frontmatter."""
    target = tmp_path / "stub.md"
    now = datetime.datetime.now().astimezone()
    write_failed_stub_card(
        target,
        experiment_id="exp-fail-001",
        hypothesis="X does not hold.",
        dispatched_at=now,
        completed_at=now + datetime.timedelta(seconds=5),
        duration_seconds=5.0,
        transcript_path="/tmp/transcript.jsonl",
        failure_reason="timeout after 1800s",
    )
    body = target.read_text()
    fm = _frontmatter(body)
    assert fm["status"] == "failed"
    assert "timeout" in fm["failure_reason"]
    # Hypothesis still in frontmatter so thinking has something to act on.
    assert "X does not hold" in fm["hypothesis"]
    # All six sections still present (with stub bodies).
    for heading in (
        "## Abstract",
        "## Hypothesis",
        "## Method",
        "## Results",
        "## Discussion",
        "## Conclusion",
    ):
        assert heading in body


# ---------------------------------------------------------------------------
# Dispatch validation


def _make_runner(tmp_path: pathlib.Path, **kw) -> ExperimentRunner:
    emitter = CapturingEmitter()
    return ExperimentRunner(
        emitter=emitter,
        vault_experiments_dir=tmp_path / "vault" / "experiments",
        surface_dir=tmp_path / "surface",
        experiments_jsonl=tmp_path / "experiments.jsonl",
        state_dir=tmp_path / "state",
        tmp_root=tmp_path / "tmproot",
        **kw,
    )


def test_dispatch_rejects_both_method_and_inline(tmp_path: pathlib.Path) -> None:
    runner = _make_runner(tmp_path)
    with pytest.raises(ExperimentDispatchError):
        runner.dispatch(
            hypothesis="h",
            method="/tmp/somewhere.py",
            inline_instructions="also this",
            expected_output="metrics-table",
        )


def test_dispatch_rejects_neither_method_nor_inline(tmp_path: pathlib.Path) -> None:
    runner = _make_runner(tmp_path)
    with pytest.raises(ExperimentDispatchError):
        runner.dispatch(
            hypothesis="h",
            method=None,
            inline_instructions=None,
            expected_output="metrics-table",
        )


def test_dispatch_rejects_missing_method_file(tmp_path: pathlib.Path) -> None:
    runner = _make_runner(tmp_path)
    with pytest.raises(ExperimentDispatchError):
        runner.dispatch(
            hypothesis="h",
            method=str(tmp_path / "does-not-exist.py"),
            inline_instructions=None,
            expected_output="metrics-table",
        )


# ---------------------------------------------------------------------------
# End-to-end happy path with a mocked claude subprocess
#
# The fake subprocess writes the card + status file itself (simulating
# the production submit_result MCP server) and emits two JSON lines on
# stdout so the transcript exercises the JSON-passthrough path.


class _FakeProcess:
    """Minimal asyncio.subprocess.Process double for the runner.

    The runner calls ``process.stdout`` / ``process.stderr`` (streams),
    ``process.stdin`` (writer), and ``process.wait()``. The fake also
    exposes ``returncode`` after ``wait()``.
    """

    def __init__(self, stdout_lines: list[bytes], stderr_lines: list[bytes], rc: int = 0) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self.stdin: Optional[Any] = None
        self.returncode: Optional[int] = None
        self._rc = rc

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc


class _FakeStream:
    """Async readline() over a fixed list of bytes lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


@pytest.mark.asyncio
async def test_end_to_end_happy_path(tmp_path: pathlib.Path) -> None:
    """Dispatch + complete: card written, surface dropped, event emitted, jsonl appended."""
    # The fake subprocess pretends to be the claude CLI: it writes the card
    # + status file directly (just like the real submit_result MCP server
    # would when called from inside the subagent) before "exiting".
    captured_card_paths: list[pathlib.Path] = []

    async def fake_subprocess_runner(args: list[str], *, env: dict, prompt: str) -> _FakeProcess:
        # Pull the per-experiment paths out of the argv. The runner spawns
        # claude with --settings <path>; the path encodes the experiment_id
        # in its parent directory name.
        settings_idx = args.index("--settings")
        settings_path = pathlib.Path(args[settings_idx + 1])
        state_dir = settings_path.parent
        # Card path is reconstructable from the experiment_id (the leaf of
        # state_dir).
        experiment_id = state_dir.name
        card_path = tmp_path / "vault" / "experiments" / f"{experiment_id}.md"
        captured_card_paths.append(card_path)

        # Production submit_result would write a card; mimic that.
        now = datetime.datetime.now().astimezone()
        write_card(
            card_path,
            experiment_id=experiment_id,
            content=CardContent(
                title="Mocked experiment",
                abstract="Fake subprocess wrote a card directly.",
                hypothesis="The runner correctly orchestrates the lifecycle.",
                method="Mocked.",
                results="Pass.",
                discussion="The card lands at the spec'd path.",
                conclusion="Confirmed.",
            ),
            dispatched_at=now - datetime.timedelta(seconds=1),
            completed_at=now,
            duration_seconds=1.0,
            transcript_path=str(state_dir / "transcript.jsonl"),
        )
        write_status_file(
            state_dir,
            experiment_id=experiment_id,
            card_path=card_path,
            status="complete",
            completed_at=now,
        )
        # Return a fake process that yields a couple of jsonl lines + a
        # bare text line so the transcript stream exercises both branches.
        return _FakeProcess(
            stdout_lines=[
                b'{"type":"assistant","text":"hi"}\n',
                b"plain stdout line\n",
                b"",  # EOF
            ],
            stderr_lines=[b"warning: nothing real here\n", b""],
        )

    runner = _make_runner(tmp_path, subprocess_runner=fake_subprocess_runner)
    meta = runner.dispatch(
        hypothesis="Will the runner write a card via submit_result?",
        method=None,
        inline_instructions="Run a no-op and call submit_result.",
        expected_output="summary-text",
    )
    # Dispatch metadata must match the spec's keys.
    payload = meta.to_tool_response()
    assert payload["status"] == "dispatched"
    assert payload["experiment_id"].startswith("exp-")
    assert payload["card_path"].endswith(f"{meta.experiment_id}.md")
    assert payload["transcript_path"].endswith("transcript.jsonl")
    # Wait for the background task to drain.
    await runner.wait_for(meta.experiment_id, timeout=10)

    # Card landed at the spec'd path with the right content.
    card_path = captured_card_paths[0]
    assert card_path.is_file()
    body = card_path.read_text()
    assert "Mocked experiment" in body
    assert "## Conclusion" in body

    # Surface note was dropped.
    surface_files = list((tmp_path / "surface").rglob("*-experiment-complete-*.md"))
    assert len(surface_files) == 1
    surface_body = surface_files[0].read_text()
    assert "complete" in surface_body
    assert meta.experiment_id in surface_body

    # JSONL line was appended.
    jsonl_lines = (tmp_path / "experiments.jsonl").read_text().splitlines()
    assert len(jsonl_lines) == 1
    record = json.loads(jsonl_lines[0])
    assert record["experiment_id"] == meta.experiment_id
    assert record["status"] == "complete"
    assert record["duration_seconds"] is not None

    # Event emitter saw the experiment-card event.
    emitter = runner.emitter  # type: ignore[attr-defined]
    matching = [e for e in emitter.events if e["event"] == "experiment-card"]
    assert len(matching) == 1
    evt = matching[0]
    assert evt["experiment_id"] == meta.experiment_id
    assert evt["status"] == "complete"

    # Transcript captured both JSON + plain lines + stderr.
    transcript = (tmp_path / "state" / meta.experiment_id / "transcript.jsonl").read_text()
    transcript_records = [json.loads(line) for line in transcript.splitlines() if line]
    sources = {r["source"] for r in transcript_records}
    assert "stdout" in sources
    assert "stderr" in sources
    # The JSON line was passed through as an event, not text.
    assert any("event" in r for r in transcript_records if r["source"] == "stdout")


@pytest.mark.asyncio
async def test_end_to_end_failed_stub_when_subagent_does_not_submit(tmp_path: pathlib.Path) -> None:
    """When the subprocess exits without writing the status file, we get a stub."""

    async def fake_subprocess_runner(args: list[str], *, env: dict, prompt: str) -> _FakeProcess:
        # Do NOT write a card or status file — simulate a crashed subagent.
        return _FakeProcess(
            stdout_lines=[b"", ],
            stderr_lines=[b"fatal: simulated crash\n", b""],
            rc=1,
        )

    runner = _make_runner(tmp_path, subprocess_runner=fake_subprocess_runner)
    meta = runner.dispatch(
        hypothesis="Does the runner stub a card when submit_result is never called?",
        method=None,
        inline_instructions="Do nothing.",
        expected_output="summary-text",
    )
    await runner.wait_for(meta.experiment_id, timeout=10)

    card_body = meta.card_path.read_text()
    assert "status: failed" in card_body
    assert "subagent exited" in card_body

    jsonl_lines = (tmp_path / "experiments.jsonl").read_text().splitlines()
    record = json.loads(jsonl_lines[-1])
    assert record["status"] == "failed"


# ---------------------------------------------------------------------------
# Note: async tests use the explicit ``@pytest.mark.asyncio`` decorator;
# the project's pytest-asyncio mode is "strict" (per pyproject.toml) so we
# never default-mark every test in this file.
