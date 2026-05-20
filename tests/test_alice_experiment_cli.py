"""Tests for the ``alice-experiment`` CLI.

The CLI is a thin shell over :class:`ExperimentRunner`; the existing
test_run_experiment.py covers the runner's pipeline end-to-end. These
tests focus on the CLI's value-add:

- argparse validation (XOR, required, empty strings, missing paths)
- happy-path end-to-end with a mocked subprocess that writes a card
- timeout → exit 4
- subagent failure status → exit 5 with the failure summary in the JSON
- ``--no-json`` produces the human preamble + the JSON line

Subprocesses are mocked the same way the existing runner tests do it
(see :class:`_FakeProcess` in test_run_experiment.py). No real ``claude``
processes are spawned.
"""

from __future__ import annotations

import datetime
import io
import json
import pathlib
from typing import Any, Optional

from core.events import CapturingEmitter
from alice_thinking.cli import experiment as cli
from alice_thinking.experiments import (
    CardContent,
    ExperimentRunner,
    write_card,
)
from alice_thinking.experiments.submit_result import write_status_file


# ---------------------------------------------------------------------------
# Fake subprocess scaffolding — copied from test_run_experiment.py so the
# CLI tests can stand alone (no cross-file imports between test modules).


class _FakeStream:
    """Async readline() over a fixed list of bytes lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeProcess:
    """Minimal asyncio.subprocess.Process double for the runner."""

    def __init__(
        self,
        stdout_lines: list[bytes],
        stderr_lines: list[bytes],
        rc: int = 0,
    ) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self.stdin: Optional[Any] = None
        self.returncode: Optional[int] = None
        self._rc = rc

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc


def _make_runner_factory(
    tmp_path: pathlib.Path,
    *,
    subprocess_runner,
):
    """Build a runner_factory the CLI accepts, wired to a temp vault."""

    def factory(*, emitter, api_key, api_base_url) -> ExperimentRunner:
        return ExperimentRunner(
            emitter=emitter,
            vault_experiments_dir=tmp_path / "vault" / "experiments",
            surface_dir=tmp_path / "surface",
            experiments_jsonl=tmp_path / "experiments.jsonl",
            state_dir=tmp_path / "state",
            tmp_root=tmp_path / "tmproot",
            subprocess_runner=subprocess_runner,
            api_key=api_key,
            api_base_url=api_base_url,
        )

    return factory


def _capture_streams() -> tuple[io.StringIO, io.StringIO]:
    return io.StringIO(), io.StringIO()


# ---------------------------------------------------------------------------
# Validation — every branch produces exit 2.


def _common_args(tmp_path: pathlib.Path) -> list[str]:
    """A baseline-valid argv that test cases mutate one field at a time."""
    return [
        "--hypothesis",
        "Does the CLI dispatch?",
        "--expected-output",
        "summary-text",
        "--inline-instructions",
        "Do nothing.",
        "--log-path",
        str(tmp_path / "events.log"),
    ]


def test_missing_hypothesis_exits_2(tmp_path: pathlib.Path) -> None:
    """argparse-level: --hypothesis is required → exit 2."""
    out, err = _capture_streams()
    rc = cli.run(
        [
            "--expected-output",
            "summary-text",
            "--inline-instructions",
            "x",
            "--log-path",
            str(tmp_path / "events.log"),
        ],
        emitter=CapturingEmitter(),
        stdout=out,
        stderr=err,
    )
    assert rc == cli.EXIT_VALIDATION_ERROR


def test_both_method_and_inline_exits_2(tmp_path: pathlib.Path) -> None:
    """XOR violation: argparse mutually-exclusive group rejects both → exit 2."""
    out, err = _capture_streams()
    rc = cli.run(
        [
            "--hypothesis",
            "h",
            "--expected-output",
            "summary-text",
            "--inline-instructions",
            "inline",
            "--method",
            str(tmp_path / "nope.py"),
            "--log-path",
            str(tmp_path / "events.log"),
        ],
        emitter=CapturingEmitter(),
        stdout=out,
        stderr=err,
    )
    assert rc == cli.EXIT_VALIDATION_ERROR


def test_neither_method_nor_inline_exits_2(tmp_path: pathlib.Path) -> None:
    """XOR violation: argparse mutex group is required → exit 2."""
    out, err = _capture_streams()
    rc = cli.run(
        [
            "--hypothesis",
            "h",
            "--expected-output",
            "summary-text",
            "--log-path",
            str(tmp_path / "events.log"),
        ],
        emitter=CapturingEmitter(),
        stdout=out,
        stderr=err,
    )
    assert rc == cli.EXIT_VALIDATION_ERROR


def test_bad_context_path_exits_2(tmp_path: pathlib.Path) -> None:
    """Context-path that doesn't exist → exit 2 (our post-parse validator)."""
    out, err = _capture_streams()
    rc = cli.run(
        _common_args(tmp_path)
        + ["--context-paths", str(tmp_path / "does-not-exist.md")],
        emitter=CapturingEmitter(),
        stdout=out,
        stderr=err,
    )
    assert rc == cli.EXIT_VALIDATION_ERROR
    assert "does not exist" in err.getvalue()


