"""alice_cozylobe — third reasoning region (CozyHem / home-automation domain).

Lives inside the alice container alongside :mod:`alice_speaking` and
:mod:`alice_thinking`. Unlike the two hemispheres, the cozylobe is a
*specialized* sub-component: bounded domain (home control + presence
+ scene inference), small model (qwen 27b on desktop-3090), event-
driven cadence (push from cozyhem-engine's SSE stream rather than
cron).

Design notes (mandatory reading before extending this module):

* ``cortex-memory/research/2026-05-23-cozyhem-lobe-design.md`` —
  placement, vault boundary, urgency model.
* ``cortex-memory/research/2026-05-23-cozyhem-lobe-wake-loop.md`` —
  four-phase wake loop (ingest → classify → reason → surface).
* ``cortex-memory/research/2026-05-23-cozyhem-lobe-qwen-prompt.md`` —
  prompt template + intent vocabulary.
* ``cortex-memory/decisions/cozyhem-inter-lobe-protocol.md`` —
  HTTP/JSON-RPC (sync), SSE (async). A2A rejected.

Walking-skeleton scope (PR #344): one SSE event in → optional qwen
classification → one ``run_agent`` reasoning pass on the cozylobe
AgentSpec → one observation note or surface file out. Richer
batching, urgency-tier fast-path, qwen prompt tuning, and the
multi-event reasoning window land in follow-up PRs.

The cozylobe runs the agent step through :func:`core.agent_library.run_agent`
on the registered ``cozylobe`` :class:`~core.agent_library.AgentSpec`
(Jason's 2026-05-24 directive). Direct kernel calls are forbidden.
"""

from __future__ import annotations

from .activity_fetcher import ActivityFetcher, ActivitySnapshot
from .events import CozyHemEvent
from .qwen_client import QwenClassification, QwenClient, QwenUnreachable
from .sse_consumer import SSEConsumer
from .surfaces import write_observation_note, write_urgent_surface
from .wake_loop import WakeLoop


__all__ = [
    "ActivityFetcher",
    "ActivitySnapshot",
    "CozyHemEvent",
    "QwenClassification",
    "QwenClient",
    "QwenUnreachable",
    "SSEConsumer",
    "WakeLoop",
    "write_observation_note",
    "write_urgent_surface",
]
