"""AG-UI protocol dataclasses + SSE encoder.

Hand-rolled for Phase-1 ``/alice/run`` — no first-party Python SDK
exists upstream (https://github.com/ag-ui-protocol/ag-ui is TypeScript
+ a few community backends). The wire shape we emit tracks the
`BaseEvent` taxonomy documented at
https://docs.ag-ui.com/concepts/events.

Conventions (from the spec we verified against the TS SDK types):

- Every event has a ``type`` field whose value is the
  SCREAMING_SNAKE_CASE discriminator (``"RUN_STARTED"``,
  ``"TOOL_CALL_ARGS"``, …). All other field names are camelCase
  (``threadId``, ``runId``, ``toolCallId``, ``toolCallName``).
- ``timestamp`` is optional on every event; we omit it unless the
  caller passes one (tests pin a value for determinism).
- SSE framing is ``data: <json>\\n\\n``. We do not set an ``event:``
  line — the discriminator lives inside the JSON payload, which
  matches the SDK's own decoder and the status-site precedent in
  ``src/status_site/_sse.py``.

Only the three event *families* Phase 1 needs are modelled here —
``Run*``, ``ToolCall*``, ``TextMessage*``. Adding a new family is a
one-liner once the ``@dataclass(frozen=True)`` + ``EVENT_TYPE``
constant pattern is clear.

Also included: an ``RunAgentInput`` loader that validates the subset
of fields the daemon reads (threadId, runId, messages, tools,
context, state, forwardedProps). Unknown fields are preserved on
``extra`` so a future AG-UI version adding a field doesn't 400.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Event type discriminators


class EventType:
    """SCREAMING_SNAKE_CASE discriminator strings; the wire ``type`` field."""

    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
    TOOL_CALL_END = "TOOL_CALL_END"
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"


# ---------------------------------------------------------------------------
# Event dataclasses (frozen; immutable once constructed).
#
# Each event's ``to_dict`` produces the camelCase wire shape. ``to_sse_line``
# wraps it in the ``data: <json>\\n\\n`` SSE framing. The trailing blank line
# is a single ``\\n`` — ``data: …\\n\\n`` reads naturally when you tail it.


@dataclass(frozen=True)
class _BaseEvent:
    """Fields every AG-UI event may carry."""

    timestamp: Optional[float] = None
    raw_event: Optional[Any] = None

    # ``type`` is a class attribute set by subclasses — we read it in
    # ``_payload_dict`` below so the subclass never has to re-declare it.
    TYPE: str = ""

    def _payload_dict(self) -> dict[str, Any]:
        """Subclasses override to add their specific fields."""
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.TYPE}
        out.update(self._payload_dict())
        if self.timestamp is not None:
            out["timestamp"] = self.timestamp
        if self.raw_event is not None:
            out["rawEvent"] = self.raw_event
        return out

    def to_sse_line(self) -> str:
        """Return a single SSE record: ``data: <json>\\n\\n``."""
        return f"data: {json.dumps(self.to_dict(), separators=(',', ':'))}\n\n"


@dataclass(frozen=True)
class RunStarted(_BaseEvent):
    thread_id: str = ""
    run_id: str = ""

    TYPE: str = EventType.RUN_STARTED

    def _payload_dict(self) -> dict[str, Any]:
        return {"threadId": self.thread_id, "runId": self.run_id}


@dataclass(frozen=True)
class RunFinished(_BaseEvent):
    thread_id: str = ""
    run_id: str = ""
    result: Optional[Any] = None

    TYPE: str = EventType.RUN_FINISHED

    def _payload_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"threadId": self.thread_id, "runId": self.run_id}
        if self.result is not None:
            out["result"] = self.result
        return out


@dataclass(frozen=True)
class RunError(_BaseEvent):
    message: str = ""
    code: Optional[str] = None

    TYPE: str = EventType.RUN_ERROR

    def _payload_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"message": self.message}
        if self.code is not None:
            out["code"] = self.code
        return out


@dataclass(frozen=True)
class ToolCallStart(_BaseEvent):
    tool_call_id: str = ""
    tool_call_name: str = ""
    parent_message_id: Optional[str] = None

    TYPE: str = EventType.TOOL_CALL_START

    def _payload_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "toolCallId": self.tool_call_id,
            "toolCallName": self.tool_call_name,
        }
        if self.parent_message_id is not None:
            out["parentMessageId"] = self.parent_message_id
        return out


@dataclass(frozen=True)
class ToolCallArgs(_BaseEvent):
    """One JSON-fragment delta for a tool-call's ``arguments`` string.

    The AG-UI spec streams arguments as a JSON string accumulated across
    deltas. A caller that already has the full args JSON may emit a
    single delta carrying the whole string — that is still spec-valid.
    """

    tool_call_id: str = ""
    delta: str = ""

    TYPE: str = EventType.TOOL_CALL_ARGS

    def _payload_dict(self) -> dict[str, Any]:
        return {"toolCallId": self.tool_call_id, "delta": self.delta}


@dataclass(frozen=True)
class ToolCallEnd(_BaseEvent):
    tool_call_id: str = ""

    TYPE: str = EventType.TOOL_CALL_END

    def _payload_dict(self) -> dict[str, Any]:
        return {"toolCallId": self.tool_call_id}


@dataclass(frozen=True)
class TextMessageStart(_BaseEvent):
    message_id: str = ""
    role: str = "assistant"

    TYPE: str = EventType.TEXT_MESSAGE_START

    def _payload_dict(self) -> dict[str, Any]:
        return {"messageId": self.message_id, "role": self.role}


@dataclass(frozen=True)
class TextMessageContent(_BaseEvent):
    message_id: str = ""
    delta: str = ""

    TYPE: str = EventType.TEXT_MESSAGE_CONTENT

    def _payload_dict(self) -> dict[str, Any]:
        # The AG-UI spec insists deltas are non-empty. The emitter is
        # responsible for skipping zero-length deltas before constructing
        # one of these; we don't enforce here because the dataclass is
        # already frozen and validation belongs upstream.
        return {"messageId": self.message_id, "delta": self.delta}


@dataclass(frozen=True)
class TextMessageEnd(_BaseEvent):
    message_id: str = ""

    TYPE: str = EventType.TEXT_MESSAGE_END

    def _payload_dict(self) -> dict[str, Any]:
        return {"messageId": self.message_id}


# ---------------------------------------------------------------------------
# RunAgentInput — the POST body the client hands us to start a run.


@dataclass(frozen=True)
class AGUITool:
    """One client-registered tool the agent may invoke this run.

    ``parameters`` is a JSON Schema the agent will respect when
    emitting ``ToolCallArgs`` deltas. We keep it as a raw ``dict`` —
    no validation here, the model's own schema-following is phase 2.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class AGUIMessage:
    """Transport-level `Message` from RunAgentInput.messages[].

    AG-UI's ``Message`` is a union discriminated by ``role``. We keep
    the dataclass intentionally lax — the daemon-side agent runner is
    the only consumer today and it only needs ``role`` + ``content``.
    Tool-result messages carry ``tool_call_id`` so a resumed run can
    thread them to the right call.
    """

    id: str
    role: str
    content: Any = ""
    name: Optional[str] = None
    tool_call_id: Optional[str] = None


