"""Regression test for the host-claude watcher's inotify event loop.

Issue #133: with `inotifywait` invoked without `-m`, the watcher exits
after the first event. The MCP tool writes inbox files via tempfile+
rename in the same directory, which fires CLOSE_WRITE on the .tmp file
before the MOVED_TO on the final .md name. Without `-m`, inotifywait
exits on that first CLOSE_WRITE event — which we filter out as
non-`*.md` — and the subsequent MOVED_TO event for the real file is
gone by the time the outer loop respawns inotifywait. Every request
then stalls indefinitely, which matches the symptom reported in the
issue: file written into inbox/, no outbox, MCP wrapper times out.

This test starts the bash watcher under its real inotify branch, drops
a single request file via the same tempfile+rename the MCP tool uses,
and asserts the outbox file appears within a few seconds. Pre-fix the
outbox never appears; post-fix it round-trips immediately.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import signal
import subprocess
import textwrap
import time

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WATCHER = REPO_ROOT / "sandbox" / "host-claude-watcher" / "alice-host-claude-watcher.sh"


pytestmark = pytest.mark.skipif(
    shutil.which("inotifywait") is None,
    reason="inotify-tools not installed; live-watch test needs inotifywait",
)


def test_startup_banner_includes_script_sha(tmp_path: pathlib.Path):
    """Issue #144: stale `/usr/local/bin/` copies of the watcher silently
    survived `systemctl restart`, leaving the host running pre-#138 code
    while the repo claimed the fix was deployed. The startup banner now
    emits `script_sha=<12hex>` so `journalctl` answers "which version is
    running?" at a glance — a deploy that didn't take shows the old sha.

    We invoke the watcher in --single-shot mode (no watch loop, exits
    after draining the empty inbox) and assert the banner is in stderr
    and matches the script's actual sha256 prefix.
    """
    root = tmp_path / "host-claude"
    for d in (root / "inbox", root / "outbox", root / ".handled"):
        d.mkdir(parents=True)

    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_claude.chmod(0o755)

    env = os.environ.copy()
    env["ALICE_HOST_CLAUDE_ROOT"] = str(root)
    env["ALICE_HOST_CLAUDE_BIN"] = str(fake_claude)

    proc = subprocess.run(
        ["bash", str(WATCHER), "--single-shot"],
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")

    stderr = proc.stderr.decode("utf-8", errors="replace")
    expected_sha = hashlib.sha256(WATCHER.read_bytes()).hexdigest()[:12]
    assert "alice-host-claude-watcher starting" in stderr, stderr
    assert f"script_sha={expected_sha}" in stderr, stderr
    assert f"inbox_dir={root / 'inbox'}" in stderr, stderr


def _write_request(inbox: pathlib.Path, request_id: str, body: str) -> pathlib.Path:
    """Mirror the MCP tool's tempfile+rename so the watcher only sees the
    final filename via `moved_to`, not a half-written file via `close_write`.
    This is the exact pattern in src/alice_speaking/tools/host_claude.py
    (_atomic_write) — fakery here would defeat the purpose of the test.
    """
    final = inbox / f"{request_id}.md"
    tmp = inbox / f".{request_id}.md.tmp"
    tmp.write_text(
        textwrap.dedent(
            f"""\
            ---
            id: {request_id}
            requested_by: speaking
            created_at: 2026-05-12T00:00:00Z
            urgency: normal
            timeout_seconds: 30
            allow_destructive: false
            ---
            # Task

            {body}
            """
        )
    )
    os.replace(tmp, final)
    return final


def _wait_for_outbox(outbox_path: pathlib.Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if outbox_path.is_file():
            return True
        time.sleep(0.1)
    return False


def _wait_for_inotifywait_on(target: pathlib.Path, timeout: float = 5.0) -> None:
    """Block until an inotifywait process exists with `target` in its
    cmdline. A fixed sleep is racy — the watcher's bash + fork dance can
    take a surprising fraction of a second on a loaded runner, and a too-
    short sleep silently turns the test into a race over whether the
    watch is attached before the file lands.
    """
    target_str = str(target)
    deadline = time.monotonic() + timeout
    proc_dir = pathlib.Path("/proc")
    while time.monotonic() < deadline:
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
            except (FileNotFoundError, PermissionError):
                continue
            parts = cmdline.split(b"\x00")
            if not parts or b"inotifywait" not in parts[0]:
                continue
            if any(p.decode("utf-8", "replace") == target_str for p in parts):
                return
        time.sleep(0.05)
    raise AssertionError(
        f"inotifywait never attached to {target_str} within {timeout}s"
    )


def test_watcher_processes_tempfile_rename_request(tmp_path: pathlib.Path):
    """A request file dropped via the MCP tool's tempfile+rename pattern
    round-trips through inbox → outbox within seconds.

    Pre-fix (no `-m`): the .tmp file's CLOSE_WRITE fires first and
    exhausts inotifywait. The MOVED_TO for the .md is missed. Outbox
    stays empty.
    """
    root = tmp_path / "host-claude"
    inbox = root / "inbox"
    outbox = root / "outbox"
    handled = root / ".handled"
    for d in (inbox, outbox, handled):
        d.mkdir(parents=True)

    # Stub claude: print a marker and echo stdin. Keeps the test offline
    # and deterministic.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            echo "FAKE-CLAUDE-OK"
            cat
            """
        )
    )
    fake_claude.chmod(0o755)

    env = os.environ.copy()
    env["ALICE_HOST_CLAUDE_ROOT"] = str(root)
    env["ALICE_HOST_CLAUDE_BIN"] = str(fake_claude)

    # Capture watcher stderr to a file so we can surface diagnostics in
    # the failure message without blocking on an open pipe.
    stderr_log = tmp_path / "watcher.stderr"
    stderr_fh = stderr_log.open("wb")
    proc = subprocess.Popen(
        ["bash", str(WATCHER)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
        # New process group so SIGTERM at teardown reaches the
        # inotifywait child too.
        start_new_session=True,
    )
    try:
        _wait_for_inotifywait_on(inbox, timeout=5.0)

        request_id = "2026-05-12T00-00-00Z-tempfile-rename-smoke"
        _write_request(inbox, request_id, "echo hello")

        out_path = outbox / f"{request_id}.md"
        if not _wait_for_outbox(out_path, timeout=8.0):
            pytest.fail(
                "request never processed — inotifywait likely missing -m "
                "(MOVED_TO event lost after the .tmp file's CLOSE_WRITE)\n"
                f"watcher stderr:\n{stderr_log.read_text(errors='replace')}"
            )

        body = out_path.read_text()
        assert "status: success" in body
        assert "FAKE-CLAUDE-OK" in body
    finally:
        stderr_fh.close()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)
