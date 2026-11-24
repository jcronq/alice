"""Shared fixtures for the hooks test package.

The two user-global scripts under ``sandbox/claude-hooks/`` are not importable
(they're hashbang scripts bound to ``/state/worker/...`` paths). Each test
copies the script into a tmp dir, rewrites the hard-coded log paths to the
tmp jsonls, then invokes it via a subprocess. This keeps the production
scripts single-purpose and testable without heavy mocking.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "sandbox" / "claude-hooks"


def _rewrite_script(script_path: Path, stop_log: Path, start_log: Path) -> Path:
    """Copy a hook script into a tmp dir with the log paths patched.

    Both hook scripts hard-code the production jsonl paths at module level.
    Rewriting those constants is cheaper than monkeypatching inside a child
    process and keeps the tests close to a real invocation.
    """
    text = script_path.read_text(encoding="utf-8")
    replaced = (
        text.replace(
            'STOP_LOG = Path("/state/worker/alice-subagent-stop.jsonl")',
            f'STOP_LOG = Path({str(stop_log)!r})',
        )
        .replace(
            'START_LOG = Path("/state/worker/alice-task-dispatch.jsonl")',
            f'START_LOG = Path({str(start_log)!r})',
        )
    )
    patched = script_path.with_name(script_path.stem + "_patched.py")
    # Write into a per-test tmp dir rather than mutating the repo file.
    return patched, replaced


@pytest.fixture
def hook_runner(tmp_path):
    """Return a callable(script_name, stdin_payload) → (stop_rows, stderr)."""

    stop_log = tmp_path / "alice-subagent-stop.jsonl"
    start_log = tmp_path / "alice-task-dispatch.jsonl"

    def _runner(script_name: str, stdin_payload: dict | str) -> tuple[list[dict], str]:
        src_path = HOOKS_DIR / script_name
        _patched_name, patched_text = _rewrite_script(src_path, stop_log, start_log)
        patched_path = tmp_path / script_name
        patched_path.write_text(patched_text, encoding="utf-8")

        payload_text = (
            stdin_payload if isinstance(stdin_payload, str) else json.dumps(stdin_payload)
        )
        proc = subprocess.run(
            [sys.executable, str(patched_path)],
            input=payload_text.encode(),
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 0, (
            f"{script_name} exited non-zero: {proc.stderr.decode(errors='replace')}"
        )
        rows: list[dict] = []
        if stop_log.exists():
            for line in stop_log.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return rows, proc.stderr.decode(errors="replace")

    _runner.stop_log = stop_log  # type: ignore[attr-defined]
    _runner.start_log = start_log  # type: ignore[attr-defined]
    return _runner


@pytest.fixture
def start_log_writer(hook_runner):
    """Append rows to the start log the hook_runner uses."""

    def _write(rows: list[dict]) -> None:
        with hook_runner.start_log.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    return _write
