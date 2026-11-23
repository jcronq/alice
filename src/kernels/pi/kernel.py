"""PiKernel — :class:`Kernel` impl backed by ``pi --mode json``.

Subprocess-and-JSONL wrapper around pi-coding-agent (Mario Zechner's
Node binary). Architecturally analogous to ``claude_agent_sdk``'s
own subprocess transport — same shape, different binary, different
event vocabulary.

Auth: pi reads ``~/.pi/agent/auth.json``. The container entrypoint
runs the codex→pi bridge to populate that file from
``~/.codex/auth.json``; PiKernel itself doesn't touch auth.

Models registry: pi reads ``~/.pi/agent/models.json`` for its
provider/model list. Alice's source-of-truth registry lives in
the vault at ``~/alice-mind/config/pi-models.json``. PiKernel.run()
stages vault → pi runtime location via
:func:`kernels.pi.models_staging.ensure_pi_models_json` at the start
of every run. Idempotent (skips when content matches) and
fail-soft (warning event, no raise) — pi can always fall back to
its built-in providers.

Skills: PiKernel passes ``--skill <rendered_dir>`` (the per-hemisphere
ephemeral skills dir from Plan 07 P3). Pi auto-discovery falls back
to ``.claude/skills/`` under cwd as well; we set cwd to the same
dir to be defensive.

Session resume (2026-11-22): PiKernel honors
:attr:`KernelSpec.resume` via pi's native ``--session <id>`` +
``--session-dir <dir>`` mechanism. Every turn persists its JSONL
transcript to :data:`_DEFAULT_SESSION_DIR` (overridable via
``ALICE_PI_SESSION_DIR``); when ``resume=<session_id>`` is passed
and a matching file exists, pi replays the transcript before the
new prompt so the model sees the full conversation. If the file
is missing (first turn of a new session_id) we emit
``pi_resume_file_missing`` and fall back to a fresh pi session —
the transport-level id pi then generates is captured by the
translator's ``session`` event and flows back to speaking so the
*next* turn can resume cleanly. Any filesystem failure on the
session-dir degrades to ephemeral ``--no-session`` with a
``pi_session_dir_unavailable`` event — never crash the turn over
missing persistence.

Pi's auto-compaction is not configured here; grand-Alice's expected
dialogue length (short Signal threads) keeps us well under the
default threshold. If auto-compaction becomes a problem we can add
a pi settings file with ``compaction.enabled: false`` or an
Alice-side cap on transcript length.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from dataclasses import replace as _dc_replace
from importlib import resources
from typing import Any, Optional

from core.events import EventEmitter
from core.kernel import (
    BlockHandler,
    KernelResult,
    KernelSpec,
    ThinkingLevel,
)

from . import transport as _transport_mod
from .models_staging import ensure_pi_models_json
from .transport import stream_pi_events
from .translator import PiEventTranslator


__all__ = ["PiKernel"]


# Close-out budget — when the main wake trips its max_seconds, fire ONE
# more turn with this budget so the model can persist state before the
# process exits (Step-5 summary, active-thread.md handoff, daily note
# line). Keeping it short (~2 min) bounds damage if the close-out itself
# hangs; the outer s6 WAKE_TIMEOUT=3600s remains the ultimate backstop.
_CLOSEOUT_MAX_SECONDS = 120


# Prompt sent to the close-out turn. Deliberately prescriptive — the
# model has very little budget here and shouldn't reason about what to
# do, only execute. See wake death-loop fix (2026-10-02):
# 39+ consecutive wakes were hard-killed at the outer 60-min timeout
# with no state persisted; this converts the hard kill into a graceful
# handoff.
_CLOSEOUT_PROMPT = """\
Your main wake just hit the hard-stop budget and was killed mid-work. \
You have a tight ~120-second close-out budget. Do exactly these steps, \
nothing else:

