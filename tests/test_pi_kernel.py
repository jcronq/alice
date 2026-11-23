"""End-to-end-ish tests for kernels.pi.kernel.PiKernel.

Uses a tmp shell script as the ``pi`` binary that emits a recorded
JSONL stream — exercises argv construction, JSONL parsing, event
translation, and KernelResult assembly without spinning up real pi.
"""

from __future__ import annotations

import pathlib
import stat

import pytest

from core.events import CapturingEmitter
from core.kernel import KernelSpec, NullHandler, TurnSummary

from kernels.pi.kernel import PiKernel


def _write_fake_pi(
    tmp_path: pathlib.Path, jsonl_lines: list[str], *, exit_code: int = 0
) -> pathlib.Path:
    """Write a bash script that prints the given JSONL lines then
    exits. Used as ``ALICE_PI_BIN`` so PiKernel subprocesses it
    instead of the real pi."""
    body = "\n".join(jsonl_lines)
    script = tmp_path / "fake-pi"
    content = (
        "#!/usr/bin/env bash\n"
        "cat <<'PI_FIXTURE_EOF'\n"
        f"{body}\n"
        "PI_FIXTURE_EOF\n"
        f"exit {exit_code}\n"
    )
    script.write_text(content)
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


@pytest.fixture
def fake_pi_env(monkeypatch, tmp_path):
    """Wire ALICE_PI_BIN to a fake script in tmp_path; return a
    helper that writes a fixture and returns the script path."""
    captured: dict = {}

    def make(jsonl_lines: list[str], *, exit_code: int = 0) -> pathlib.Path:
        script = _write_fake_pi(tmp_path, jsonl_lines, exit_code=exit_code)
        monkeypatch.setenv("ALICE_PI_BIN", str(script))
        captured["script"] = script
        return script

    yield make


@pytest.mark.asyncio
async def test_pi_kernel_run_against_recorded_stream(fake_pi_env, tmp_path) -> None:
    """Replay the spike's gpt-5.3-codex 'ok' turn through PiKernel
    and confirm KernelResult shape + handler fan-out."""
    fake_pi_env(
        [
            '{"type":"session","version":3,"id":"sess-fake","cwd":"/tmp"}',
            '{"type":"agent_start"}',
            '{"type":"turn_start"}',
            (
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"text_delta","delta":"ok"}}'
            ),
            (
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"text_end","content":"ok"}}'
            ),
            (
                '{"type":"message_end","message":{"role":"assistant",'
                '"content":[{"type":"text","text":"ok"}],'
                '"usage":{"input":1050,"output":5,"cacheRead":0,'
                '"cacheWrite":0,"totalTokens":1055},'
                '"timestamp":1777599884167}}'
            ),
            '{"type":"turn_end","message":{"role":"assistant"},"toolResults":[]}',
            '{"type":"agent_end","messages":[]}',
        ]
    )

    cap = CapturingEmitter()
    kernel = PiKernel(cap, correlation_id="t-1")

    fired_text: list[str] = []
    fired_results: list[TurnSummary] = []

    class H(NullHandler):
        async def on_text(self, text):
            fired_text.append(text)

        async def on_result(self, summary):
            fired_results.append(summary)

    result = await kernel.run(
        "say ok",
        KernelSpec(model="gpt-5.3-codex", max_seconds=0),
        handlers=[H()],
    )

    assert result.text == "ok"
    assert result.session_id == "sess-fake"
    assert result.usage is not None
    assert result.usage.input_tokens == 1050
    assert result.cost_usd is None  # subscription-billed
    assert result.is_error is False
    assert fired_text == ["ok"]
    assert len(fired_results) == 1
    assert fired_results[0].session_id == "sess-fake"


@pytest.mark.asyncio
async def test_pi_kernel_propagates_pi_exit_failure(fake_pi_env) -> None:
    """Pi exits non-zero (e.g. auth failure) → PiKernel surfaces it
    as RuntimeError so the caller can decide retry semantics."""
    fake_pi_env(
        [
            '{"type":"session","version":3,"id":"x","cwd":"/tmp"}',
        ],
        exit_code=2,
    )

    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    with pytest.raises(RuntimeError, match=r"pi exited 2"):
        await kernel.run("x", KernelSpec(model="gpt-5.3-codex"))


