"""Hermes Agent harness selection and stream-json subprocess integration."""
import asyncio
import sys

import pytest

from core.config.model import ModelConfigError, from_mapping
from core.kernel import KernelSpec, NullHandler, make_kernel
from kernels.hermes_agent.kernel import HermesAgentKernel
from kernels.pi.kernel import PiKernel


class Events:
    def __init__(self):
        self.items = []

    def emit(self, event, **fields):
        self.items.append((event, fields))


class Capture(NullHandler):
    def __init__(self):
        self.calls = []

    async def on_tool_use(self, name, input, id):
        self.calls.append(("use", name, id))

    async def on_tool_result(self, tool_use_id, content, is_error):
        self.calls.append(("result", tool_use_id, is_error))

    async def on_result(self, summary):
        self.calls.append(("summary", summary.session_id))


def test_selects_real_hermes_agent_not_model_shim():
    cfg = from_mapping({"thinking": {"harness": "hermes-agent", "model": "litellm/qwen-local"},
                        "backends": {"pi": {"base_url": "http://localhost:4000/v1"}}})
    assert cfg.thinking.backend == "pi"
    kernel = make_kernel(cfg.thinking, Events())
    assert isinstance(kernel, HermesAgentKernel)
    assert kernel.base_url == "http://localhost:4000/v1"
    pi = from_mapping({"thinking": {"harness": "pi-mono", "model": "litellm/qwen-local"}})
    assert isinstance(make_kernel(pi.thinking, Events()), PiKernel)


def test_rejects_invalid_pairing():
    with pytest.raises(ModelConfigError):
        from_mapping({"thinking": {"harness": "hermes-agent", "backend": "subscription"}})


def test_missing_endpoint_fails_closed():
    kernel = HermesAgentKernel(Events())
    with pytest.raises(ValueError, match="backends.pi.base_url"):
        asyncio.run(kernel.run("hello", KernelSpec(model="litellm/qwen-local")))


def test_speaking_outbox_fails_closed():
    kernel = HermesAgentKernel(Events(), base_url="http://localhost/v1")
    with pytest.raises(ValueError, match="send_message"):
        asyncio.run(kernel.run("hello", KernelSpec(model="qwen-local", allowed_tools=["mcp__alice__send_message"])))


def test_stream_json_subprocess(monkeypatch, tmp_path):
    stub = tmp_path / "hermes"
    stub.write_text("#!" + sys.executable + "\n"
        "import json,sys\n"
        "a=sys.argv[1:]\n"
        "assert a[:4] == ['chat','--query-file','-','--oneshot']\n"
        "assert a[a.index('--model')+1] == 'qwen-local'\n"
        "assert a[a.index('--base-url')+1] == 'http://localhost/v1'\n"
        "assert '--run-budget' not in a and '--toolsets' not in a\n"
        "assert 'instructions' in sys.stdin.read()\n"
        "for e in ({'type':'system','session_id':'s1'},"
        "{'type':'tool_use','name':'terminal','input':{'command':'pwd'},'tool_call_id':'t1'},"
        "{'type':'tool_result','name':'terminal','output':'ok','tool_call_id':'t1'},"
        "{'type':'text','text':'Done'},"
        "{'type':'result','session_id':'s1','text':'Done','tokens':{'input':10,'output':2},'duration_ms':8,'exit_code':0}):"
        " print(json.dumps(e),flush=True)\n")
    stub.chmod(0o755)
    monkeypatch.setenv("ALICE_HERMES_BIN", str(stub))
    events = Events()
    handler = Capture()
    kernel = HermesAgentKernel(events, base_url="http://localhost/v1")
    result = asyncio.run(kernel.run("hello", KernelSpec(model="litellm/qwen-local",
        append_system_prompt="instructions", max_seconds=5), [handler]))
    assert result.text == "Done"
    assert result.session_id == "s1"
    assert result.usage.input_tokens == 10
    assert handler.calls == [("use", "terminal", "t1"), ("result", "t1", False), ("summary", "s1")]
    assert any(e == "result" for e, _ in events.items)


def test_litellm_key_is_scoped_to_matching_endpoint(monkeypatch, tmp_path):
    stub = tmp_path / "hermes"
    stub.write_text("#!" + sys.executable + "\n"
        "import json,os\n"
        "print(json.dumps({'type':'result','text':json.dumps({"
        "'key':os.getenv('OPENAI_API_KEY'), 'url':os.getenv('OPENAI_BASE_URL')}),"
        "'exit_code':0}),flush=True)\n")
    stub.chmod(0o755)
    monkeypatch.setenv("ALICE_HERMES_BIN", str(stub))
    monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    matching = HermesAgentKernel(Events(), base_url="http://localhost/v1")
    result = asyncio.run(matching.run("ping", KernelSpec(model="qwen-local")))
    assert result.text == '{"key": "test-key", "url": "http://localhost/v1"}'
    other = HermesAgentKernel(Events(), base_url="http://other/v1")
    result = asyncio.run(other.run("ping", KernelSpec(model="qwen-local")))
    assert result.text == '{"key": null, "url": null}'