@dataclass(frozen=True)
class RunAgentInput:
    """Validated POST body for ``/alice/run``.

    ``forwardedProps`` is spec-required but unused today — we store it
    so a future runner can read it without a protocol bump. Unknown
    top-level fields fall into ``extra`` so a newer AG-UI client
    doesn't get a 400 on a harmless addition.
    """

    thread_id: str
    run_id: str
    messages: Sequence[AGUIMessage] = ()
    tools: Sequence[AGUITool] = ()
    context: Sequence[Mapping[str, Any]] = ()
    state: Any = None
    forwarded_props: Any = None
    extra: Mapping[str, Any] = field(default_factory=dict)


def parse_run_agent_input(payload: Mapping[str, Any]) -> RunAgentInput:
    """Validate + normalise a POST body into :class:`RunAgentInput`.

    Raises :class:`ValueError` with a human-readable reason when the
    body is unusable. The ``/alice/run`` endpoint maps that to a
    ``400 Bad Request`` with the message as the error body.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("expected JSON object")

    thread_id = payload.get("threadId")
    run_id = payload.get("runId")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("threadId required")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("runId required")

    raw_messages = payload.get("messages", []) or []
    if not isinstance(raw_messages, list):
        raise ValueError("messages must be a list")
    messages: list[AGUIMessage] = []
    for i, m in enumerate(raw_messages):
        if not isinstance(m, Mapping):
            raise ValueError(f"messages[{i}] must be an object")
        mid = m.get("id") or ""
        role = m.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError(f"messages[{i}].role required")
        messages.append(
            AGUIMessage(
                id=str(mid),
                role=role,
                content=m.get("content", ""),
                name=m.get("name"),
                tool_call_id=m.get("toolCallId"),
            )
        )

    raw_tools = payload.get("tools", []) or []
    if not isinstance(raw_tools, list):
        raise ValueError("tools must be a list")
    tools: list[AGUITool] = []
    for i, t in enumerate(raw_tools):
        if not isinstance(t, Mapping):
            raise ValueError(f"tools[{i}] must be an object")
        name = t.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"tools[{i}].name required")
        params = t.get("parameters") or {}
        if not isinstance(params, Mapping):
            raise ValueError(f"tools[{i}].parameters must be an object")
        tools.append(
            AGUITool(
                name=name,
                description=str(t.get("description") or ""),
                parameters=dict(params),
            )
        )

    context = payload.get("context") or []
    if not isinstance(context, list):
        raise ValueError("context must be a list")

    known = {"threadId", "runId", "messages", "tools", "context", "state", "forwardedProps"}
    extra = {k: v for k, v in payload.items() if k not in known}

    return RunAgentInput(
        thread_id=thread_id,
        run_id=run_id,
        messages=tuple(messages),
        tools=tuple(tools),
        context=tuple(context),
        state=payload.get("state"),
        forwarded_props=payload.get("forwardedProps"),
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Public surface

AGUIEvent = _BaseEvent  # alias; callers type-check against the base class.


__all__ = [
    "AGUIEvent",
    "AGUIMessage",
    "AGUITool",
    "EventType",
    "RunAgentInput",
    "RunStarted",
    "RunFinished",
    "RunError",
    "ToolCallStart",
    "ToolCallArgs",
    "ToolCallEnd",
    "TextMessageStart",
    "TextMessageContent",
    "TextMessageEnd",
    "parse_run_agent_input",
]