@pytest.mark.asyncio
async def test_pi_kernel_emits_turn_id_when_correlation_id_set(fake_pi_env) -> None:
    fake_pi_env(
        [
            '{"type":"session","version":3,"id":"s","cwd":"/tmp"}',
            (
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"text_delta","delta":"hi"}}'
            ),
            (
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"text_end","content":"hi"}}'
            ),
            '{"type":"agent_end","messages":[]}',
        ]
    )
    cap = CapturingEmitter()
    kernel = PiKernel(cap, correlation_id="my-turn")
    await kernel.run("ping", KernelSpec(model="gpt-5.3-codex"))
    text_evts = [e for e in cap.events if e["event"] == "assistant_text"]
    assert text_evts and text_evts[0]["turn_id"] == "my-turn"


@pytest.mark.asyncio
async def test_pi_kernel_silent_suppresses_events_but_runs_handlers(
    fake_pi_env,
) -> None:
    fake_pi_env(
        [
            '{"type":"session","version":3,"id":"s","cwd":"/tmp"}',
            (
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"text_delta","delta":"hi"}}'
            ),
            (
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"text_end","content":"hi"}}'
            ),
            '{"type":"agent_end","messages":[]}',
        ]
    )
    cap = CapturingEmitter()
    kernel = PiKernel(cap, silent=True)

    fired: list[str] = []

    class H(NullHandler):
        async def on_text(self, text):
            fired.append(text)

    await kernel.run("x", KernelSpec(model="gpt-5.3-codex"), handlers=[H()])
    assert fired == ["hi"]
    assert cap.events == []


def test_pi_kernel_argv_normalizes_model_and_threads_thinking() -> None:
    """Unit test on the argv builder — no subprocess. Operator can
    write ``model: gpt-5.3-codex`` (bare) or
    ``openai-codex/gpt-5.3-codex``; both resolve to the explicit
    provider/model form pi expects."""
    cap = CapturingEmitter()
    kernel = PiKernel(cap)

    bare = kernel._build_argv("hi", KernelSpec(model="gpt-5.3-codex"))
    assert "openai-codex/gpt-5.3-codex" in bare

    explicit = kernel._build_argv("hi", KernelSpec(model="openai-codex/gpt-5.3-codex"))
    # Already prefixed — leave untouched.
    assert "openai-codex/gpt-5.3-codex" in explicit
    assert "openai-codex/openai-codex/gpt-5.3-codex" not in explicit

    with_thinking = kernel._build_argv(
        "hi", KernelSpec(model="gpt-5.3-codex", thinking="medium")
    )
    assert "--thinking" in with_thinking
    idx = with_thinking.index("--thinking")
    assert with_thinking[idx + 1] == "medium"

    no_thinking = kernel._build_argv("hi", KernelSpec(model="gpt-5.3-codex"))
    assert "--thinking" in no_thinking
    idx = no_thinking.index("--thinking")
    assert no_thinking[idx + 1] == "off"


def test_pi_kernel_argv_silently_drops_add_dirs() -> None:
    """add_dirs is Anthropic-SDK shape; pi has no equivalent flag.
    The argv builder must not pass ``--add-dir`` (pi exits 1 on
    unknown flags). Skill bodies referencing absolute mind paths
    still resolve because pi's tools default to filesystem-wide
    read access from the running user account."""
    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    spec = KernelSpec(
        model="gpt-5.3-codex",
        add_dirs=[pathlib.Path("/home/alice/alice-mind"), pathlib.Path("/extra")],
    )
    argv = kernel._build_argv("hi", spec)
    assert "--add-dir" not in argv


def test_pi_kernel_argv_passes_skill_path_when_cwd_has_skills(tmp_path) -> None:
    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    skills = tmp_path / ".claude" / "skills" / "log-meal"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: log-meal\ndescription: x\n---\n")

    spec = KernelSpec(model="gpt-5.3-codex", cwd=tmp_path)
    argv = kernel._build_argv("hi", spec)
    assert "--skill" in argv
    idx = argv.index("--skill")
    assert argv[idx + 1] == str(tmp_path / ".claude" / "skills")
    # And --no-skills is set so directory-based discovery doesn't
    # double-up with the explicit path.
    assert "--no-skills" in argv


