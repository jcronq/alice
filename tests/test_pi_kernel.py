"""End-to-end-ish tests for alice_pi.kernel.PiKernel.

Uses a tmp shell script as the ``pi`` binary that emits a recorded
JSONL stream — exercises argv construction, JSONL parsing, event
translation, and KernelResult assembly without spinning up real pi.
"""

from __future__ import annotations

import os
import pathlib
import stat
import textwrap

import pytest

from alice_core.events import CapturingEmitter
from alice_core.kernel import KernelSpec, NullHandler, TurnSummary

from alice_pi.kernel import PiKernel


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
async def test_pi_kernel_silent_suppresses_events_but_runs_handlers(fake_pi_env) -> None:
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

    explicit = kernel._build_argv(
        "hi", KernelSpec(model="openai-codex/gpt-5.3-codex")
    )
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


def test_pi_kernel_argv_drops_tools_without_pi_equivalent() -> None:
    """WebFetch and WebSearch have no pi built-in; they drop
    silently. If the operator's allowlist is ALL drops, --tools is
    omitted so pi falls back to its default tool set."""
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