def test_blank_hypothesis_exits_2(tmp_path: pathlib.Path) -> None:
    """Whitespace-only hypothesis → exit 2 (argparse can't catch that)."""
    out, err = _capture_streams()
    rc = cli.run(
        [
            "--hypothesis",
            "   ",
            "--expected-output",
            "summary-text",
            "--inline-instructions",
            "x",
            "--log-path",
            str(tmp_path / "events.log"),
        ],
        emitter=CapturingEmitter(),
        stdout=out,
        stderr=err,
    )
    assert rc == cli.EXIT_VALIDATION_ERROR
    assert "--hypothesis" in err.getvalue()


def test_missing_method_file_exits_2(tmp_path: pathlib.Path) -> None:
    """--method pointing at a non-file → exit 2 (caught before dispatch)."""
    out, err = _capture_streams()
    rc = cli.run(
        [
            "--hypothesis",
            "h",
            "--expected-output",
            "summary-text",
            "--method",
            str(tmp_path / "missing.py"),
            "--log-path",
            str(tmp_path / "events.log"),
        ],
        emitter=CapturingEmitter(),
        stdout=out,
        stderr=err,
    )
    assert rc == cli.EXIT_VALIDATION_ERROR
    assert "not found" in err.getvalue()


# ---------------------------------------------------------------------------
# Happy path — end-to-end with a mocked subprocess that writes the card.


def test_happy_path_writes_card_and_prints_json(tmp_path: pathlib.Path) -> None:
    """Dispatch → wait → card on disk + JSON-on-stdout with status=complete."""

    async def fake_subprocess_runner(
        args: list[str], *, env: dict, prompt: str
    ) -> _FakeProcess:
        # Mimic submit_result: write the card + status file the runner
        # polls for before "exiting".
        settings_idx = args.index("--settings")
        settings_path = pathlib.Path(args[settings_idx + 1])
        state_dir = settings_path.parent
        experiment_id = state_dir.name
        card_path = tmp_path / "vault" / "experiments" / f"{experiment_id}.md"
        now = datetime.datetime.now().astimezone()
        write_card(
            card_path,
            experiment_id=experiment_id,
            content=CardContent(
                title="CLI happy path",
                abstract="The CLI dispatched, waited, and read back the card.",
                hypothesis="The CLI works end-to-end.",
                method="Mocked subprocess.",
                results="JSON printed on stdout.",
                discussion="The thin shell holds.",
                conclusion="Confirmed.",
            ),
            dispatched_at=now - datetime.timedelta(seconds=2),
            completed_at=now,
            duration_seconds=2.0,
            transcript_path=str(state_dir / "transcript.jsonl"),
        )
        write_status_file(
            state_dir,
            experiment_id=experiment_id,
            card_path=card_path,
            status="complete",
            completed_at=now,
        )
        return _FakeProcess(
            stdout_lines=[b'{"type":"assistant","text":"hi"}\n', b""],
            stderr_lines=[b""],
        )

    emitter = CapturingEmitter()
    out, err = _capture_streams()
    rc = cli.run(
        _common_args(tmp_path),
        emitter=emitter,
        runner_factory=_make_runner_factory(
            tmp_path, subprocess_runner=fake_subprocess_runner
        ),
        stdout=out,
        stderr=err,
    )
    assert rc == cli.EXIT_OK, err.getvalue()

    # Stdout must be a single parseable JSON line.
    stdout_text = out.getvalue().strip()
    assert "\n" not in stdout_text, "default --json mode prints one line"
    payload = json.loads(stdout_text)
    assert payload["status"] == "complete"
    assert payload["experiment_id"].startswith("exp-")
    card_path = pathlib.Path(payload["card_path"])
    assert card_path.is_file()
    # Abstract body flows through as the summary.
    assert "CLI dispatched" in payload["summary"]

    # And the on-disk card matches.
    body = card_path.read_text()
    assert "## Conclusion" in body
    assert "CLI happy path" in body

    # CLI-flavoured dispatch event is on the emitter.
    cli_events = [e for e in emitter.events if e["event"] == "experiment_cli_dispatch"]
    assert len(cli_events) == 1
    assert cli_events[0]["via"] == "alice-experiment"