def test_pi_kernel_argv_translates_tool_names_to_lowercase() -> None:
    """Pi's built-in tools are lowercase (bash, read, write, edit,
    grep, find, ls); Alice passes Claude-Code-style capitalized
    names (Bash, Read, ...). The argv builder must translate so pi
    actually recognizes the allowlist; otherwise pi ends up with
    zero tools and the agent reports 'no file/tool access'."""
    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    spec = KernelSpec(
        model="gpt-5.3-codex",
        allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
    )
    argv = kernel._build_argv("hi", spec)
    idx = argv.index("--tools")
    tools = argv[idx + 1].split(",")
    # Glob → find; everything else lowercases.
    assert tools == ["bash", "read", "write", "edit", "find", "grep"]


def test_pi_kernel_argv_maps_send_message_mcp_tool_to_extension() -> None:
    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    spec = KernelSpec(
        model="gpt-5.3-codex",
        allowed_tools=["Bash", "mcp__alice__send_message", "mcp__alice__read_memory"],
    )
    argv = kernel._build_argv("hi", spec)
    idx = argv.index("--tools")
    assert argv[idx + 1] == "bash,send_message"
    assert "--extension" in argv
    ext = argv[argv.index("--extension") + 1]
    assert ext.endswith("kernels/pi/extensions/send-message.js")


@pytest.mark.asyncio
async def test_pi_kernel_emits_drop_event_for_mcp_servers(fake_pi_env) -> None:
    """KernelSpec.mcp_servers is Anthropic-only — pi has no MCP
    client. PiKernel must surface a ``pi_spec_field_dropped`` event
    when a caller actually populates it, so the next operator who
    wires an MCP tool into pi notices the drop instead of hunting a
    silent failure for days."""
    fake_pi_env(
        [
            '{"type":"session","version":3,"id":"s","cwd":"/tmp"}',
            '{"type":"agent_end","messages":[]}',
        ]
    )
    cap = CapturingEmitter()
    kernel = PiKernel(cap, correlation_id="t-drop")

    await kernel.run(
        "hi",
        KernelSpec(
            model="gpt-5.3-codex",
            mcp_servers={"alice": {"command": "node", "args": ["server.js"]}},
        ),
    )

    drops = [e for e in cap.events if e["event"] == "pi_spec_field_dropped"]
    assert len(drops) == 1
    assert drops[0]["field"] == "mcp_servers"
    # Summary identifies the value shape without dumping the whole
    # config — collection types report length.
    assert drops[0]["value_summary"] == "dict(len=1)"
    # Correlation id threads through like every other PiKernel event.
    assert drops[0]["turn_id"] == "t-drop"


@pytest.mark.asyncio
async def test_pi_kernel_emits_drop_event_for_hooks(fake_pi_env) -> None:
    fake_pi_env(
        [
            '{"type":"session","version":3,"id":"s","cwd":"/tmp"}',
            '{"type":"agent_end","messages":[]}',
        ]
    )
    cap = CapturingEmitter()
    kernel = PiKernel(cap)

    await kernel.run(
        "hi",
        KernelSpec(
            model="gpt-5.3-codex",
            hooks={"PreToolUse": [object(), object()]},
        ),
    )

    drops = [e for e in cap.events if e["event"] == "pi_spec_field_dropped"]
    assert len(drops) == 1
    assert drops[0]["field"] == "hooks"
    assert drops[0]["value_summary"] == "dict(len=1)"


@pytest.mark.asyncio
async def test_pi_kernel_emits_one_drop_event_per_populated_field(
    fake_pi_env,
) -> None:
    """Both Anthropic-only fields populated → one event per field,
    not a merged event. Lets ``grep pi_spec_field_dropped`` give the
    operator a per-field hit count."""
    fake_pi_env(
        [
            '{"type":"session","version":3,"id":"s","cwd":"/tmp"}',
            '{"type":"agent_end","messages":[]}',
        ]
    )
    cap = CapturingEmitter()
    kernel = PiKernel(cap)

    await kernel.run(
        "hi",
        KernelSpec(
            model="gpt-5.3-codex",
            mcp_servers={"alice": {}},
            hooks={"PreToolUse": []},
        ),
    )

    dropped_fields = sorted(
        e["field"] for e in cap.events if e["event"] == "pi_spec_field_dropped"
    )
    assert dropped_fields == ["hooks", "mcp_servers"]


