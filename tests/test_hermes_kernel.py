"""End-to-end-ish tests for kernels.hermes.kernel.HermesKernel.

Uses a monkeypatched httpx.AsyncClient replacement to return canned
OpenAI-schema chat-completions responses — exercises request
construction, response translation, and KernelResult assembly
without touching the network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.events import CapturingEmitter
from core.kernel import KernelSpec, NullHandler, TurnSummary

from kernels.hermes.kernel import HermesKernel
from kernels.hermes.translator import (
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    alice_tools_to_openai,
    openai_message_to_blocks,
    openai_usage_to_info,
)


# --- Test doubles -------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeAsyncClient:
    """httpx.AsyncClient stand-in — captures POST args and returns a
    scripted response. One-shot per request."""

    def __init__(
        self,
        *,
        response: _FakeResponse,
        capture: list[dict],
        **_kwargs: Any,
    ) -> None:
        self._response = response
        self._capture = capture

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict) -> _FakeResponse:
        self._capture.append({"url": url, "json": json, "headers": headers})
        return self._response


@pytest.fixture
def fake_httpx(monkeypatch):
    """Patch httpx.AsyncClient inside kernels.hermes.kernel so
    HermesKernel talks to a scripted responder instead of the wire."""
    captured: list[dict] = []
    response_holder: dict[str, _FakeResponse] = {}

    def _factory(**kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(
            response=response_holder["response"],
            capture=captured,
            **kwargs,
        )

    import kernels.hermes.kernel as hermes_kernel_mod

    monkeypatch.setattr(hermes_kernel_mod.httpx, "AsyncClient", _factory)

    def set_response(*, status_code: int = 200, payload: Any) -> None:
        response_holder["response"] = _FakeResponse(
            status_code=status_code, payload=payload
        )

    return set_response, captured


# --- Kernel-level tests -------------------------------------------------------


@pytest.mark.asyncio
async def test_hermes_kernel_run_returns_kernel_result_for_plain_text_reply(
    fake_httpx,
) -> None:
    set_response, captured = fake_httpx
    set_response(
        payload={
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello world"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49},
        }
    )

    cap = CapturingEmitter()
    kernel = HermesKernel(cap, correlation_id="t-1")

    fired_text: list[str] = []
    fired_results: list[TurnSummary] = []

    class H(NullHandler):
        async def on_text(self, text):
            fired_text.append(text)

        async def on_result(self, summary):
            fired_results.append(summary)

    result = await kernel.run(
        "say hello",
        KernelSpec(model="nousresearch/hermes-4-405b"),
        handlers=[H()],
    )

    assert result.text == "hello world"
    assert result.usage is not None
    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 7
    assert result.is_error is False
    assert result.cost_usd is None
    assert fired_text == ["hello world"]
    assert len(fired_results) == 1

    # Request wiring: model + user message threaded through.
    assert len(captured) == 1
    body = captured[0]["json"]
    assert body["model"] == "nousresearch/hermes-4-405b"
    assert body["messages"] == [{"role": "user", "content": "say hello"}]
    assert body["stream"] is False


@pytest.mark.asyncio
async def test_hermes_kernel_appends_system_prompt_when_populated(fake_httpx) -> None:
    set_response, captured = fake_httpx
    set_response(
        payload={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    kernel = HermesKernel(CapturingEmitter())
    await kernel.run(
        "ping",
        KernelSpec(
            model="nousresearch/hermes-4-405b",
            append_system_prompt="You are Alice.",
        ),
    )
    body = captured[0]["json"]
    assert body["messages"] == [
        {"role": "system", "content": "You are Alice."},
        {"role": "user", "content": "ping"},
    ]


@pytest.mark.asyncio
async def test_hermes_kernel_declares_tools_in_openai_shape(fake_httpx) -> None:
    set_response, captured = fake_httpx
    set_response(
        payload={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    kernel = HermesKernel(CapturingEmitter())
    await kernel.run(
        "hi",
        KernelSpec(
            model="hermes-4-405b",
            allowed_tools=["Bash", "Read", "WebFetch"],
        ),
    )
    body = captured[0]["json"]
    tool_names = [t["function"]["name"] for t in body.get("tools", [])]
    # WebFetch drops silently; Bash + Read declared.
    assert tool_names == ["Bash", "Read"]


@pytest.mark.asyncio
async def test_hermes_kernel_emits_tool_use_for_tool_calls_response(
    fake_httpx,
) -> None:
    set_response, captured = fake_httpx
    set_response(
        payload={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "Bash",
                                    "arguments": '{"command": "ls"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        }
    )

    cap = CapturingEmitter()
    kernel = HermesKernel(cap)

    seen_tools: list[tuple[str, Any, str]] = []

    class H(NullHandler):
        async def on_tool_use(self, name, input, id):  # noqa: A002
            seen_tools.append((name, input, id))

    await kernel.run(
        "list files",
        KernelSpec(model="hermes-4-405b", allowed_tools=["Bash"]),
        handlers=[H()],
    )
    assert seen_tools == [("Bash", {"command": "ls"}, "call-1")]
    tool_events = [e for e in cap.events if e["event"] == "tool_use"]
    assert tool_events and tool_events[0]["name"] == "Bash"


@pytest.mark.asyncio
async def test_hermes_kernel_surfaces_reasoning_content_as_thinking(
    fake_httpx,
) -> None:
    set_response, captured = fake_httpx
    set_response(
        payload={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "final answer",
                        "reasoning_content": "step 1: ...",
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    cap = CapturingEmitter()
    kernel = HermesKernel(cap)

    thoughts: list[str] = []

    class H(NullHandler):
        async def on_thinking(self, text):
            thoughts.append(text)

    result = await kernel.run("q", KernelSpec(model="hermes-4-405b"), handlers=[H()])
    assert thoughts == ["step 1: ..."]
    assert result.text == "final answer"


@pytest.mark.asyncio
async def test_hermes_kernel_maps_rate_limit_to_runtime_error(fake_httpx) -> None:
    set_response, _ = fake_httpx
    set_response(status_code=429, payload={"error": "too many requests"})
    kernel = HermesKernel(CapturingEmitter())
    with pytest.raises(RuntimeError, match=r"hermes rate_limit"):
        await kernel.run("hi", KernelSpec(model="hermes-4-405b"))


@pytest.mark.asyncio
async def test_hermes_kernel_maps_http_error_to_runtime_error(fake_httpx) -> None:
    set_response, _ = fake_httpx
    set_response(status_code=500, payload={"error": "internal"})
    kernel = HermesKernel(CapturingEmitter())
    with pytest.raises(RuntimeError, match=r"hermes error: HTTP 500"):
        await kernel.run("hi", KernelSpec(model="hermes-4-405b"))


@pytest.mark.asyncio
async def test_hermes_kernel_emits_drop_events_for_unsupported_fields(
    fake_httpx,
) -> None:
    set_response, _ = fake_httpx
    set_response(
        payload={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    cap = CapturingEmitter()
    kernel = HermesKernel(cap, correlation_id="t-drop")

    await kernel.run(
        "hi",
        KernelSpec(
            model="hermes-4-405b",
            hooks={"PreToolUse": [object()]},
            mcp_servers={"alice": {}},
            resume="sess-xyz",
        ),
    )

    drops = sorted(
        e["field"] for e in cap.events if e["event"] == "hermes_spec_field_dropped"
    )
    # Three of the five _HERMES_UNSUPPORTED_SPEC_FIELDS were populated.
    assert drops == ["hooks", "mcp_servers", "resume"]
    for e in cap.events:
        if e["event"] == "hermes_spec_field_dropped":
            assert e["turn_id"] == "t-drop"


@pytest.mark.asyncio
async def test_hermes_kernel_silent_suppresses_events(fake_httpx) -> None:
    set_response, _ = fake_httpx
    set_response(
        payload={
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    cap = CapturingEmitter()
    kernel = HermesKernel(cap, silent=True)

    fired: list[str] = []

    class H(NullHandler):
        async def on_text(self, text):
            fired.append(text)

    await kernel.run("x", KernelSpec(model="hermes-4-405b"), handlers=[H()])
    # Handlers still fire; event emission suppressed.
    assert fired == ["hi"]
    assert cap.events == []


@pytest.mark.asyncio
async def test_hermes_kernel_sends_bearer_token_when_env_key_present(
    fake_httpx, monkeypatch
) -> None:
    set_response, captured = fake_httpx
    set_response(
        payload={
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    monkeypatch.setenv("HERMES_API_KEY", "test-key-xyz")
    kernel = HermesKernel(CapturingEmitter())
    await kernel.run("x", KernelSpec(model="hermes-4-405b"))
    headers = captured[0]["headers"]
    assert headers.get("Authorization") == "Bearer test-key-xyz"


@pytest.mark.asyncio
async def test_hermes_kernel_omits_authorization_when_no_key(
    fake_httpx, monkeypatch
) -> None:
    set_response, captured = fake_httpx
    set_response(
        payload={
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    kernel = HermesKernel(CapturingEmitter())
    await kernel.run("x", KernelSpec(model="hermes-4-405b"))
    assert "Authorization" not in captured[0]["headers"]


@pytest.mark.asyncio
async def test_hermes_kernel_resolves_base_url_from_spec_then_env(
    fake_httpx, monkeypatch
) -> None:
    set_response, captured = fake_httpx
    set_response(
        payload={
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    # Env-provided base_url used when spec doesn't override.
    monkeypatch.setenv("HERMES_BASE_URL", "https://env.example.org/v1")
    kernel = HermesKernel(CapturingEmitter())
    await kernel.run("x", KernelSpec(model="hermes-4-405b"))
    assert captured[0]["url"] == "https://env.example.org/v1/chat/completions"


# --- Translator unit tests ----------------------------------------------------


def test_openai_message_to_blocks_text_only() -> None:
    blocks = openai_message_to_blocks(
        {"role": "assistant", "content": "hello"}
    )
    assert blocks == [TextBlock(text="hello")]


def test_openai_message_to_blocks_tool_calls_parses_arguments() -> None:
    blocks = openai_message_to_blocks(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c-1",
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "arguments": '{"command": "ls -la"}',
                    },
                }
            ],
        }
    )
    assert blocks == [ToolUseBlock(id="c-1", name="Bash", input={"command": "ls -la"})]


def test_openai_message_to_blocks_malformed_arguments_survive() -> None:
    blocks = openai_message_to_blocks(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c-2",
                    "type": "function",
                    "function": {"name": "Bash", "arguments": "not-json"},
                }
            ],
        }
    )
    assert blocks == [ToolUseBlock(id="c-2", name="Bash", input={"_raw": "not-json"})]


def test_openai_message_to_blocks_prepends_thinking_when_reasoning_present() -> None:
    blocks = openai_message_to_blocks(
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "step-by-step",
        }
    )
    assert blocks == [
        ThinkingBlock(thinking="step-by-step"),
        TextBlock(text="answer"),
    ]


def test_alice_tools_to_openai_declares_known_tools_and_drops_web_tools() -> None:
    tools = alice_tools_to_openai(["Bash", "WebFetch", "WebSearch", "Read"])
    names = [t["function"]["name"] for t in tools]
    assert names == ["Bash", "Read"]
    for t in tools:
        assert t["type"] == "function"
        assert "parameters" in t["function"]


def test_alice_tools_to_openai_registers_unknown_tools_permissively() -> None:
    tools = alice_tools_to_openai(["CustomTool"])
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "CustomTool"
    assert tools[0]["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }


def test_openai_usage_to_info_maps_prompt_and_completion_tokens() -> None:
    info = openai_usage_to_info(
        {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    )
    assert info is not None
    assert info.input_tokens == 100
    assert info.output_tokens == 50
    assert info.total_tokens == 150


def test_openai_usage_to_info_reads_prompt_cache_hits() -> None:
    info = openai_usage_to_info(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 40},
        }
    )
    assert info is not None
    assert info.cache_read_input_tokens == 40


def test_openai_usage_to_info_none_on_missing_or_bad_input() -> None:
    assert openai_usage_to_info(None) is None
    assert openai_usage_to_info({}) is None
    assert openai_usage_to_info("not a dict") is None  # type: ignore[arg-type]
