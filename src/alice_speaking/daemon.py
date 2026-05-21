"""Speaking Alice's outer loop.

Three producers feed one serial consumer:
- signal_client.receive(): user envelopes from Signal
- surface_watcher: files that thinking Alice drops into inner/surface/
- emergency_watcher: files that external monitors drop into inner/emergency/

The consumer processes one event at a time — Alice is a single mind juggling
messages and surfaced thoughts, not a parallel worker pool.

Context persistence (v3, see
cortex-memory/reference/design-unified-context-compaction.md):

- Layer 1: session_id is persisted to ``inner/state/session.json`` after
  every ResultMessage. On startup the daemon reads it back and passes
  ``resume=`` on the first turn, so the daemon wakes warm after a
  restart.
- Layer 2: if session.json is missing / corrupt, or the SDK session
  JSONL has been deleted, or resume= fails at runtime, the daemon falls
  back to a silent bootstrap turn that injects render_for_prompt of the
  recent turn_log. That turn's session_id becomes the active session.
- Compaction: after each turn, if effective context tokens (input +
  cache_read + cache_creation) exceed
  ``cfg.speaking["context_compaction_threshold"]``, a flag is set. The
  consumer runs a silent compaction turn before the next event, writes
  a 4-part summary to ``inner/state/context-summary.md``, rolls the
  session, and injects the summary + tail(5) turns on the next turn.

Outbox (v3):

- The daemon no longer auto-sends the final assistant text. Alice must
  call the ``send_message`` tool explicitly. A turn that closes without
  any send_message call logs a ``missed_reply`` event for auditing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal as _signal
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .transports.a2a import A2ATransport
    from .transports.discord import DiscordTransport

from core.config.auth import ensure_auth_env
from core.kernel import KernelSpec, make_kernel
from claude_agent_sdk import HookMatcher
from . import _dispatch as _dispatch_module
from . import auto_fix as auto_fix_module
from . import factory as factory_module
from . import tools as tools_module
from .domain import principals as principals_module
from .domain import session_state
from .domain.principals import AddressBook
from .domain.turn_log import TurnLog
from .infra import config as config_module
from .infra.config import Config
from .infra.events import EventLogger
from .infra.signal_rpc import SignalRPC as SignalClient
from .internal import (
    BackgroundTaskCompleteEvent,
    BackgroundTaskCompletionSource,
    CozyHemEventSubscriber,
    EmergencyEvent,
    EmergencyWatcher,
    SurfaceEvent,
    SurfaceWatcher,
)
from .pipeline import compaction as compaction_module
from .pipeline.dedup import DedupStore
from .pipeline.outbox import OutboxRouter
from .pipeline.quiet_hours import QuietQueue, is_quiet_hours
from .pipeline.quiet_queue_runner import QuietQueueRunner
from .tools.messaging import SELF_RECIPIENT, ResolvedRecipient
from .transports import (
    CLITransport,
    ChannelRef,
    SignalTransport,
    ViewerChatTransport,
)

# DiscordTransport is imported lazily below, only when the daemon is actually
# configured to use Discord. Module-top ``import discord`` in transports.discord
# would otherwise crash the daemon at import time when discord.py isn't
# installed (e.g. stale worker image after a Dockerfile bump).
# Per-transport event dataclasses live next to their transports
# (transport events: Plan 01 Phase 2; SurfaceEvent / EmergencyEvent:
# Phase 3). Daemon no longer touches them directly — the registry
# routes by ``type(event)``. These re-imports stay so existing
# external callers (tests, the viewer's narrative dump) keep their
# ``from alice_speaking.daemon import …Event`` paths working.
from .diagnostics import ContextProbe
from .transports.a2a import A2AEvent
from .transports.cli import CLIEvent
from .transports.discord import DiscordEvent
from .transports.signal import SignalEvent
from .transports.viewer_chat import ViewerChatEvent
from .turn_runner import BUILTIN_TOOLS, TurnRunner


log = logging.getLogger("alice_speaking")


# Public names re-exported from this module for back-compat. The
# event types live in their owning modules (transports/* and
# internal/*) — see the import block above. Listed here so the
# re-exports are intentional, not accidental.
__all__ = [
    "A2AEvent",
    "CLIEvent",
    "DiscordEvent",
    "EmergencyEvent",
    "SignalEvent",
    "SpeakingDaemon",
    "SurfaceEvent",
    "ViewerChatEvent",
]


class SpeakingDaemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Phase 3: AddressBook is the unified ACL + display-name + recipient
        # resolution surface. Loaded from principals.yaml when present;
        # synthesized from ALLOWED_SENDERS + the daemon's own uid as a
        # migration shim when it isn't.
        # Resolve personae once up front — both the address book's
        # CLI fallback (Phase L) and the tool descriptions (Phase I)
        # need it. The factory loader handles missing/malformed
        # personae files; reused here.
        self._personae = factory_module.build_personae(cfg)
        self.address_book: AddressBook = principals_module.load(
            yaml_path=cfg.principals_path,
            fallback_signal_senders=cfg.allowed_senders_fallback,
            fallback_cli_uid=os.getuid(),
            personae=self._personae,
        )
        # Signal is opt-in. Without SIGNAL_ACCOUNT in alice.env we skip the
        # transport entirely and let CLI / Discord (if configured) carry
        # conversation. The daemon still runs.
        self.signal: Optional[SignalClient] = (
            SignalClient(
                api=cfg.signal_api,
                account=cfg.signal_account,
                log_path=cfg.signal_log_path,
                offset_path=cfg.offset_path,
            )
            if cfg.signal_account
            else None
        )
        self.signal_transport: Optional[SignalTransport] = (
            SignalTransport(signal_client=self.signal) if self.signal else None
        )
        self.dedup = DedupStore(cfg.seen_path)
        self.turns = TurnLog(cfg.turn_log_path)
        self.events = EventLogger(cfg.event_log_path)
        self.quiet_queue = QuietQueue(
            cfg.mind_dir / "inner" / "state" / "quiet-queue.jsonl"
        )

        # State directory — used for session.json and context-summary.md.
        self._state_dir = cfg.mind_dir / "inner" / "state"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._session_path = self._state_dir / "session.json"
        self._summary_path = self._state_dir / "context-summary.md"

        # Session identity (Layer 1): pre-populate from disk if
        # present, drop it if the underlying SDK JSONL is gone.
        # Stored on :class:`TurnRunner` (Phase 6c of plan 01); the
        # ``session_id`` property below delegates so handlers and
        # the compaction trigger can keep their existing
        # ``ctx.session_id`` access unchanged.
        # Plan-pi Phase C swapped the kernel cwd from cfg.work_dir to
        # the per-hemisphere rendered skills dir. The Claude Code SDK
        # stores session JSONL files per-cwd, so a session_id created
        # under the old cwd is invalid in the new one. Resolve the
        # ACTUAL kernel cwd here so the preflight checks the right
        # location.
        self._skills_cwd = cfg.state_dir / "alice-skills" / "speaking"

        initial_session_id: Optional[str] = None
        persisted = session_state.read(self._session_path)
        if persisted is not None:
            if session_state.sdk_session_exists(self._skills_cwd, persisted.session_id):
                initial_session_id = persisted.session_id
                log.info(
                    "loaded persisted session %s (saved_at=%s)",
                    persisted.session_id,
                    persisted.saved_at,
                )
            else:
                log.warning(
                    "persisted session %s has no SDK JSONL under %s; "
                    "starting cold (cwd may have changed since persist)",
                    persisted.session_id,
                    self._skills_cwd,
                )
                session_state.clear(self._session_path)

        # MCP tools — build AFTER we know signal/session state because the
        # send_message sender closure needs self.signal plus the did-send
        # tracker below.
        # ``self._personae`` already resolved earlier — pass it into
        # the tool builder so descriptions reflect the agent's name.
        self.mcp_servers, self.custom_tool_names, self.mcp_tool_defs = (
            tools_module.build(
                cfg,
                address_book=self.address_book,
                sender=self._send_message,
                personae=self._personae,
            )
        )

        # Compaction policy + state. Phase 6b of plan 01 replaced
        # the bare ``self._compaction_pending`` flag with a
        # CompactionTrigger that owns ``should_run(event)`` and the
        # actual run orchestration. Both consumers go through it.
        self.compaction = compaction_module.CompactionTrigger()
        # Per-turn did-send tracker. Set back to False at the start of each
        # call to _run_turn(); flipped to True by _send_message when Alice
        # explicitly sends. Used to flag missed_reply events.
        self._turn_did_send: bool = False
        # Per-turn outbound text capture. Set to None at start of each turn;
        # _send_message records the most recent outbound text here so the
        # turn_log entry can attach it (Layer 2 bootstrap relies on this).
        self._turn_last_outbound: Optional[str] = None
        # Current turn kind — set by _handle_signal/_handle_surface/
        # _handle_emergency/_handle_cli at entry, reset in finally.
        # _send_message uses this to decide whether to honor quiet hours:
        # signal + cli + emergency turns bypass (the user is waiting on
        # an answer); surface turns honor (Alice-initiated thoughts wait
        # for morning).
        self._current_turn_kind: Optional[str] = None
        # Reply channel for the current turn — set by handlers at entry,
        # cleared in finally. _send_message uses this when Alice picks
        # recipient='self' to dispatch back over the originating
        # transport. None outside of a turn or for surface/emergency
        # turns where there's no inbound channel.
        self._current_reply_channel: Optional[ChannelRef] = None
        # Display name for the principal whose turn we're inside. Used
        # by ``_emit_send_event`` so e.g. cli_send's sender_name reads
        # the principal's display name rather than the opaque conn_id
        # from the ChannelRef. None outside of an inbound conversational turn.
        self._current_principal_display_name: Optional[str] = None
        # The bootstrap preamble lives on :class:`TurnRunner` —
        # constructed below, after the CLI transport gates so the
        # CLITraceHandler can wire to it.
        # One-shot consumer startup guard.
        self._consumer_started: bool = False
        # Background-task subagent registry. Each entry is an
        # asyncio.Task running an isolated kernel.run() for the
        # dispatched sub-agent. Populated by :meth:`_dispatch_subagent`
        # (called via the dispatch_background_task MCP tool); entries
        # remove themselves on completion. Drain cancels everything
        # still in-flight at shutdown.
        self._subagent_tasks: dict[str, asyncio.Task] = {}
        # Internal source for the synthetic completion event the
        # subagent waiter pushes onto self._queue. Constructed once
        # so the registry can route by event_type; no producer.
        self._background_task_source = BackgroundTaskCompletionSource()
        # Mid-turn context injection inbox. Keyed by canonical channel
        # key (``<transport>:<address>``). When a new inbound message
        # arrives while Alice is mid-turn FOR THAT SAME CHANNEL,
        # producers divert the message here instead of queueing it as
        # the next turn. The PostToolUse hook drains the per-channel
        # list at every tool boundary and injects the messages into
        # Alice's context as ``additionalContext`` so the next LLM
        # round sees them.
        #
        # Each entry is a tuple ``(text_for_context, original_event)``:
        # the text is what the hook surfaces; the event is what gets
        # pushed back onto the per-transport queue at turn-end if the
        # drain didn't run (e.g., a tool-less turn).
        self._mid_turn_inbox: dict[str, list[tuple[str, Any]]] = {}
        # Drain-stopper flag — flipped True by ``_send_message`` when
        # Alice replies on the inbound channel. Once she's emitted the
        # user-visible reply, further mid-turn injections would land
        # context the model can't act on ("here's a follow-up" after
        # the conversation already rolled). So once replied, producers
        # stop diverting and the hook stops injecting for this turn.
        # Reset to False on turn entry.
        self._current_turn_replied: bool = False

        # CLI transport — optional, falls back to no-op if disabled.
        # Constructed here so it shares the daemon's lifecycle and can
        # see _current_reply_channel via _send_message. ACL + display
        # name come from the address book.
        self.cli_transport: Optional[CLITransport] = (
            CLITransport(
                socket_path=cfg.cli_socket_path,
                is_allowed=lambda uid: self.address_book.is_allowed("cli", uid),
                principal_name_for=lambda uid: self.address_book.display_name_for(
                    "cli", uid
                ),
            )
            if cfg.cli_enabled
            else None
        )

        # Discord transport — optional. Constructed only when a bot token
        # is configured; absent token = transport stays None and existing
        # deploys keep working unchanged. The import itself is lazy: the
        # transport module top-imports ``discord``, which would otherwise
        # crash the daemon at startup when the optional dep is missing.
        self.discord_transport: Optional["DiscordTransport"] = None
        if cfg.discord_bot_token:
            from .transports.discord import DiscordTransport

            self.discord_transport = DiscordTransport(token=cfg.discord_bot_token)

        # A2A transport — optional. Constructed only when explicitly
        # enabled in alice.env. Import is lazy so worker images that
        # don't ship a2a-sdk (e.g. minimal builds) start fine.
        self.a2a_transport: Optional["A2ATransport"] = None
        if cfg.a2a_enabled:
            from .transports.a2a import A2ATransport

            self.a2a_transport = A2ATransport(
                port=cfg.a2a_port,
                host=cfg.a2a_host,
                principal_name=cfg.a2a_principal,
                external_url=cfg.a2a_external_url or None,
            )

        # Viewer-chat transport — local HTTP loopback ingress that the
        # viewer's chat panel POSTs to and subscribes to via SSE.
        # Construction here mirrors the other transports; the daemon
        # owns the lifecycle and the registry picks it up below.
        self.viewer_chat_transport: Optional[ViewerChatTransport] = None
        if cfg.viewer_chat_enabled:
            self.viewer_chat_transport = ViewerChatTransport(
                host=cfg.viewer_chat_host,
                port=cfg.viewer_chat_port,
                principal_name=cfg.viewer_chat_principal,
                principal_display_name=cfg.viewer_chat_principal_display_name,
            )

        # Heterogeneous event queue: each producer pushes its own
        # event type, the registry routes by ``type(event)`` (Phase 3
        # of plan 01). No Union annotation — the closed set lives in
        # the registry, not here.
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        # Phase 3 / 5 of plan 01: dispatcher routes by event type
        # via a registry instead of an isinstance ladder. Signal is
        # intentionally omitted — its events flow through the
        # transport's own inbox (Phase 2a), never the main queue.
        # Watchers are constructed up here because the daemon also
        # reaches them directly for archive bookkeeping; the factory
        # registers them by reference.
        self._surface_watcher = SurfaceWatcher(cfg.mind_dir)
        self._emergency_watcher = EmergencyWatcher(cfg.mind_dir)
        # CozyHem SSE subscriber — optional. Empty URL disables the
        # subscriber entirely (operator opted out, or there's no
        # CozyHem deployed on this network). Non-empty URL means
        # construct the subscriber; the producer reconnects on its
        # own if CozyHem itself is unreachable.
        self._cozyhem_subscriber: Optional[CozyHemEventSubscriber] = (
            CozyHemEventSubscriber(events_url=cfg.cozyhem_events_url)
            if cfg.cozyhem_events_url
            else None
        )
        # Plan 06 Phase 3: load model.yml so the daemon knows which
        # backend speaking will run on. Missing → subscription default
        # (today's behaviour). The auth env-mutation happens in
        # :meth:`run` once the daemon is actually about to dispatch
        # turns; here we only resolve the spec.
        self._model_config = factory_module.build_model_config(cfg)
        # Plan 04 Phase 7: install the mind-aware prompt loader as
        # the package-level singleton so every ``prompts.load(...)``
        # call site (compaction, transport build_prompt, surface /
        # emergency handlers) sees this mind's override path.
        # ``self._personae`` was resolved earlier (before tools_module.build).
        import prompts as _prompts

        _prompts.set_default_loader(
            factory_module.build_prompt_loader(cfg, self._personae)
        )
        # Plan 05 Phase 3: render the persona system-prompt fragment
        # once at startup; TurnRunner threads it into every kernel
        # call via KernelSpec.append_system_prompt.
        self._system_prompt = factory_module.build_system_prompt(self._personae)
        # Plan 07 P3 / plan-pi Phase C: render speaking-scope skills
        # to a per-hemisphere ephemeral dir (computed earlier as
        # self._skills_cwd so the session preflight uses the right
        # cwd). Kernel cwd swaps to this dir so the SDK auto-loader /
        # pi auto-discovery sees only in-scope, Jinja-rendered,
        # strict-YAML SKILL.md files.
        from skills.registry import SkillRegistry
        from skills.render import render_to_disk

        skill_registry = SkillRegistry.from_mind(cfg.mind_dir)
        render_to_disk(
            skill_registry,
            hemisphere="speaking",
            target_dir=self._skills_cwd,
            personae=self._personae,
            mind_dir=cfg.mind_dir,
        )
        self._registry = factory_module.build_registry(
            cfg,
            transports=(
                self.cli_transport,
                self.discord_transport,
                self.a2a_transport,
                self.viewer_chat_transport,
            ),
            surface_watcher=self._surface_watcher,
            emergency_watcher=self._emergency_watcher,
            background_task_source=self._background_task_source,
            cozyhem_subscriber=self._cozyhem_subscriber,
        )
        # Phase 6a of plan 01: outbound dispatch + quiet-queue
        # routing + canonical send-event emission live in
        # :class:`OutboxRouter`. Daemon's ``_send_message`` becomes
        # a thin facade that resolves recipient → channel and
        # delegates here.
        self.outbox = OutboxRouter(
            transport_for=lambda name: {
                "signal": self.signal_transport,
                "cli": self.cli_transport,
                "discord": self.discord_transport,
                "a2a": self.a2a_transport,
                "viewer-chat": self.viewer_chat_transport,
            }.get(name),
            address_book=self.address_book,
            events=self.events,
            quiet_queue=self.quiet_queue,
            speaking_cfg=cfg.speaking,
        )
        # Phase 2a of plan 01 introduced a second consumer (Signal's
        # per-transport batch loop runs alongside the main consumer).
        # Both must serialise on shared kernel state — _run_turn,
        # session_id, _current_turn_kind, etc. — so each turn-runner
        # acquires this lock around the pre-turn services + handler
        # body. Phase 6 replaces the lock with a TurnDispatcher that
        # owns the same invariant explicitly.
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        # Two-stage shutdown:
        #   _drain (first SIGTERM) — stop accepting new events, finish
        #       in-flight turn, drain queued events, then exit cleanly
        #       so the blue/green deploy can hand the lease over without
        #       killing a turn mid-Claude-call.
        #   _stop (second SIGTERM) — force-stop, cancels everything
        #       immediately. Escape hatch for a hung drain.
        self._drain = asyncio.Event()
        self._stop = asyncio.Event()
        # Phase 6c of plan 01: quiet-hours queue watcher + drain
        # entry point live on QuietQueueRunner. Daemon's run loop
        # schedules ``runner.watch()`` and the startup path calls
        # ``runner.drain()``.
        self.quiet_queue_runner = QuietQueueRunner(
            speaking_cfg=cfg.speaking,
            quiet_queue=self.quiet_queue,
            events=self.events,
            dispatch_outbound=self._dispatch_outbound,
            stop_event=self._stop,
        )
        # Phase 6c of plan 01: kernel-call orchestration + session
        # identity + bootstrap preamble live on :class:`TurnRunner`.
        # Daemon proxies ``session_id`` and ``_run_turn`` /
        # ``_prime_bootstrap_preamble`` through to it so existing
        # callers (the handlers in ``_dispatch.py``, the compaction
        # trigger reaching via ``ctx``) keep working.
        self.turn_runner = TurnRunner(
            cfg=cfg,
            events=self.events,
            turns=self.turns,
            mcp_servers=self.mcp_servers,
            custom_tool_names=self.custom_tool_names,
            session_path=self._session_path,
            summary_path=self._summary_path,
            compaction=self.compaction,
            cli_transport=self.cli_transport,
            turn_did_send_getter=lambda: self._turn_did_send,
            current_reply_channel_getter=lambda: self._current_reply_channel,
            # Resolve transport name → instance for the lifecycle handler.
            # Same registry shape as the OutboxRouter above, evaluated
            # at call time so transports added later (or replaced during
            # a hot reload) are picked up.
            transport_for=lambda name: {
                "signal": self.signal_transport,
                "cli": self.cli_transport,
                "discord": self.discord_transport,
                "a2a": self.a2a_transport,
                "viewer-chat": self.viewer_chat_transport,
            }.get(name),
            system_prompt=self._system_prompt,
            # Plan 06 Phase 3: model.yml's speaking.model wins over
            # alice.config.json's speaking.model when set; back-compat
            # falls through to the legacy field when model.yml is
            # missing or omits speaking.model.
            model=factory_module.build_kernel_model(
                cfg.speaking, self._model_config.speaking
            ),
            # Plan-pi Phase B: thread the BackendSpec through so the
            # TurnRunner's per-turn make_kernel call picks the right
            # impl (AnthropicKernel for subscription/api/bedrock,
            # PiKernel for pi).
            backend=self._model_config.speaking,
            pi_send_message=lambda args: tools_module.messaging.send_message_from_args(
                args,
                address_book=self.address_book,
                sender=self._send_message,
            ),
            # Plan-pi Phase C: kernel cwd swaps to the per-hemisphere
            # rendered skills dir; the agent retains read access to
            # the mind via add_dirs so skill bodies referencing
            # ~/alice-mind/... paths keep working.
            skills_cwd=self._skills_cwd,
            mind_dir=cfg.mind_dir,
            # Native Task interception via PreToolUse hook. The hook
            # fires before every Task/Agent call; we deny it (so the
            # SDK doesn't run the blocking built-in) and dispatch the
            # sub-agent into asyncio background work instead. The
            # deny reason becomes the model-visible tool result.
            # (PreToolUse rather than the SDK's can_use_tool callback,
            # which only fires when the CLI sends a permission request
            # — built-in tools in default mode never do — observed
            # empirically on the b06d2f1 deploy.)
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher="Task|Agent",
                        hooks=[self._pretooluse_hook],
                    ),
                ],
                # PostToolUse fires after every tool result. We use
                # it as the "next convenient point" for injecting
                # mid-turn inbound messages into Alice's context.
                # Matcher None == match every tool.
                "PostToolUse": [
                    HookMatcher(
                        matcher=None,
                        hooks=[self._posttooluse_hook],
                    ),
                ],
            },
        )
        self.turn_runner.session_id = initial_session_id
        # ContextProbe — read-only snapshot of the live context
        # composition for the CLI socket's ``{"type": "context"}``
        # request. All accessors are lambdas so the probe always
        # returns current state, never the value at construction
        # time.
        self.context_probe = ContextProbe(
            get_system_prompt=lambda: self._system_prompt,
            get_builtin_tools=lambda: list(BUILTIN_TOOLS),
            get_custom_tool_names=lambda: list(self.custom_tool_names),
            get_mcp_servers=lambda: dict(self.mcp_servers or {}),
            get_mcp_tool_defs=lambda: dict(self.mcp_tool_defs or {}),
            get_session_id=lambda: self.turn_runner.session_id,
            get_pending_preamble=lambda: self.turn_runner._pending_preamble,
            get_current_turn_kind=lambda: self._current_turn_kind,
            get_model=lambda: self.turn_runner._model
            or self.cfg.speaking.get("model"),
            get_backend=lambda: self._model_config.speaking.backend,
            get_mind_dir=lambda: str(self.cfg.mind_dir),
            get_skills_cwd=lambda: str(self._skills_cwd),
        )
        # Attach the probe to the CLI transport so its
        # ``{"type": "context"}`` RPC handler can answer requests.
        # The transport was built earlier (its lifecycle starts before
        # the probe's dependencies are wired); this is the post-hoc
        # bind.
        if self.cli_transport is not None:
            self.cli_transport.context_probe = self.context_probe
        self._config_path = cfg.mind_dir / "config" / "alice.config.json"
        self._config_mtime: float = (
            self._config_path.stat().st_mtime if self._config_path.is_file() else 0.0
        )

    # ------------------------------------------------------------------
    # session_id / pending_preamble live on :class:`TurnRunner`
    # (Phase 6c of plan 01); proxy them so existing callers (the
    # handlers in ``_dispatch.py``, ``compaction.run()`` reaching
    # via ctx) keep working unchanged.

    @property
    def session_id(self) -> Optional[str]:
        return self.turn_runner.session_id

    @session_id.setter
    def session_id(self, value: Optional[str]) -> None:
        self.turn_runner.session_id = value

    @property
    def _pending_preamble(self) -> Optional[str]:
        return self.turn_runner._pending_preamble

    @_pending_preamble.setter
    def _pending_preamble(self, value: Optional[str]) -> None:
        self.turn_runner._pending_preamble = value

    async def _run_turn(
        self,
        prompt: str,
        *,
        turn_id: str,
        outbound_recipient: Optional[str],
        silent: bool = False,
    ) -> str:
        """Facade so handlers / tests can keep calling ``ctx._run_turn``.

        Resets the per-turn flags (``_turn_did_send`` /
        ``_turn_last_outbound``) before delegating because those
        live on the daemon — :meth:`_send_message` writes them, and
        :class:`TurnRunner` reads ``_turn_did_send`` via the
        injected getter to decide whether to emit ``missed_reply``.
        """
        self._turn_did_send = False
        self._turn_last_outbound = None
        return await self.turn_runner.run_turn(
            prompt,
            turn_id=turn_id,
            outbound_recipient=outbound_recipient,
            silent=silent,
        )

    def _prime_bootstrap_preamble(self) -> None:
        """Facade so :class:`CompactionTrigger.run` can reach the
        preamble primer through ctx."""
        self.turn_runner.prime_bootstrap_preamble()

    # ------------------------------------------------------------------
    # Lifecycle

    async def run(self) -> None:
        # Resolve auth from alice.env + os.environ. ensure_auth_env() sets
        # the right vars on os.environ so the Agent SDK's CLI subprocess
        # inherits either subscription (CLAUDE_CODE_OAUTH_TOKEN), api-mode
        # (ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY), or bedrock-mode
        # (CLAUDE_CODE_USE_BEDROCK=1 + AWS_REGION) credentials.
        #
        # Plan 06 Phase 3: when model.yml declares the speaking backend
        # explicitly, pass that as ``mode_hint`` so the resolution is
        # config-driven rather than implicit-from-env. Minds without
        # model.yml fall through to the implicit logic (mode_hint=None).
        speaking_backend = self._model_config.speaking
        ensure_auth_env(
            mode_hint=speaking_backend.backend,
            aws_region=speaking_backend.region,
            aws_profile=speaking_backend.profile,
        )

        loop = asyncio.get_event_loop()

        # Force-stop hard budget: once the second signal fires we
        # guarantee the process exits within this many seconds, even
        # if asyncio cancellation is being swallowed somewhere (e.g.
        # an in-flight subagent's CLI subprocess that ignores
        # CancelledError, or a transport.stop() blocked in sync I/O).
        # Override via env for ops; floor at 1s.
        force_stop_budget_env = os.environ.get(
            "ALICE_SPEAKING_FORCE_STOP_BUDGET", ""
        ).strip()
        try:
            force_stop_budget = (
                float(force_stop_budget_env)
                if force_stop_budget_env
                else 8.0
            )
        except ValueError:
            log.warning(
                "ignoring non-numeric ALICE_SPEAKING_FORCE_STOP_BUDGET=%r",
                force_stop_budget_env,
            )
            force_stop_budget = 8.0
        force_stop_budget = max(1.0, force_stop_budget)

        def _hard_kill_after_budget() -> None:
            # Runs on a plain daemon thread so it survives even if
            # the event loop is wedged. If we get here the shutdown
            # sequence didn't finish in time — exit hard. Non-zero
            # because this is an abnormal exit; init/Docker will
            # restart the container.
            log.error(
                "force-stop budget (%.1fs) elapsed; calling os._exit(1)",
                force_stop_budget,
            )
            os._exit(1)

        def _on_signal() -> None:
            if not self._drain.is_set():
                log.info("signal received; entering drain mode")
                self._drain.set()
            else:
                log.warning(
                    "second signal received; force-stopping (hard kill in %.1fs)",
                    force_stop_budget,
                )
                self._stop.set()
                # Arm the wall-clock guard exactly once.
                if not getattr(self, "_hard_kill_armed", False):
                    self._hard_kill_armed = True
                    t = threading.Timer(
                        force_stop_budget, _hard_kill_after_budget
                    )
                    t.daemon = True
                    t.start()

        for sig in (_signal.SIGTERM, _signal.SIGINT):
            loop.add_signal_handler(sig, _on_signal)

        self.events.emit(
            "daemon_start",
            model=self.turn_runner._model or self.cfg.speaking.get("model"),
            backend=self._model_config.speaking.backend,
            session_id=self.session_id,
            compaction_threshold=self.cfg.speaking.get("context_compaction_threshold"),
            bootstrap_turns=self.cfg.speaking.get("context_bootstrap_turns"),
        )
        try:
            if self.signal is not None and self.signal_transport is not None:
                log.info("waiting for signal-cli at %s", self.cfg.signal_api)
                await self.signal.wait_ready()
                await self.signal_transport.start()
            else:
                log.info("signal disabled (no SIGNAL_ACCOUNT); skipping signal-cli")
            log.info("daemon ready; listening")
            self.events.emit("daemon_ready", signal_api=self.cfg.signal_api)

            # If quiet hours ended while we were down, drain the queue first.
            if not is_quiet_hours(self.cfg.speaking) and self.quiet_queue.size() > 0:
                await self.quiet_queue_runner.drain(reason="startup")

            # Prime the Layer 2 bootstrap preamble if we don't have a
            # session to resume. The consumer picks it up on the first turn.
            self._prime_bootstrap_preamble()

            # Phase 5 of plan 01: every event-producing source owns
            # its own producer task, including the surface and
            # emergency watchers. Daemon supervises them under
            # uniform start/cancel semantics; the only thing left
            # daemon-private is the quiet-hours queue watcher (a
            # cross-cutting concern, not an event source).
            ctx = _dispatch_module.DaemonContext(self)

            # Startup phase: best-effort one-shot tasks that prime
            # ``ctx`` with mind-state (surface backlog, fitness
            # registry, meso-cycle, cortex-index freshness). Each
            # source is fail-soft per-source, so a missing mind
            # file or a kernel-side OSError doesn't block boot.
            await factory_module.run_startup_phase(self._registry, ctx)

            producers: list[asyncio.Task] = [
                asyncio.create_task(
                    self.quiet_queue_runner.watch(), name="quiet-watch"
                ),
            ]
            for source in self._registry.all_event_sources():
                # Transports that need a network-level handshake
                # (Discord, A2A) expose ``start()`` on the channel-
                # layer half of the Transport protocol. Internal
                # sources (SurfaceWatcher, EmergencyWatcher) don't.
                start = getattr(source, "start", None)
                if start is not None:
                    await start()
                task = source.producer(ctx)
                if task is not None:
                    producers.append(task)
            # Signal owns its own per-transport consumer loop
            # (Phase 2a) and is intentionally absent from the
            # registry; schedule it separately. Tracked apart from
            # ``producers`` so drain can stop just its inner _produce
            # while leaving _consume to finish the inbox.
            signal_run_task: Optional[asyncio.Task] = None
            if self.signal_transport is not None:
                signal_run_task = self.signal_transport.producer(ctx)
            consumer = asyncio.create_task(self._consumer(), name="consumer")
            drain_task = asyncio.create_task(self._drain.wait(), name="drain")
            stop_task = asyncio.create_task(self._stop.wait(), name="stop")

            watch_set: set[asyncio.Task] = {
                *producers,
                consumer,
                drain_task,
                stop_task,
            }
            if signal_run_task is not None:
                watch_set.add(signal_run_task)

            done, _ = await asyncio.wait(
                watch_set, return_when=asyncio.FIRST_COMPLETED
            )
            log.info("shutdown trigger: %s", [t.get_name() for t in done])

            if stop_task in done:
                # Force-stop path: SIGTERM with no preceding drain
                # (_drain not set first), or a second signal during
                # drain. Cancel everything now; in-flight turn dies
                # via CancelledError — but bound the await so a task
                # that swallows CancelledError can't wedge shutdown.
                # The wall-clock timer armed in _on_signal is the
                # last-resort backstop (os._exit after budget).
                log.warning("force-stop: cancelling all tasks")
                cancel_set: list[asyncio.Task] = [
                    *producers,
                    consumer,
                    drain_task,
                ]
                if signal_run_task is not None:
                    cancel_set.append(signal_run_task)
                # Subagent tasks (the most likely offender: their
                # claude CLI subprocess may not honor CancelledError
                # promptly). Cancel these too on force-stop.
                cancel_set.extend(
                    t for t in self._subagent_tasks.values() if not t.done()
                )
                for task in cancel_set:
                    if not task.done():
                        task.cancel()
                # Bounded join: ``asyncio.wait(timeout=N)`` is
                # genuinely bounded -- it returns (done, pending)
                # without awaiting pending tasks. ``wait_for(gather(...))``
                # is NOT: if any child swallows CancelledError, the
                # inner ``_cancel_and_wait`` hangs indefinitely. The
                # wall-clock os._exit guard armed in _on_signal is
                # the absolute backstop either way.
                if cancel_set:
                    with contextlib.suppress(BaseException):
                        await asyncio.wait(cancel_set, timeout=3.0)
                stragglers = [t for t in cancel_set if not t.done()]
                if stragglers:
                    log.warning(
                        "force-stop: %d task(s) did not honor cancel in 3s: %s",
                        len(stragglers),
                        [t.get_name() for t in stragglers],
                    )
            else:
                # Drain path. Triggered by SIGTERM (drain_task done)
                # or by an upstream task crashing — either way the
                # in-flight turn shouldn't pay the price. Stop
                # accepting new events; let consumers finish what's
                # already queued; then exit.
                log.info("drain: stopping producers (no new events accepted)")

                # Cancel non-Signal producers; events they've already
                # pushed to ``self._queue`` stay there and will be
                # processed by the main consumer below.
                for prod in producers:
                    if not prod.done():
                        prod.cancel()
                for prod in producers:
                    with contextlib.suppress(BaseException):
                        await prod

                # Signal: stop the inner _produce, let _consume finish
                # the inbox. Supervisor task is cancelled afterwards.
                if self.signal_transport is not None:
                    with contextlib.suppress(Exception):
                        await self.signal_transport.drain()

                # Background subagents: cancel everything still running.
                # Each task's finally block pushes a completion event
                # before exiting (now flagged is_error=True via the
                # CancelledError path), so any in-flight handles still
                # surface back as "didn't finish" rather than vanishing
                # from the registry. Await with suppression — the tasks
                # may raise CancelledError or propagate other transport
                # errors as they tear down.
                if self._subagent_tasks:
                    log.info(
                        "drain: cancelling %d in-flight subagent task(s)",
                        len(self._subagent_tasks),
                    )
                    subagent_snapshot = list(self._subagent_tasks.values())
                    for task in subagent_snapshot:
                        if not task.done():
                            task.cancel()
                    # Bounded await: ``asyncio.wait(timeout=N)`` returns
                    # (done, pending) without re-awaiting pending tasks.
                    # A subagent whose claude CLI subprocess swallows
                    # CancelledError stays in ``pending``; we log and
                    # move on rather than wedge.
                    if subagent_snapshot:
                        with contextlib.suppress(BaseException):
                            await asyncio.wait(
                                subagent_snapshot, timeout=3.0
                            )
                    stragglers = [
                        t for t in subagent_snapshot if not t.done()
                    ]
                    if stragglers:
                        log.warning(
                            "drain: %d subagent task(s) did not honor "
                            "cancel in 3s: %s",
                            len(stragglers),
                            [t.get_name() for t in stragglers],
                        )

                log.info("drain: waiting for in-flight turn + queued events")
                drain_timeout_env = os.environ.get(
                    "ALICE_SPEAKING_DRAIN_TIMEOUT", ""
                ).strip()
                drain_timeout: Optional[float]
                try:
                    drain_timeout = (
                        float(drain_timeout_env) if drain_timeout_env else None
                    )
                except ValueError:
                    log.warning(
                        "ignoring non-numeric ALICE_SPEAKING_DRAIN_TIMEOUT=%r",
                        drain_timeout_env,
                    )
                    drain_timeout = None

                # _queue.join() blocks until every put() has a matching
                # task_done(). Consumer's task_done is in finally
                # (see _consumer below), so this implicitly waits for
                # the current turn to complete.
                drain_await = asyncio.create_task(
                    self._queue.join(), name="drain-await"
                )
                stop_during_drain = asyncio.create_task(
                    self._stop.wait(), name="stop-during-drain"
                )
                try:
                    if drain_timeout is not None:
                        done2, _ = await asyncio.wait(
                            {drain_await, stop_during_drain},
                            timeout=drain_timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done2:
                            log.warning(
                                "drain timeout (%.1fs) exceeded", drain_timeout
                            )
                    else:
                        await asyncio.wait(
                            {drain_await, stop_during_drain},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    if stop_during_drain.done() and not drain_await.done():
                        log.warning("force-stop received mid-drain")
                finally:
                    for task in (drain_await, stop_during_drain):
                        if not task.done():
                            task.cancel()
                        with contextlib.suppress(BaseException):
                            await task

                log.info("drain complete; cancelling consumers")
                cancel_set = [consumer, drain_task]
                if signal_run_task is not None:
                    cancel_set.append(signal_run_task)
                for task in cancel_set:
                    if not task.done():
                        task.cancel()
                # Bounded await: ``asyncio.wait(timeout=N)`` is the
                # only genuinely bounded primitive when cancellation
                # may be swallowed. The wall-clock timer armed by a
                # subsequent SIGTERM is the absolute backstop.
                if cancel_set:
                    with contextlib.suppress(BaseException):
                        await asyncio.wait(cancel_set, timeout=5.0)
                stragglers = [t for t in cancel_set if not t.done()]
                if stragglers:
                    log.warning(
                        "drain: %d consumer task(s) did not honor "
                        "cancel in 5s: %s",
                        len(stragglers),
                        [t.get_name() for t in stragglers],
                    )
        finally:
            # Bounded transport teardown: any single stop() that
            # blocks in sync I/O could otherwise wedge shutdown
            # indefinitely. Per-call cap is small; the wall-clock
            # force-stop guard is the absolute backstop.
            async def _stop_with_timeout(name: str, coro_factory) -> None:
                try:
                    await asyncio.wait_for(coro_factory(), timeout=3.0)
                except asyncio.TimeoutError:
                    log.warning(
                        "shutdown: %s.stop() exceeded 3s timeout", name
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "shutdown: %s.stop() raised %s: %s",
                        name,
                        type(exc).__name__,
                        exc,
                    )

            if self.signal_transport is not None:
                await _stop_with_timeout(
                    "signal_transport", self.signal_transport.stop
                )
            if self.signal is not None:
                await _stop_with_timeout("signal", self.signal.aclose)
            if self.cli_transport is not None:
                await _stop_with_timeout(
                    "cli_transport", self.cli_transport.stop
                )
            if self.discord_transport is not None:
                await _stop_with_timeout(
                    "discord_transport", self.discord_transport.stop
                )
            if self.a2a_transport is not None:
                await _stop_with_timeout(
                    "a2a_transport", self.a2a_transport.stop
                )
            if self.viewer_chat_transport is not None:
                await _stop_with_timeout(
                    "viewer_chat_transport", self.viewer_chat_transport.stop
                )
            self.events.emit("shutdown")
            log.info("shutdown complete")

    # ------------------------------------------------------------------
    # Producers
    #
    # All event-producing sources own their producer task in Phase 5
    # of plan 01: transports under ``transports/*`` (Phase 2),
    # internal sources under ``internal/*`` (Phase 5). Daemon's
    # ``run()`` schedules them via ``self._registry``; nothing
    # event-source-specific lives here anymore.

    # ------------------------------------------------------------------
    # Consumer

    async def _consumer(self) -> None:
        # Signal events bypass this loop — Phase 2a of plan 01 routes
        # them through SignalTransport's own per-transport inbox.
        # Everything else (CLI, Discord, A2A, surfaces, emergencies)
        # reaches the dispatcher here, and Phase 3's registry routes
        # by ``type(event)`` instead of an isinstance ladder.
        ctx = _dispatch_module.DaemonContext(self)
        while True:
            event = await self._queue.get()
            try:
                source = self._registry.lookup(type(event))
                if source is None:
                    log.warning("no handler for event type: %s", type(event).__name__)
                    continue
                async with self._turn_lock:
                    await self._pre_turn(event)
                    await source.handle(ctx, event)
            except Exception:
                log.exception("consumer error handling %s", type(event).__name__)
            finally:
                self._queue.task_done()

    async def _pre_turn(self, event: object) -> None:
        """Pre-turn services run before any handler.

        Both consumers (the dispatcher main loop and SignalTransport's
        per-transport batch loop) hold ``self._turn_lock`` and call
        this so the config reload + compaction policy can't race.
        Compaction runs BEFORE any inbound event so the token check
        from the previous turn has a chance to roll the session
        before we append more context. Phase 6b of plan 01 routes
        the policy through :class:`CompactionTrigger.should_run` —
        the deferral hook lives there.
        """
        self._maybe_reload_config()
        if self.compaction.should_run(event):
            await self.compaction.run(_dispatch_module.DaemonContext(self))

    def _maybe_reload_config(self) -> None:
        """Reload alice.config.json if it has changed on disk.

        Hot-reload happens at event boundaries — each signal / surface /
        emergency turn begins with the freshest config. Alice's `write_config`
        tool therefore takes effect on her next turn, no daemon restart
        needed. Hot-reloadable: model, quiet_hours,
        working_context_token_budget, context_bootstrap_turns,
        context_compaction_threshold.
        """
        if not self._config_path.is_file():
            return
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._config_mtime:
            return
        try:
            new_cfg = config_module.load()
        except Exception:  # noqa: BLE001
            log.exception("config reload failed; keeping current cfg")
            return
        # Mutate cfg.speaking in place rather than rebinding self.cfg —
        # TurnRunner and other components hold the original Config by
        # reference (captured at startup), so a rebind would leave their
        # ``self._cfg.speaking`` pointing at the pre-reload dict and the
        # changed knobs (compaction threshold, etc.) would never reach
        # the per-turn read sites.
        old_speaking = dict(self.cfg.speaking)
        self.cfg.speaking.clear()
        self.cfg.speaking.update(new_cfg.speaking)
        self._config_mtime = mtime
        changes = {
            k: v for k, v in self.cfg.speaking.items() if old_speaking.get(k) != v
        }
        log.info(
            "config reloaded (changes: %s)", list(changes.keys()) or "none observed"
        )
        self.events.emit("config_reload", changes=list(changes.keys()))

    # ------------------------------------------------------------------
    # Per-event handlers (CLI, Discord, A2A, Signal, Surface, Emergency)
    # all live in :mod:`alice_speaking._dispatch` (Phase 1 of plan 01)
    # and are reached via the source registry (Phase 3) or — for Signal
    # — its per-transport consumer loop (Phase 2a). Daemon-side delegate
    # methods retired with Phase 3.

    # Per-transport prompt assembly lives on each transport class
    # (Phase 6c of plan 01) — handlers in :mod:`_dispatch` reach
    # ``ctx.<name>_transport.build_prompt(...)``.

    # Quiet-hours queue watcher + manual drain live on
    # :class:`QuietQueueRunner` (Phase 6c of plan 01). Surface /
    # emergency archive live on the watcher classes
    # (``ctx._surface_watcher.archive_unresolved(...)`` /
    # ``ctx._emergency_watcher.archive(...)``).

    # ------------------------------------------------------------------
    # Mid-turn context injection — producer-side routing + drain helpers

    @staticmethod
    def _channel_key(channel: "ChannelRef") -> str:
        return f"{channel.transport}:{channel.address}"

    def divert_to_mid_turn(
        self, channel: "ChannelRef", text: str, original_event: Any
    ) -> bool:
        """Producer entry point. Returns True when the inbound is
        diverted into the mid-turn inbox; False when the caller should
        proceed with normal queueing (start a new turn after the
        current one finishes).

        Producers (Signal ``_produce``, CLI accept loop, etc.) call
        this with the inbound's canonical channel + display text +
        the transport's original event object. The event is what we
        push back onto the per-transport queue at turn-end if the
        drain hook didn't fire (so a tool-less turn doesn't black-hole
        the message).
        """
        # No in-flight turn, no channel to inject into.
        if self._current_reply_channel is None:
            return False
        # Once Alice has replied on the channel, the conversation has
        # logically rolled — drained messages would be acting on a
        # finished thread. Queue as a new turn instead.
        if self._current_turn_replied:
            return False
        cur_key = self._channel_key(self._current_reply_channel)
        new_key = self._channel_key(channel)
        if cur_key != new_key:
            return False
        self._mid_turn_inbox.setdefault(cur_key, []).append((text, original_event))
        self.events.emit(
            "mid_turn_inbound_diverted",
            channel=cur_key,
            text_chars=len(text or ""),
            pending=len(self._mid_turn_inbox[cur_key]),
        )
        return True

    def _flush_mid_turn_inbox(self, channel: "ChannelRef") -> None:
        """Called at turn-end. Any messages still in the mid-turn inbox
        for this channel didn't get drained by the PostToolUse hook
        (probably a tool-less turn) — push them back into normal
        circulation so they become the next turn's prompt.

        The original transport events are stashed in the inbox tuples
        for exactly this purpose; each transport knows how to handle
        its own event type via the registry.
        """
        key = self._channel_key(channel)
        pending = self._mid_turn_inbox.pop(key, [])
        if not pending:
            return
        for _text, event in pending:
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning(
                    "mid_turn flush: queue full; dropping message on %s",
                    key,
                )
        self.events.emit(
            "mid_turn_inbox_flushed",
            channel=key,
            message_count=len(pending),
        )

    # ------------------------------------------------------------------
    # Native Task interception via PreToolUse hook
    #
    # The SDK's ``can_use_tool`` permission callback only fires when
    # the CLI actually sends a permission request, which doesn't
    # happen for built-in tools like Task in the default permission
    # mode. PreToolUse hooks fire on every tool call regardless,
    # which is what we need to intercept Task before it blocks the
    # parent turn for minutes on a synchronous sub-agent run.

    async def _pretooluse_hook(
        self,
        input_data: dict[str, Any],
        tool_use_id: Optional[str],
        context: Any,
    ) -> dict[str, Any]:
        """PreToolUse hook callback — intercepts Task/Agent.

        Hook input shape (per :class:`PreToolUseHookInput` in the SDK):
        ``{"hook_event_name": "PreToolUse", "tool_name": str,
        "tool_input": dict, "tool_use_id": str, "agent_id": str?}``.

        Return value is a SyncHookJSONOutput dict. We use
        ``hookSpecificOutput.permissionDecision="deny"`` with a
        ``permissionDecisionReason`` that becomes the model-visible
        tool result. Any other tool returns an empty dict (pass-through).
        """
        tool_name = input_data.get("tool_name") or ""
        # Pass through anything that isn't Task. Empty dict = no
        # decision = SDK proceeds normally.
        if tool_name not in ("Task", "Agent"):
            return {}

        # Defensive: never intercept inside a sub-agent context.
        # Sub-agents only get BUILTIN_TOOLS (no Task) so this can't
        # happen today, but the guard keeps us safe if that changes.
        if input_data.get("agent_id"):
            return {}

        tool_input = input_data.get("tool_input") or {}
        description = (tool_input.get("description") or "").strip() or "background task"
        prompt = (tool_input.get("prompt") or "").strip()
        if not prompt:
            log.warning(
                "Task interception: empty prompt; passing through to SDK"
            )
            return {}

        try:
            handle = await self._dispatch_subagent(description, prompt)
        except Exception as exc:  # noqa: BLE001
            log.exception("Task interception: dispatch failed")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Task interception failed to dispatch a "
                        f"background sub-agent: {type(exc).__name__}: "
                        f"{exc}. Try again or do the work inline."
                    ),
                }
            }

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Task intercepted — sub-agent dispatched as "
                    f"{handle} ({description!r}). It runs in the "
                    f"background; your parent turn is NOT blocked. "
                    f"You'll receive a fresh inbound turn with the "
                    f"result on the originating channel when it "
                    f"finishes. Wrap up briefly and end this turn — "
                    f"don't wait."
                ),
            }
        }

    async def _posttooluse_hook(
        self,
        input_data: dict[str, Any],
        tool_use_id: Optional[str],
        context: Any,
    ) -> dict[str, Any]:
        """Drain the mid-turn inbox for the current channel and inject
        pending user messages as ``additionalContext`` for the next
        LLM round.

        Fires after every tool result. Most of the time the inbox is
        empty and we return ``{}`` (no-op). When there's pending
        inbound (a same-channel message arrived while Alice was
        working), the next round of the model sees a synthesised user
        block describing what showed up.

        Stops draining once Alice has replied on the channel for this
        turn (``_current_turn_replied`` set by ``_send_message``).
        Drained-but-too-late messages would land in context that the
        model can't act on; better to let them queue as the next turn
        from the user's POV. (Alice's design call.)
        """
        if self._current_reply_channel is None:
            return {}
        if self._current_turn_replied:
            return {}
        key = self._channel_key(self._current_reply_channel)
        pending = self._mid_turn_inbox.pop(key, [])
        if not pending:
            return {}

        principal = self._current_principal_display_name or "the user"
        if len(pending) == 1:
            header = (
                f"--- {principal} sent a follow-up message while you "
                "were working on this turn ---"
            )
        else:
            header = (
                f"--- {principal} sent {len(pending)} follow-up messages "
                "while you were working on this turn ---"
            )
        body_lines = [f"{principal}: {text}" for text, _evt in pending]
        additional_context = "\n".join([header, *body_lines])

        self.events.emit(
            "mid_turn_context_injected",
            channel=key,
            message_count=len(pending),
            chars=len(additional_context),
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": additional_context,
            }
        }

    # ------------------------------------------------------------------
    # Background-task dispatch (used by _intercept_task above)

    async def _dispatch_subagent(
        self, description: str, instructions: str
    ) -> str:
        """Spawn a sub-agent in an asyncio background task and return
        its handle immediately.

        The sub-agent runs as stock Claude — :data:`BUILTIN_TOOLS` only,
        no MCP servers, no Alice persona, no resume of her session.
        That deliberately walls it off: it can't recursively dispatch,
        can't talk to Signal, can't read her notes (except via Bash /
        Read on shared filesystem). It does the job and reports back.

        The originating reply channel + principal display name are
        captured here so the eventual completion event routes back
        to whoever Alice was talking to when she dispatched. By the
        time the sub-agent finishes, ``_current_reply_channel`` will
        almost certainly point somewhere else (a different turn, or
        nothing at all).

        Returns the ``bg-<short-uuid>`` handle. Same handle appears on
        the :class:`BackgroundTaskCompleteEvent` later, letting Alice
        correlate "this finish → that earlier promise."
        """
        handle = f"bg-{uuid.uuid4().hex[:12]}"
        channel = self._current_reply_channel
        principal = self._current_principal_display_name or "?"
        started = time.monotonic()
        self.events.emit(
            "background_task_dispatch_request",
            handle=handle,
            description=description,
            principal_name=principal,
            channel_transport=(channel.transport if channel else None),
            channel_address=(channel.address if channel else None),
            instruction_chars=len(instructions),
        )

        # Auto-fix race-window suppression: when the subagent prompt
        # matches the auto-fix worker template, write a
        # ``type: dispatched-in-flight`` gh-state record BEFORE the
        # worker spawns so Thinking's dispatcher scan doesn't surface
        # a duplicate ``attempt-issue-fix`` before the worker pushes a
        # branch. Non-auto-fix dispatches are a no-op. See
        # cortex-memory/research/2026-05-19-dispatched-inflight-speaking-wiring.md
        # and PR #262 (write path).
        auto_fix_module.record_auto_fix_inflight(instructions, handle)

        async def _run_subagent() -> None:
            result_text = ""
            is_error = False
            try:
                # Build a fresh kernel — same backend as Alice (so
                # auth env is shared) but with no resume, no MCP,
                # and no persona system prompt.
                sub_kernel = make_kernel(
                    self.turn_runner._backend,
                    self.events,
                    correlation_id=handle,
                    silent=True,
                    short_cap=4000,
                )
                sub_spec = KernelSpec(
                    model=self.turn_runner._model
                    or self.cfg.speaking.get("model"),
                    allowed_tools=list(BUILTIN_TOOLS),
                    mcp_servers={},
                    cwd=self.turn_runner._skills_cwd or self.cfg.work_dir,
                    add_dirs=(
                        [self.turn_runner._mind_dir]
                        if self.turn_runner._mind_dir is not None
                        else None
                    ),
                    resume=None,
                    thinking="medium",
                    append_system_prompt=None,
                )
                result = await sub_kernel.run(
                    instructions, sub_spec, handlers=[]
                )
                result_text = (result.text or "").strip()
                is_error = bool(result.is_error or result.error)
            except asyncio.CancelledError:
                # Drain or shutdown cancelled us — propagate.
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "background subagent %s crashed", handle
                )
                result_text = (
                    f"Sub-agent crashed: {type(exc).__name__}: {exc}"
                )
                is_error = True
            finally:
                self._subagent_tasks.pop(handle, None)
                # Push completion event onto the dispatcher queue —
                # even on cancel, so a drained subagent still surfaces
                # back as "didn't finish" rather than vanishing.
                try:
                    self._queue.put_nowait(
                        BackgroundTaskCompleteEvent(
                            handle=handle,
                            description=description,
                            result_text=result_text,
                            is_error=is_error,
                            channel=channel,
                            principal_name=principal,
                        )
                    )
                except asyncio.QueueFull:
                    log.warning(
                        "queue full pushing completion for %s; "
                        "result lost",
                        handle,
                    )
                self.events.emit(
                    "background_task_dispatch_complete",
                    handle=handle,
                    description=description,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    is_error=is_error,
                    result_chars=len(result_text or ""),
                )

        task = asyncio.create_task(
            _run_subagent(), name=f"bg-subagent-{handle}"
        )
        self._subagent_tasks[handle] = task
        return handle

    # ------------------------------------------------------------------
    # send_message router (closure given to tools.messaging)

    async def _send_message(
        self,
        recipient: ResolvedRecipient,
        text: str,
        attachments: Optional[list[str]] = None,
    ) -> None:
        """Dispatch send_message to the right transport and track did-send.

        Two recipient modes:

        - ``recipient == SELF_RECIPIENT`` — Alice asked to reply on the
          inbound channel. We use ``self._current_reply_channel`` directly
          and dispatch via the transport that owns it.
        - a :class:`ChannelRef` — an explicit channel resolved by the
          messaging tool (via the address book or a raw E.164 number).
          Routed through the transport identified by ``channel.transport``.

        Quiet-hours policy (signal + discord): replies to inbound turns
        and emergencies bypass the queue — the user is waiting on an
        answer. Surface-triggered sends (Alice's own thoughts) honor
        quiet hours and route through :class:`QuietQueue`. Attachments
        always bypass — the queue stores text-only payloads and dropping
        attachments there would silently ditch media.

        CLI sends never queue: the user is at a terminal waiting; quiet
        hours don't apply.
        """
        if recipient == SELF_RECIPIENT:
            channel = self._current_reply_channel
            if channel is None:
                raise RuntimeError(
                    "send_message(recipient='self') has no inbound channel "
                    "to reply on (only valid during a signal/cli/discord/"
                    "emergency turn)"
                )
        else:
            assert isinstance(recipient, ChannelRef)
            channel = recipient

        # CLI deliverability is now decided at write time inside
        # CLITransport.send: if the address book's CLI channel (uid)
        # has any live connection, the send broadcasts to all of them;
        # otherwise the transport logs and drops. The previous
        # pre-flight `durable=False` reject was too aggressive — it
        # blocked surface- and emergency-driven sends to "owner" even
        # when a TUI session was actively connected and addressable.

        emergency = getattr(self, "_emergency_bypass", False)
        # Bypass triggers: emergency-flavored turn, or we're inside an
        # inbound conversational turn whose user is waiting, or we'd have
        # to drop attachments to queue. CLI + viewer-chat are
        # always-bypass (operator is at a UI waiting).
        bypass_quiet = (
            channel.transport in ("cli", "a2a", "viewer-chat")
            or emergency
            or self._current_turn_kind
            in ("signal", "discord", "cli", "a2a", "viewer-chat")
            or bool(attachments)
        )

        await self._dispatch_outbound(
            channel,
            text,
            attachments,
            emergency=emergency,
            bypass_quiet=bypass_quiet,
        )
        self._turn_last_outbound = text
        self._turn_did_send = True
        # Drain-stopper for mid-turn injection. Once the user-visible
        # reply has landed, further mid-turn injections would surface
        # context the model can't act on (conversation has rolled).
        # Producers stop diverting and the PostToolUse hook stops
        # draining once this flips. Reset to False on turn entry.
        self._current_turn_replied = True

    # ------------------------------------------------------------------
    # Unified outbound dispatch — Phase 6a of plan 01 lifted the
    # routing + quiet-queue + send-event code into
    # :class:`OutboxRouter`. Daemon-side helpers are thin facades
    # that pass the daemon's per-turn principal-display-name through.

    async def _dispatch_outbound(
        self,
        channel: ChannelRef,
        text: str,
        attachments: Optional[list[str]] = None,
        *,
        turn_id: Optional[str] = None,
        emergency: bool = False,
        bypass_quiet: bool = False,
    ) -> None:
        await self.outbox.dispatch(
            channel,
            text,
            attachments,
            turn_id=turn_id,
            emergency=emergency,
            bypass_quiet=bypass_quiet,
            principal_display_name=self._current_principal_display_name,
        )

    def _sender_name_for(self, recipient: str) -> str:
        return self.address_book.display_name_for("signal", recipient)

    # ------------------------------------------------------------------
    # Kernel invocation + bootstrap preamble + compaction execution
    # all live in their own modules now (Phase 6c of plan 01):
    #
    #   - ``_run_turn`` / ``_compose_prompt`` / ``_build_spec`` /
    #     ``_build_handlers`` → :class:`TurnRunner`
    #     (see ``self.turn_runner``).
    #   - ``_prime_bootstrap_preamble`` → :meth:`TurnRunner.prime_bootstrap_preamble`
    #     (the daemon facade above delegates).
    #   - Compaction execution → :class:`CompactionTrigger.run`
    #     (see ``self.compaction``).


async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = config_module.load()
    log.info("speaking alice starting (model=%s)", cfg.speaking.get("model"))
    daemon = SpeakingDaemon(cfg)
    await daemon.run()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