@pytest.mark.asyncio
async def test_pi_kernel_does_not_warn_for_none_or_empty_fields(
    fake_pi_env,
) -> None:
    """Empty / None values aren't actually being dropped — the
    operator never set them. Don't spam the event log."""
    fake_pi_env(
        [
            '{"type":"session","version":3,"id":"s","cwd":"/tmp"}',
            '{"type":"agent_end","messages":[]}',
        ]
    )
    cap = CapturingEmitter()
    kernel = PiKernel(cap)

    # All defaults — nothing populated.
    await kernel.run("hi", KernelSpec(model="gpt-5.3-codex"))
    # Empty containers — populated but no content.
    await kernel.run(
        "hi",
        KernelSpec(model="gpt-5.3-codex", mcp_servers={}, hooks={}),
    )

    drops = [e for e in cap.events if e["event"] == "pi_spec_field_dropped"]
    assert drops == []


@pytest.mark.asyncio
async def test_pi_kernel_silent_suppresses_drop_event(fake_pi_env) -> None:
    """Silent kernels (used for internal bootstrap/compaction turns)
    swallow every emit — including the drop warning. The next public
    turn re-runs the check with full event visibility."""
    fake_pi_env(
        [
            '{"type":"session","version":3,"id":"s","cwd":"/tmp"}',
            '{"type":"agent_end","messages":[]}',
        ]
    )
    cap = CapturingEmitter()
    kernel = PiKernel(cap, silent=True)

    await kernel.run(
        "hi",
        KernelSpec(model="gpt-5.3-codex", mcp_servers={"alice": {}}),
    )
    assert cap.events == []


@pytest.mark.asyncio
async def test_pi_kernel_timeout_fires_closeout_turn(
    monkeypatch, tmp_path
) -> None:
    """Wake death-loop fix (2026-10-02): when the main turn trips the
    max_seconds budget, PiKernel must fire ONE close-out turn with its
    own short budget before returning error=timeout. The close-out gives
    the model a chance to persist Step-5 state + an active-thread
    handoff. Without it, 39+ consecutive wakes died silently."""
    from kernels.pi import kernel as _pi_kernel

    # Patch the main _drive to hang (triggers TimeoutError inside the
    # asyncio.timeout block). Record close-out invocation so we assert
    # it fired exactly once after the timeout.
    calls: list[dict] = []

    import asyncio as _asyncio

    async def fake_drive(self, argv, spec, translator, handlers):
        calls.append({"prompt_argv": argv, "max_seconds": spec.max_seconds})
        if len(calls) == 1:
            # First call is the main wake — hang so the outer
            # asyncio.timeout fires.
            await _asyncio.sleep(10)
        # Second call is the close-out — return promptly to simulate
        # the model writing state and exiting.
        return

    monkeypatch.setattr(_pi_kernel.PiKernel, "_drive", fake_drive)
    # Short-circuit the models_staging + unsupported-fields probes.
    monkeypatch.setattr(
        _pi_kernel, "ensure_pi_models_json", lambda emit=None: None
    )

    cap = CapturingEmitter()
    kernel = PiKernel(cap, correlation_id="t-closeout")
    result = await kernel.run(
        "main prompt",
        KernelSpec(model="gpt-5.3-codex", max_seconds=1),
    )

    # One main call + one close-out call.
    assert len(calls) == 2
    # Close-out budget is bounded (~120s per module constant).
    assert calls[1]["max_seconds"] == _pi_kernel._CLOSEOUT_MAX_SECONDS
    # Prompt argv for the close-out carries the handoff instructions.
    joined = " ".join(str(x) for x in calls[1]["prompt_argv"])
    assert "close-out" in joined
    assert "active-thread.md" in joined
    # Timeout + closeout_ok events both surfaced; result still signals
    # the main-wake timeout so the caller can log + propagate.
    event_names = [e["event"] for e in cap.events]
    assert "timeout" in event_names
    assert "closeout_ok" in event_names
    assert result.error == "timeout"
    assert result.is_error is True


