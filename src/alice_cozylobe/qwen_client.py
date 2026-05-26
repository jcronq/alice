"""Qwen 27b HTTP client — fast classifier for CozyHem events.

The lobe's reasoning step has two layers:

1. **qwen 27b pattern matcher** (this module). Lives on
   ``desktop-3090`` behind an OpenAI-compatible HTTP endpoint. Fast
   (~2-5s per call), cheap, bounded-domain. Produces a structured
   :class:`QwenClassification` per event: urgency tier + intent +
   summary.
2. **agent_library run_agent supervisor**. Lives in the alice
   container; consumes qwen's classification + the raw event and
   decides whether to write an observation note, a surface, or
   nothing. Runs through :func:`core.agent_library.run_agent` on the
   registered ``cozylobe`` AgentSpec (Jason's directive).

**Graceful degrade contract** (design's "lobe-goes-quiet-on-link-loss"
requirement): when qwen is unreachable, :meth:`QwenClient.classify`
raises :class:`QwenUnreachable`. The wake loop catches it, logs once,
and either skips reasoning entirely for the event or falls back to
hardcoded rules for CRITICAL events. The lobe must never fabricate a
classification when the model is down.

Hardcoded endpoint: ``http://desktop-3090:8080`` per the design's
named target. TODO(cozyhem-engine#31): replace with AI-fleet-managed
service binding once the registry ships.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

from .events import CozyHemEvent


__all__ = [
    "DEFAULT_QWEN_ENDPOINT",
    "DEFAULT_QWEN_MODEL",
    "QwenClassification",
    "QwenClient",
    "QwenUnreachable",
]


log = logging.getLogger(__name__)


# Classifier endpoint. In-container this is the LiteLLM proxy
# (LITELLM_BASE_URL=http://alice-litellm:4000/v1), the single seam for
# all local-model traffic — matches viewer.lobe_labeler + thinking's
# stage_b/d call sites so backend moves never touch this code again.
# Fallback is the direct LAN desktop Qwen (3090, 10.20.30.147:8033) for
# dev outside the container. NOTE: the base now includes ``/v1`` (the
# OpenAI convention these sites share); :meth:`classify` appends only
# ``/chat/completions``. The old hardcoded ``desktop-3090:8080`` was
# stale on both host and port.
DEFAULT_QWEN_ENDPOINT = os.environ.get(
    "LITELLM_BASE_URL", "http://10.20.30.147:8033/v1"
)

# Virtual model name resolved by sandbox/litellm/config.yaml -> the
# 3090 desktop Qwen. The LAN llama.cpp server ignores the model field,
# so this also works against the direct fallback.
DEFAULT_QWEN_MODEL = os.environ.get("LITELLM_QWEN_MODEL", "qwen-desktop")

# Bearer token for the LiteLLM proxy (its master key). Empty in dev/tests
# and against the direct LAN fallback (unauthenticated) — the
# Authorization header is only sent when this is non-empty.
DEFAULT_QWEN_API_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

# Per the qwen-prompt design: low temperature for structured output,
# high enough for some flexibility in pattern recognition.
DEFAULT_TEMPERATURE = 0.4

# Per the wake-loop design: qwen 27b inference is ~2-5s; cap a single
# call at 30s so a hung endpoint surfaces as :class:`QwenUnreachable`
# rather than wedging the wake loop indefinitely.
DEFAULT_TIMEOUT_SECONDS = 30.0


class QwenUnreachable(RuntimeError):
    """Raised when the qwen endpoint is unreachable or errored.

    The wake loop catches this and skips the reasoning step for the
    event (or falls back to hardcoded rules for CRITICAL events). Do
    not retry inside this client — backoff happens in the supervisor.
    """


@dataclass(frozen=True)
class QwenClassification:
    """One qwen classification of one CozyHem event.

    Shape mirrors the qwen-prompt design's output JSON object
    (urgency + intent + summary + reasoning). Multiple-action outputs
    from a batched call are flattened to one classification per event
    by the wake loop; richer batching ships in a follow-up.

    Attributes:
        urgency: One of ``"CRITICAL" | "HIGH" | "MEDIUM" | "LOW"``.
            CRITICAL is reserved for the hardcoded fast-path and is
            never produced by qwen itself.
        intent: One of ``"notify" | "act" | "log" | "investigate"``.
        summary: One-line human-readable summary.
        reasoning: Brief explanation (max 20 words per the prompt).
        raw: The unparsed JSON object qwen returned. Escape hatch for
            inspection; structured handlers should rely on the typed
            fields above.
    """

    urgency: str
    intent: str
    summary: str
    reasoning: str
    raw: dict


class QwenClient:
    """Thin HTTP wrapper around the qwen 27b OpenAI-compatible API.

    Inject ``http_client_factory`` in tests so we don't open real
    sockets. The factory returns an :class:`httpx.AsyncClient`-shaped
    async context manager.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_QWEN_ENDPOINT,
        *,
        model: str = DEFAULT_QWEN_MODEL,
        api_key: str = DEFAULT_QWEN_API_KEY,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        http_client_factory=None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._temperature = temperature
        self._timeout_seconds = timeout_seconds
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout_seconds)
        )

    async def classify(
        self,
        event: CozyHemEvent,
        *,
        context: Optional[dict] = None,
    ) -> QwenClassification:
        """Send one event to qwen and parse the structured response.

        Raises :class:`QwenUnreachable` on any network / HTTP / parse
        failure. The wake loop catches it; this method does NOT retry.
        """
        prompt = self._build_prompt(event, context or {})
        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self._temperature,
            "stream": False,
            # The Qwen3.6 builds are reasoning models: with thinking on
            # they spend the token budget on ``reasoning_content`` and
            # return ``content=""`` — which would make :meth:`_parse_response`
            # see no JSON. Classification is a closed-form task, so turn
            # thinking off (same fix viewer.lobe_labeler uses). LiteLLM's
            # ``drop_params: true`` strips this safely if a backend ignores it.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers = (
            {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        )
        try:
            client_cm = self._http_client_factory()
            async with client_cm as client:
                response = await client.post(
                    f"{self._endpoint}/chat/completions",
                    json=body,
                    headers=headers,
                )
                response.raise_for_status()
                blob = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, OSError) as exc:
            raise QwenUnreachable(
                f"qwen endpoint {self._endpoint} unreachable or errored: {exc}"
            ) from exc

        return self._parse_response(blob)

    def _build_prompt(self, event: CozyHemEvent, context: dict) -> str:
        """Render the qwen-prompt template from the design note.

        Walking-skeleton scope: single-event prompt rather than the
        full 30s batched window. The output schema is the same; the
        wake loop unpacks a one-element ``actions`` list to a single
        :class:`QwenClassification`.
        """
        event_summary = json.dumps(
            [
                {
                    "id": "event_0",
                    "type": event.kind,
                    "entity": event.entity_id,
                    "payload": event.payload,
                    "ts": event.received_at,
                }
            ],
            separators=(",", ":"),
        )
        context_json = json.dumps(context, separators=(",", ":"))
        return (
            "You are a home automation reasoning engine. Analyze the "
            "events below and return structured actions.\n\n"
            f"EVENTS (batched, ~30s window):\n{event_summary}\n\n"
            f"CONTEXT (from lobe scratch space):\n{context_json}\n\n"
            "RULES:\n"
            "1. CRITICAL events (doorbell, motion indoors at night, security) "
            "are handled by hardcoded rules upstream — do not classify them.\n"
            "2. You process HIGH, MEDIUM, and LOW events.\n"
            "3. Each action has one intent: notify, act, log, or investigate.\n"
            "4. NEVER propose \"act\" for high-impact actions (door locks, "
            "HVAC changes, anthem power).\n"
            "5. If unsure, prefer \"log\" — never escalate uncertain events.\n\n"
            "RETURN ONLY a JSON object with this structure:\n"
            "{\n"
            "  \"actions\": [\n"
            "    {\n"
            "      \"urgency\": \"HIGH\" | \"MEDIUM\" | \"LOW\",\n"
            "      \"intent\": \"notify\" | \"act\" | \"log\" | \"investigate\",\n"
            "      \"entity_ids\": [\"...\"],\n"
            "      \"summary\": \"one-line summary\",\n"
            "      \"reasoning\": \"max 20 words\"\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            "No preamble. No markdown. No explanation outside the JSON object."
        )

    def _parse_response(self, blob: dict) -> QwenClassification:
        """Pull the assistant content out of the OpenAI-style response
        envelope and parse the inner JSON object. Strict on shape —
        anything we don't recognize becomes ``QwenUnreachable`` so the
        supervisor degrades gracefully.
        """
        try:
            content = blob["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise QwenUnreachable(
                f"qwen response missing choices[0].message.content: {exc}"
            ) from exc

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise QwenUnreachable(
                f"qwen returned non-JSON content: {exc}"
            ) from exc

        actions = parsed.get("actions") if isinstance(parsed, dict) else None
        if not actions or not isinstance(actions, list):
            raise QwenUnreachable(
                "qwen response missing or empty 'actions' list"
            )

        first = actions[0]
        if not isinstance(first, dict):
            raise QwenUnreachable("qwen action is not a dict")

        return QwenClassification(
            urgency=str(first.get("urgency", "LOW")),
            intent=str(first.get("intent", "log")),
            summary=str(first.get("summary", "")),
            reasoning=str(first.get("reasoning", "")),
            raw=parsed,
        )
