"""Per-turn orchestration: SDK invocation + bootstrap preamble + retry.

Plan 01 Phase 6c finishing pass extracts the kernel-call machinery
out of ``SpeakingDaemon`` so the daemon's body reads as lifecycle
orchestration only.

What this owns:

- Session identity (``session_id``) — Layer 1 of the context
  persistence design. The daemon proxies ``session_id`` through to
  this runner so callers like :class:`CompactionTrigger.run`
  (reaching via ``ctx``) write through transparently.
- One-shot bootstrap preamble (``_pending_preamble``) — Layer 2.
  Primed by :meth:`prime_bootstrap_preamble` when no warm session
  is available; consumed by :meth:`compose_prompt` on the next
  turn and cleared.
- The ``run_turn`` method itself: build kernel spec + handlers,
  call :class:`AgentKernel`, recover from a Layer-1 resume failure
  by clearing ``session_id`` + priming Layer 2 + retrying once.

What it borrows (callables, not mutated):

- ``turn_did_send_getter`` — read whether the in-flight turn has
  called ``send_message`` yet (the daemon's ``_send_message``
  flips that flag from False to True). Used to emit
  ``missed_reply`` events.
- ``current_reply_channel_getter`` — the per-turn reply channel
  the handlers set on entry. Threaded into
  :class:`CLITraceHandler` so the CLI trace events fire on the
  right connection.

What it doesn't touch:

- ``_turn_last_outbound`` / ``_current_*`` per-turn state stays on
  the daemon — set by handlers in ``_dispatch`` and read by
  ``_send_message``. Phase 6c keeps that boundary intact rather
  than threading getters/setters everywhere.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from alice_core.kernel import AgentKernel, KernelSpec
from alice_core.sdk_compat import looks_like_missing_session as _looks_like_missing_session

from .domain import session_state
from .domain.turn_log import TurnLog
from .infra.config import Config
from .infra.events import EventLogger
from .pipeline import compaction as compaction_module
from .pipeline.handlers import CLITraceHandler, CompactionArmer, SessionHandler
from .transports import ChannelRef


log = logging.getLogger(__name__)


# Same as the design doc: 5 verbatim turns bridge the gap between
# the compaction-summary cutoff and "now".
SUMMARY_TAIL_TURNS = 5


# Builtin Anthropic tools the kernel always allows. MCP-supplied
# tools (Alice's send_message, resolve_surface, etc.) get appended
# at spec-build time.
_BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"]


class TurnRunner:
    """Owns session identity + bootstrap preamble + the kernel call.

    Constructed once in :class:`SpeakingDaemon.__init__`. The daemon
    proxies ``session_id`` through to this object so handlers and
    the compaction trigger keep their existing ``ctx.session_id``
    access unchanged.
    """

    def __init__(
        self,
        *,
        cfg: Config,
        events: EventLogger,
        turns: TurnLog,
        mcp_servers: dict,
        custom_tool_names: list[str],
        session_path: Any,
        summary_path: Any,
        compaction: compaction_module.CompactionTrigger,
        cli_transport: Any = None,
        # Borrowed callables — see module docstring.
        turn_did_send_getter: Callable[[], bool],
        current_reply_channel_getter: Callable[[], Optional[ChannelRef]],
        system_prompt: Optional[str] = None,
    ) -> None:
        self._cfg = cfg
        self._events = events
        self._turns = turns
        self._mcp_servers = mcp_servers
        self._custom_tool_names = custom_tool_names
        self._session_path = session_path
        self._summary_path = summary_path
        self._compaction = compaction
        self._cli_transport = cli_transport
        self._turn_did_send_getter = turn_did_send_getter
        self._current_reply_channel_getter = current_reply_channel_getter
        # Plan 05 Phase 3: persona system-prompt fragment, rendered
        # once by the daemon factory and threaded into every kernel
        # call via KernelSpec.append_system_prompt. None keeps today's
        # behaviour (no system prompt injected).
        self._system_prompt = system_prompt
        self.session_id: Optional[str] = None
        self._pending_preamble: Optional[str] = None

    # ------------------------------------------------------------------
    # Bootstrap preamble (Layer 2)

    def prime_bootstrap_preamble(self) -> None:
        """When we have no warm session, prime the next turn with a
        turn_log-derived preamble (+ compaction summary if present).

        Three cases this covers:

        - Daemon start with no ``session.json`` — bootstrap from
          turn_log.
        - ``session.json`` pointed at a stale SDK session — same
          bootstrap.
        - Just rolled the session after compaction — inject summary
          + tail. (The caller also clears ``session_id`` before
          priming.)

        Empty preamble means "first boot, no turn history" — start
        fresh, no preamble.
        """
        if self.session_id is not None:
            return
        summary = compaction_module.read_summary_if_any(self._summary_path)
        if summary:
            tail = self._turns.tail(SUMMARY_TAIL_TURNS)
            self._pending_preamble = compaction_module.build_summary_preamble(
                summary, tail
            )
            self._events.emit(
                "context_bootstrap",
                source="summary",
                tail_len=len(tail),
            )
            log.info("primed bootstrap preamble from context summary")
            return

        bootstrap_turns = int(
            self._cfg.speaking.get("context_bootstrap_turns", 20)
        )
        tail = self._turns.tail(bootstrap_turns)
        preamble = compaction_module.build_bootstrap_preamble(tail)
        if preamble:
            self._pending_preamble = preamble
            self._events.emit(
                "context_bootstrap",
                source="turn_log",
                tail_len=len(tail),
            )
            log.info(
                "primed bootstrap preamble from turn_log (%d turns)", len(tail)
            )

    def compose_prompt(self, prompt: str) -> str:
        """Prepend the one-shot bootstrap preamble if one is pending."""
        if not self._pending_preamble:
            return prompt
        composed = f"{self._pending_preamble}\n\n{prompt}"
        self._pending_preamble = None
        return composed

    # ------------------------------------------------------------------
    # Kernel spec + handlers

    def _build_spec(self) -> KernelSpec:
        return KernelSpec(
            model=self._cfg.speaking.get("model"),
            allowed_tools=_BUILTIN_TOOLS + self._custom_tool_names,
            mcp_servers=self._mcp_servers,
            cwd=self._cfg.work_dir,
            resume=self.session_id,
            # Adaptive thinking with summarized display so
            # ThinkingBlocks come back with non-empty text. Without
            # display='summarized' the SDK omits thinking text
            # entirely (signature only) and the viewer's trace shows
            # empty (thought) rows.
            thinking={"type": "adaptive", "display": "summarized"},
            append_system_prompt=self._system_prompt,
        )

    def _build_handlers(self, *, silent: bool) -> list:
        """Compose the per-turn kernel handlers.

        Silent turns still need ``session_id`` tracked (so the next
        turn passes ``resume=``) but skip the ``session.json`` write
        and the compaction arming — those are production-turn
        concerns only.
        """

        def _set_session_id(sid: str) -> None:
            self.session_id = sid

        def _arm_compaction() -> None:
            self._compaction.arm()

        handlers: list = [
            SessionHandler(
                session_path=self._session_path,
                set_session_id=_set_session_id,
                persist=not silent,
            ),
        ]
        if not silent:
            threshold = int(
                self._cfg.speaking.get(
                    "context_compaction_threshold",
                    compaction_module.DEFAULT_THRESHOLD,
                )
            )
            handlers.append(
                CompactionArmer(threshold=threshold, arm=_arm_compaction)
            )
        # CLI trace pass-through. No-op when the active channel isn't
        # CLI, so safe to install for every turn — signal/discord/
        # surface turns silently skip. Installed for silent turns too:
        # bootstrap/compaction trace events would just have no
        # listener.
        if self._cli_transport is not None:
            handlers.append(
                CLITraceHandler(
                    transport=self._cli_transport,
                    get_channel=self._current_reply_channel_getter,
                )
            )
        return handlers

    # ------------------------------------------------------------------
    # Run

    async def run_turn(
        self,
        prompt: str,
        *,
        turn_id: str,
        outbound_recipient: Optional[str],
        silent: bool = False,
    ) -> str:
        """Execute one SDK turn through the agent kernel.

        ``silent=True`` marks the turn as internal (bootstrap or
        compaction) — no missed_reply event, no usage-threshold
        check, no session.json flap. ``outbound_recipient`` is
        informational for the missed_reply event.

        On Layer 1 failure (``resume=`` points at a session the SDK
        no longer has) we clear ``session_id``, prime the Layer 2
        bootstrap preamble, and transparently retry the same prompt
        with a fresh session.

        Returns the concatenated assistant text (useful for
        compaction turns which consume the summary).
        """
        final_prompt = self.compose_prompt(prompt)
        spec = self._build_spec()
        handlers = self._build_handlers(silent=silent)

        kernel = AgentKernel(
            self._events,
            correlation_id=turn_id,
            silent=silent,
            # Generous so Opus's reasoning + replies aren't sliced
            # mid-sentence in the modal trace. Logs grow ~2x on busy
            # days but disk is cheap and the viewer's value depends
            # on this.
            short_cap=4000,
        )

        try:
            result = await kernel.run(final_prompt, spec, handlers=handlers)
        except Exception as exc:  # noqa: BLE001
            # Layer 1 failure recovery: stale resume= → drop session
            # state, prime Layer 2, retry once with a fresh session.
            if self.session_id and _looks_like_missing_session(exc):
                self._events.emit(
                    "session_resume_failed",
                    turn_id=turn_id,
                    session_id=self.session_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                log.warning(
                    "resume=%s failed (%s); retrying with fresh session",
                    self.session_id,
                    type(exc).__name__,
                )
                self.session_id = None
                session_state.clear(self._session_path)
                self.prime_bootstrap_preamble()
                retry_prompt = self.compose_prompt(prompt)
                retry_spec = self._build_spec()
                retry_handlers = self._build_handlers(silent=silent)
                result = await kernel.run(
                    retry_prompt, retry_spec, handlers=retry_handlers
                )
            else:
                raise

        if result.is_error or result.error:
            # Kernel returned an error result (timeout, etc.). Callers
            # see an empty / partial text; the kernel already emitted
            # the specific error event.
            pass

        # Missed-reply observability: only meaningful when the turn
        # was supposed to be able to reach a user and Alice skipped
        # it.
        if not silent and not self._turn_did_send_getter():
            self._events.emit(
                "missed_reply",
                turn_id=turn_id,
                outbound_recipient=outbound_recipient,
                session_id=result.session_id,
            )

        return result.text


__all__ = ["SUMMARY_TAIL_TURNS", "TurnRunner"]