@pytest.mark.asyncio
async def test_pi_kernel_closeout_timeout_still_returns_timeout_result(
    monkeypatch, tmp_path
) -> None:
    """If the close-out turn ALSO times out, PiKernel must still return
    error=timeout cleanly — a wedged close-out can't leak into the
    outer error path."""
    from kernels.pi import kernel as _pi_kernel
    import asyncio as _asyncio

    async def hanging_drive(self, argv, spec, translator, handlers):
        # Both main AND close-out hang.
        await _asyncio.sleep(10)

    monkeypatch.setattr(_pi_kernel.PiKernel, "_drive", hanging_drive)
    monkeypatch.setattr(
        _pi_kernel, "ensure_pi_models_json", lambda emit=None: None
    )
    # Shorten the close-out budget to keep the test fast.
    monkeypatch.setattr(_pi_kernel, "_CLOSEOUT_MAX_SECONDS", 1)

    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    result = await kernel.run(
        "main prompt",
        KernelSpec(model="gpt-5.3-codex", max_seconds=1),
    )

    assert result.error == "timeout"
    assert result.is_error is True
    event_names = [e["event"] for e in cap.events]
    assert "timeout" in event_names
    assert "closeout_timeout" in event_names


def test_pi_kernel_argv_drops_tools_without_pi_equivalent() -> None:
    """WebFetch and WebSearch have no pi built-in; they drop. If the
    operator's allowlist is ALL drops, --tools is omitted so pi falls
    back to its default tool set."""
    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    spec = KernelSpec(
        model="gpt-5.3-codex",
        allowed_tools=["WebFetch", "WebSearch"],
    )
    argv = kernel._build_argv("hi", spec)
    assert "--tools" not in argv

    spec_mixed = KernelSpec(
        model="gpt-5.3-codex",
        allowed_tools=["Bash", "WebFetch", "Read"],
    )
    argv_mixed = kernel._build_argv("hi", spec_mixed)
    idx = argv_mixed.index("--tools")
    assert argv_mixed[idx + 1] == "bash,read"


def test_pi_kernel_emits_pi_tool_dropped_event_per_lost_tool() -> None:
    """Visibility fix: WebFetch / WebSearch / unmappable MCP tools
    dropping to pi should fire a ``pi_tool_dropped`` event so the
    Jason-Max-exhausted fallback trace shows exactly which
    capabilities were lost. Previously the drops were silent and the
    missing Web* access only surfaced when the model complained it
    couldn't fetch a URL."""
    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    spec = KernelSpec(
        model="gpt-5.3-codex",
        allowed_tools=["Bash", "WebFetch", "WebSearch", "mcp__alice__some_rpc"],
    )
    kernel._build_argv("hi", spec)
    dropped = [e for e in cap.events if e["event"] == "pi_tool_dropped"]
    tool_names = sorted(e["tool"] for e in dropped)
    assert tool_names == ["WebFetch", "WebSearch", "mcp__alice__some_rpc"]


# -------------------------------------------------------------------- #
# Session resume (added 2026-11-22) — PiKernel honors spec.resume via
# pi's native --session + --session-dir flags so every turn carries the
# full conversation forward. See kernels/pi/kernel.py header for the
# full design note.
# -------------------------------------------------------------------- #


def test_pi_kernel_argv_sets_session_dir_when_resume_absent(
    monkeypatch, tmp_path
) -> None:
    """Even without resume=, PiKernel must point pi at a writable
    session-dir so this turn's JSONL is persisted — the *next* turn's
    resume= then has something to reload."""
    monkeypatch.setenv("ALICE_PI_SESSION_DIR", str(tmp_path / "sessions"))
    cap = CapturingEmitter()
    kernel = PiKernel(cap)

    spec = KernelSpec(model="gpt-5.3-codex")
    argv = kernel._build_argv("hi", spec)

    assert "--no-session" not in argv
    assert "--session-dir" in argv
    idx = argv.index("--session-dir")
    assert argv[idx + 1] == str(tmp_path / "sessions")
    # Fresh turn: no --session <id>.
    assert "--session" not in argv