1. Append a brief "Step 5 — close (interrupted)" summary line to the \
wake file you wrote in Step 1 at \
`~/alice-mind/inner/thoughts/<today-UTC>/*-wake.md` (today's UTC date, \
newest file). Note that the hard-stop fired and what you had just \
finished.

2. Write or update `~/alice-mind/inner/state/active-thread.md` with \
the exact remaining next step so the next wake picks up cleanly. \
Use this format:

   ---
   topic: <one-line topic you were working on>
   last_action: <what you had just completed>
   next_step: <concrete next action>
   created: <ISO8601 UTC timestamp>
   ---

3. Append a single-line entry to today's daily note at \
`~/alice-mind/cortex-memory/dailies/<today-UTC>.md` noting the \
interrupted wake and the handoff.

Then stop. Do not start new work. Do not drain the inbox. Do not groom \
the vault. This is a graceful handoff — persist state and exit.
"""


# Session-dir resolution. Keyed off an env var so tests + alternate
# deployments can point at tmp paths without recompiling. The default
# lives under the Alice-mind bind-mount so JSONL transcripts survive
# container recreate, matching how ``session.json`` + ``cortex-index.db``
# are stored.
_SESSION_DIR_ENV = "ALICE_PI_SESSION_DIR"
_DEFAULT_SESSION_DIR = "/home/alice/alice-mind/inner/state/sessions"


def _resolve_session_dir() -> str:
    """Return the directory where PiKernel stages session JSONL files.

    Env override wins over the bind-mounted default. Both paths resolve
    at argv-build time so a test fixture's ``monkeypatch.setenv`` takes
    effect without a module reload (mirrors the ``ALICE_PI_BIN`` pattern
    in :mod:`kernels.pi.transport`).
    """
    override = os.environ.get(_SESSION_DIR_ENV)
    if override:
        return override
    return _DEFAULT_SESSION_DIR


def _pi_session_file_exists(session_dir: str, session_id: str) -> bool:
    """Return True iff a pi session file matching ``session_id`` exists
    in ``session_dir``.

    Pi stores sessions as ``<ISO-timestamp>_<uuid>.jsonl``. Rather than
    try to recover the timestamp prefix, we scan the directory for any
    file whose name ends with ``_<session_id>.jsonl``. Any IO error
    (missing dir, permission denied, …) resolves to False so the caller
    treats it as "fresh session" instead of crashing the turn.
    """
    if not session_id:
        return False
    try:
        d = pathlib.Path(session_dir)
        if not d.is_dir():
            return False
        suffix = f"_{session_id}.jsonl"
        for entry in d.iterdir():
            # is_file + endswith is enough; we don't need to validate
            # the file contents here — pi re-validates on load and
            # errors out with "No session found matching ..." which
            # our exit-code path surfaces cleanly.
            if entry.is_file() and entry.name.endswith(suffix):
                return True
    except OSError:
        # Treat unreadable session dir as "no resume available"; the
        # fresh-start path still writes a new session file (if the dir
        # is writable later) so continuity recovers on the next turn.
        return False
    return False


def _thinking_to_pi_arg(level: Optional[ThinkingLevel]) -> str:
    if level is None or level == "off":
        return "off"
    return level


def _normalize_pi_model(model: str) -> str:
    """If the operator wrote ``gpt-5.3-codex`` (no provider prefix),
    prepend ``openai-codex/`` since "pi" backend implies the Codex
    subscription provider. Power users can override by writing
    ``<provider>/<model>`` directly in model.yml."""
    if "/" in model:
        return model
    return f"openai-codex/{model}"


# Map Alice's Claude-Code-style tool names to pi-coding-agent's
# lowercase tool names. WebFetch / WebSearch have no pi equivalent
# and get dropped silently (pi extensions can add them later).
_PI_TOOL_NAME_MAP: dict[str, Optional[str]] = {
    "Bash": "bash",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Grep": "grep",
    "Glob": "find",  # closest pi equivalent
    "LS": "ls",
    "Ls": "ls",
    "WebFetch": None,  # not available in pi
    "WebSearch": None,  # not available in pi
    # Alice's speaking outbox is MCP-backed in Claude Code. Pi has no
    # MCP client, so PiKernel loads a tiny native extension with the same
    # tool name and TurnRunner handles the emitted tool call.
    "mcp__alice__send_message": "send_message",
}


# KernelSpec fields PiKernel cannot honor (Anthropic-SDK-only) and
# silently drops at argv-construction time. Listed here so the
# preflight check in :meth:`PiKernel.run` can warn the operator
# whenever a caller actually populates one — masking these on the
# pi backend is exactly the trap that hid the ``run_experiment``
# MCP-tool wiring bug from us for days. New Anthropic-only fields
# added to :class:`KernelSpec` should be appended here.
_PI_UNSUPPORTED_SPEC_FIELDS: tuple[str, ...] = (
    "mcp_servers",
    "hooks",
    "permission_mode",
)


def _summarize_dropped_value(value: Any) -> str:
    """Cheap, log-safe summary of a dropped field's value.

    Goal is "enough to identify what the operator passed" without
    dumping a 50KB hook table or an MCP server config into the event
    log. Collections report their length; everything else reports its
    type name.
    """
    if isinstance(value, (dict, list, tuple, set)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def _translate_tools(
    allowed: list[str],
    *,
    on_drop: Optional[Any] = None,
) -> list[str]:
    """Translate Claude tool names to pi names. Unknown names pass
    through lowercased so custom/extension tools (which the
    operator wrote in their pi-native form) still work. Returns
    [] when every requested tool dropped — caller treats that as
    "fall back to pi's default tool set".

    ``on_drop`` (optional) is called once per Claude-only tool name
    that has no pi equivalent (WebFetch, WebSearch, MCP tools). This
    gives :class:`PiKernel` a hook for emitting a visibility event so
    the Claude-code → pi-mono fallback (e.g. Jason's Max token
    exhausted) doesn't silently lose Web* access with no signal."""
    out: list[str] = []
    for name in allowed:
        if name in _PI_TOOL_NAME_MAP:
            mapped = _PI_TOOL_NAME_MAP[name]
            if mapped is not None:
                out.append(mapped)
            elif on_drop is not None:
                on_drop(name)
            continue
        if name.startswith("mcp__"):
            # MCP-backed tools without a direct name map silently drop
            # — pi has no MCP client. Report once so the operator can
            # spot a missing capability in the pi-mode trace.
            if on_drop is not None:
                on_drop(name)
            continue
        # Unknown name: lowercased pass-through. Either it's already
        # a pi-native name, or pi will reject it and the operator
        # gets a clear error.
        out.append(name.lower())
    return out


class PiKernel:
    """Drive one pi session to completion. Implements :class:`Kernel`."""

    def __init__(
        self,
        emitter: EventEmitter,
        *,
        correlation_id: Optional[str] = None,
        silent: bool = False,
        short_cap: int = 2000,
    ) -> None:
        self.emitter = emitter
        self.correlation_id = correlation_id
        self.silent = silent
        self._cap = short_cap

    def _emit(self, event: str, **fields: Any) -> None:
        if self.silent:
            return
        if self.correlation_id is not None:
            fields.setdefault("turn_id", self.correlation_id)
        self.emitter.emit(event, **fields)

    def _warn_unsupported_fields(self, spec: KernelSpec) -> None:
        """Emit ``pi_spec_field_dropped`` once per Anthropic-only field
        the caller populated.

        PiKernel silently ignores fields like ``mcp_servers`` and
        ``hooks`` because pi has no equivalent — but "silently" is
        the trap. The ``run_experiment`` MCP tool was wired into
        ``mcp_servers``, PiKernel ignored it, no signal surfaced, and
        thinking failed to call the tool for days. One event per
        populated drop-field gives the next victim something to grep
        for. See also :data:`_PI_UNSUPPORTED_SPEC_FIELDS`.
        """
        for field_name in _PI_UNSUPPORTED_SPEC_FIELDS:
            value = getattr(spec, field_name, None)
            if value is None or value == {} or value == [] or value == ():
                continue
            self._emit(
                "pi_spec_field_dropped",
                field=field_name,
                value_summary=_summarize_dropped_value(value),
            )

    async def run(
        self,
        prompt: str,
        spec: KernelSpec,
        handlers: Optional[list[BlockHandler]] = None,
    ) -> KernelResult:
        # Warn loudly about KernelSpec fields PiKernel cannot honor.
        # Must fire BEFORE argv translation (and before the model
        # registry stage) so an operator who passed e.g. an MCP tool
        # config sees the drop event even if a subsequent stage
        # raises.
        self._warn_unsupported_fields(spec)
        # Stage the alice-managed pi model registry before pi reads
        # ``~/.pi/agent/models.json``. Idempotent + fail-soft — never
        # blocks the run, just emits a warning event on failure.
        ensure_pi_models_json(emit=self._emit)

        handlers = list(handlers or [])
        argv = self._build_argv(prompt, spec)
        translator = PiEventTranslator(self._emit, short_cap=self._cap)

        try:
            if spec.max_seconds and spec.max_seconds > 0:
                async with asyncio.timeout(spec.max_seconds):
                    await self._drive(argv, spec, translator, handlers)
            else:
                await self._drive(argv, spec, translator, handlers)
        except asyncio.TimeoutError:
            self._emit("timeout", max_seconds=spec.max_seconds)
            # Convert the hard kill into a graceful handoff: fire ONE
            # short turn so the model can persist Step-5 + active-thread
            # state before the process exits. The main translator's
            # partial state is preserved — the close-out writes files
            # only, no kernel-result contamination.
            await self._run_closeout(spec)
            return translator.to_kernel_result(error="timeout", is_error=True)

        return translator.to_kernel_result()

    async def _run_closeout(self, spec: KernelSpec) -> None:
        """Fire a short close-out turn after the main wake times out.

        Uses a fresh translator (silent — we don't need the output) so
        the main wake's partial state survives untouched for the caller.
        Any failure here (second timeout, pi subprocess exits non-zero,
        unexpected event-stream error) is swallowed and logged — the
        caller still gets ``error="timeout"`` and the outer s6
        ``WAKE_TIMEOUT`` is the ultimate backstop.
        """
        closeout_spec = _dc_replace(spec, max_seconds=_CLOSEOUT_MAX_SECONDS)
        closeout_argv = self._build_argv(_CLOSEOUT_PROMPT, closeout_spec)
        # Silent translator — close-out output isn't surfaced to handlers
        # or the final KernelResult; it exists only to drive the pi
        # subprocess side effects (file writes) to completion.
        closeout_translator = PiEventTranslator(
            (lambda *a, **k: None), short_cap=self._cap
        )
        try:
            async with asyncio.timeout(_CLOSEOUT_MAX_SECONDS):
                await self._drive(
                    closeout_argv, closeout_spec, closeout_translator, []
                )
            self._emit("closeout_ok", max_seconds=_CLOSEOUT_MAX_SECONDS)
        except asyncio.TimeoutError:
            self._emit("closeout_timeout", max_seconds=_CLOSEOUT_MAX_SECONDS)
        except Exception as exc:  # noqa: BLE001
            self._emit(
                "closeout_exception",
                type=type(exc).__name__,
                message=str(exc),
            )

    async def _drive(
        self,
        argv: list[str],
        spec: KernelSpec,
        translator: PiEventTranslator,
        handlers: list[BlockHandler],
    ) -> None:
        cwd = str(spec.cwd) if spec.cwd is not None else None
        async for event in stream_pi_events(argv, cwd=cwd):
            await translator.handle(event, handlers)

    def _build_argv(self, prompt: str, spec: KernelSpec) -> list[str]:
        # Read PI_BIN dynamically so test fixtures + runtime env
        # changes (ALICE_PI_BIN) take effect without needing a
        # module reload of kernels.pi.kernel.
        argv: list[str] = [
            _transport_mod.pi_bin(),
            "--mode",
            "json",
            "-p",
            prompt,
            "--no-skills",  # disable directory-based discovery
        ]
        # Session persistence + resume. Pi natively stores one JSONL
        # per session under ``--session-dir``; ``--session <id>`` loads
        # a prior one. We pre-check for the file because pi exits 1
        # when the id doesn't match — the "fresh session" path has to
        # omit --session entirely so pi can mint a new uuid.
        session_dir = self._stage_session_dir()
        if session_dir is None:
            # Can't persist → keep today's ephemeral behavior. No
            # resume, no history saved. The daemon still passes
            # resume= on subsequent turns; without a dir we just
            # ignore it (and the drop event tells the operator why).
            argv.append("--no-session")
        else:
            argv.extend(["--session-dir", session_dir])
            resume_id = (spec.resume or "").strip()
            if resume_id:
                if _pi_session_file_exists(session_dir, resume_id):
                    argv.extend(["--session", resume_id])
                else:
                    # First turn of a new session_id (or the JSONL
                    # was purged). Fresh session; translator captures
                    # the new id for the next turn's resume=.
                    self._emit(
                        "pi_resume_file_missing",
                        session_id=resume_id,
                        session_dir=session_dir,
                    )
        # Skill discovery: explicit --skill <rendered_dir> beats
        # pi's auto-discovery from cwd's .claude/skills (which would
        # find the same files, but being explicit avoids surprises
        # if cwd ever drifts from skills_cwd).
        if spec.cwd is not None:
            skills_dir = spec.cwd / ".claude" / "skills"
            if skills_dir.is_dir():
                argv.extend(["--skill", str(skills_dir)])

        # Translate Alice's Claude-Code-style tool names ("Bash",
        # "Read", ...) to pi's lowercase set ("bash", "read", ...).
        # If translation drops every name (e.g. all-WebFetch list)
        # don't pass --tools at all — that lets pi default to its
        # full built-in set rather than running with zero tools.
        # ``on_drop`` surfaces every Claude-only tool that has no pi
        # equivalent so the fallback trace shows exactly which
        # capabilities were lost — the Jason-Max-exhausted fallback
        # path used to drop WebFetch/WebSearch with no event.
        def _on_tool_drop(name: str) -> None:
            self._emit("pi_tool_dropped", tool=name)

        translated = _translate_tools(
            spec.allowed_tools or [], on_drop=_on_tool_drop
        )
        if "send_message" in translated:
            argv.extend(["--extension", _send_message_extension_path()])
        if translated:
            argv.extend(["--tools", ",".join(translated)])

        argv.extend(["--model", _normalize_pi_model(spec.model)])
        argv.extend(["--thinking", _thinking_to_pi_arg(spec.thinking)])

        if spec.append_system_prompt:
            argv.extend(["--append-system-prompt", spec.append_system_prompt])

        # add_dirs: silently ignored. Anthropic's claude_agent_sdk
        # uses ``add_dirs`` to grant the agent extra read access
        # beyond cwd, but pi has no equivalent flag — its tools
        # default to whole-filesystem read access from the user
        # account, so skill bodies referencing absolute paths
        # (e.g. ~/alice-mind/...) still resolve via Read/Bash.
        # mcp_servers + hooks (Anthropic-only): also dropped, but
        # NOT silently — PiKernel.run emits one
        # ``pi_spec_field_dropped`` event per populated field so the
        # operator notices the trap (see
        # :meth:`_warn_unsupported_fields` and
        # :data:`_PI_UNSUPPORTED_SPEC_FIELDS`).
        return argv

    def _stage_session_dir(self) -> Optional[str]:
        """Ensure the session JSONL directory exists and is writable.

        Returns the resolved dir path on success, or ``None`` on any
        filesystem failure (permission denied, read-only mount, …).
        Emits ``pi_session_dir_unavailable`` on failure so operators
        can grep for continuity drops. Callers fall back to
        ``--no-session`` when ``None`` is returned — we never crash
        a turn over missing persistence.
        """
        session_dir = _resolve_session_dir()
        try:
            pathlib.Path(session_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._emit(
                "pi_session_dir_unavailable",
                path=session_dir,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        return session_dir


def _send_message_extension_path() -> str:
    return str(resources.files("kernels.pi").joinpath("extensions", "send-message.js"))