def test_happy_path_no_json_includes_preamble(tmp_path: pathlib.Path) -> None:
    """--no-json prepends two human lines before the JSON."""

    async def fake_subprocess_runner(
        args: list[str], *, env: dict, prompt: str
    ) -> _FakeProcess:
        settings_idx = args.index("--settings")
        state_dir = pathlib.Path(args[settings_idx + 1]).parent
        experiment_id = state_dir.name
        card_path = tmp_path / "vault" / "experiments" / f"{experiment_id}.md"
        now = datetime.datetime.now().astimezone()
        write_card(
            card_path,
            experiment_id=experiment_id,
            content=CardContent(
                title="t",
                abstract="a",
                hypothesis="h",
                method="m",
                results="r",
                discussion="d",
                conclusion="c",
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
        return _FakeProcess(stdout_lines=[b""], stderr_lines=[b""])

    out, err = _capture_streams()
    rc = cli.run(
        _common_args(tmp_path) + ["--no-json"],
        emitter=CapturingEmitter(),
        runner_factory=_make_runner_factory(
            tmp_path, subprocess_runner=fake_subprocess_runner
        ),
        stdout=out,
        stderr=err,
    )
    assert rc == cli.EXIT_OK
    stdout_lines = out.getvalue().strip().splitlines()
    assert len(stdout_lines) == 3
    assert stdout_lines[0].startswith("alice-experiment exp-")
    assert stdout_lines[1].startswith("card: ")
    json.loads(stdout_lines[2])  # last line parses as JSON


# ---------------------------------------------------------------------------
# Failure modes — exit 5 with failure_reason in the JSON.


def test_subagent_failure_exits_5(tmp_path: pathlib.Path) -> None:
    """No card written → failed-stub card → exit 5 + failure_reason in JSON."""

    async def fake_subprocess_runner(
        args: list[str], *, env: dict, prompt: str
    ) -> _FakeProcess:
        # No card, no status file — runner writes a failed-stub card.
        return _FakeProcess(
            stdout_lines=[b""],
            stderr_lines=[b"fatal: simulated crash\n", b""],
            rc=1,
        )

    out, err = _capture_streams()
    rc = cli.run(
        _common_args(tmp_path),
        emitter=CapturingEmitter(),
        runner_factory=_make_runner_factory(
            tmp_path, subprocess_runner=fake_subprocess_runner
        ),
        stdout=out,
        stderr=err,
    )
    assert rc == cli.EXIT_SUBAGENT_FAILURE
    payload = json.loads(out.getvalue().strip())
    assert payload["status"] == "failed"
    assert payload["failure_reason"] is not None
    assert "subagent exited" in payload["failure_reason"]


# ---------------------------------------------------------------------------
# Timeout — exit 4.
#
# The CLI's --timeout-seconds flows into ``runner.wait_for(timeout=N)``.
# With timeout=0 the CLI's asyncio.wait_for raises TimeoutError before
# the runner task drains, the CLI surfaces a ``status=timeout`` payload,
# and returns EXIT_TIMEOUT.


def test_cli_timeout_exits_4(tmp_path: pathlib.Path) -> None:
    """``--timeout-seconds 0`` → CLI wait_for times out → exit 4."""

    async def fake_subprocess_runner(
        args: list[str], *, env: dict, prompt: str
    ) -> _FakeProcess:
        # The fake itself never blocks; it doesn't matter, because the
        # CLI's wait_for(timeout=0) trips before the runner task gets to
        # finish. Return something benign so the runner can spawn cleanly.
        return _FakeProcess(
            stdout_lines=[b""],
            stderr_lines=[b""],
            rc=0,
        )

    out, err = _capture_streams()
    rc = cli.run(
        _common_args(tmp_path) + ["--timeout-seconds", "0"],
        emitter=CapturingEmitter(),
        runner_factory=_make_runner_factory(
            tmp_path, subprocess_runner=fake_subprocess_runner
        ),
        stdout=out,
        stderr=err,
    )
    assert rc == cli.EXIT_TIMEOUT
    payload = json.loads(out.getvalue().strip())
    assert payload["status"] == "timeout"
    assert "timed out" in (payload.get("error") or "")
    # experiment_id is still surfaced so the caller can correlate logs
    # if the background card eventually lands.
    assert payload["experiment_id"].startswith("exp-")