def test_pi_kernel_argv_passes_session_id_when_resume_file_exists(
    monkeypatch, tmp_path
) -> None:
    """resume= set AND a matching JSONL exists → pass --session <id>
    so pi replays the prior turns before the new prompt."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_id = "01abc123-dead-beef-cafe-0123456789ab"
    # Pi stores files as <timestamp>_<uuid>.jsonl; drop one with
    # arbitrary timestamp — we only match on the uuid suffix.
    (session_dir / f"2026-11-22T00-00-00-000Z_{session_id}.jsonl").write_text("{}\n")
    monkeypatch.setenv("ALICE_PI_SESSION_DIR", str(session_dir))

    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    spec = KernelSpec(model="gpt-5.3-codex", resume=session_id)
    argv = kernel._build_argv("hi", spec)

    assert "--session-dir" in argv
    assert "--session" in argv
    idx = argv.index("--session")
    assert argv[idx + 1] == session_id


def test_pi_kernel_argv_resume_file_missing_emits_event_and_omits_session(
    monkeypatch, tmp_path
) -> None:
    """resume= set but no matching file → fresh pi session (don't
    pass --session, since pi would exit 1 on missing session) AND
    emit ``pi_resume_file_missing`` so operators can grep for the
    continuity gap."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setenv("ALICE_PI_SESSION_DIR", str(session_dir))

    cap = CapturingEmitter()
    kernel = PiKernel(cap, correlation_id="turn-xyz")
    spec = KernelSpec(model="gpt-5.3-codex", resume="99999999-no-file-here")
    argv = kernel._build_argv("hi", spec)

    # Dir still passed (so this turn persists for future resume).
    assert "--session-dir" in argv
    # But no --session <id> — pi would exit 1 on the missing file.
    assert "--session" not in argv
    missing = [e for e in cap.events if e["event"] == "pi_resume_file_missing"]
    assert len(missing) == 1
    assert missing[0]["session_id"] == "99999999-no-file-here"
    assert missing[0]["turn_id"] == "turn-xyz"


def test_pi_kernel_argv_session_dir_unavailable_falls_back_to_no_session(
    monkeypatch, tmp_path
) -> None:
    """If the session-dir can't be created (read-only mount, perm
    denied), PiKernel must degrade to ephemeral ``--no-session``
    rather than crash the turn — Carolyn's reply still goes out
    even if persistence breaks."""
    # Point at a path whose *parent* is a regular file — mkdir can't
    # create a subdir under it, so the OSError path fires.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("sentinel")
    monkeypatch.setenv(
        "ALICE_PI_SESSION_DIR", str(blocker / "sessions-impossible")
    )

    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    spec = KernelSpec(model="gpt-5.3-codex", resume="any-id")
    argv = kernel._build_argv("hi", spec)

    assert "--no-session" in argv
    assert "--session-dir" not in argv
    assert "--session" not in argv
    unavailable = [
        e for e in cap.events if e["event"] == "pi_session_dir_unavailable"
    ]
    assert len(unavailable) == 1
    assert unavailable[0]["path"].endswith("sessions-impossible")


def test_pi_kernel_argv_empty_resume_string_is_treated_as_none(
    monkeypatch, tmp_path
) -> None:
    """``resume=""`` (empty string) should NOT be passed as --session —
    pi would exit 1 trying to match the empty id. Treat it like
    ``resume=None`` and mint a fresh session."""
    monkeypatch.setenv("ALICE_PI_SESSION_DIR", str(tmp_path / "sessions"))
    cap = CapturingEmitter()
    kernel = PiKernel(cap)
    spec = KernelSpec(model="gpt-5.3-codex", resume="")
    argv = kernel._build_argv("hi", spec)
    assert "--session" not in argv
    # Empty resume isn't a "missing file" — don't noise the event log.
    missing = [e for e in cap.events if e["event"] == "pi_resume_file_missing"]
    assert missing == []


def test_pi_session_file_exists_matches_uuid_suffix(tmp_path) -> None:
    """Pi's JSONL filename shape is ``<timestamp>_<uuid>.jsonl``.
    The helper must match on the uuid suffix regardless of the
    timestamp prefix so operator reindex-by-id lookups work."""
    from kernels.pi.kernel import _pi_session_file_exists

    (tmp_path / "2026-11-22T00-00-00-000Z_match-me.jsonl").write_text("{}\n")
    (tmp_path / "unrelated.txt").write_text("")
    (tmp_path / "match-me-prefix-but-no-underscore.jsonl").write_text("{}\n")

    assert _pi_session_file_exists(str(tmp_path), "match-me") is True
    assert _pi_session_file_exists(str(tmp_path), "match-nothing") is False
    # Empty id always False — guards against pi exit-1 on the empty
    # --session match.
    assert _pi_session_file_exists(str(tmp_path), "") is False
    # Nonexistent dir resolves to False (not raise).
    assert _pi_session_file_exists(str(tmp_path / "nope"), "match-me") is False
