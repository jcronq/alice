"""Unit tests for the SSE stall-watchdog in ``static/index.html`` (status-site LIVE badge).

The hook ``useEventStream`` is embedded in index.html alongside React
markup, so we can't import it as an ES module. Instead the Python
harness extracts the hook text, swaps the real STALL_MS / WATCHDOG
constants for tiny ones, wires React-hook shims plus a fetch mock
whose reader hangs after one chunk, and runs the result under
``node --test``. The hook must then:

1. Dispatch ``{type: "__status", connected: false, error: "stalled"}``.
2. Call ``ac.abort()`` to break the outer connect loop.
3. Clear its watchdog interval cleanly.

If ``node`` isn't installed the test is skipped. If the key markers
(``STALL_MS``, ``lastByteAt``, ``setInterval``, ``stalled``) are
missing from index.html the extract step fails loudly before any
node invocation.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from status_site.server import STATIC_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

INDEX = Path(STATIC_DIR) / "index.html"

# Markers that MUST be present in the patched hook source. A regression
# that drops any of these fails the test before the behavioural run.
REQUIRED_MARKERS = (
    "const STALL_MS = 35000",
    "const WATCHDOG_INTERVAL_MS = 5000",
    "let lastByteAt = Date.now()",
    "if (value) lastByteAt = Date.now()",
    "setInterval(() => {",
    'error: "stalled"',
    "ac.abort()",
    "clearInterval(watchdog)",
)


def _extract_hook_source(text: str) -> str:
    """Pull the BACKOFF_MS/STALL_MS constants + useEventStream body out of index.html."""
    # The block runs from `const BACKOFF_MS` up to the `}` that closes
    # useEventStream — the next top-level `function renderMarkdown(` line.
    m = re.search(
        r"(const BACKOFF_MS[^\n]*\n(?:.*\n)*?^}\n)(?=\n// |\nfunction renderMarkdown)",
        text,
        re.MULTILINE,
    )
    if not m:
        raise AssertionError("useEventStream block not found in index.html")
    return m.group(1)


def test_sse_watchdog_markers_present():
    """Guard: every marker that encodes the watchdog fix is on disk."""
    text = INDEX.read_text()
    missing = [marker for marker in REQUIRED_MARKERS if marker not in text]
    assert not missing, f"missing watchdog markers in index.html: {missing}"


def test_sse_watchdog_fires_and_aborts(tmp_path):
    """Behavioural: a hung reader must trigger stalled-dispatch + abort."""
    text = INDEX.read_text()
    hook = _extract_hook_source(text)

    # Shrink the timers so the test runs in <1s instead of 35s.
    hook_fast = hook.replace("const STALL_MS = 35000", "const STALL_MS = 60")
    hook_fast = hook_fast.replace(
        "const WATCHDOG_INTERVAL_MS = 5000", "const WATCHDOG_INTERVAL_MS = 20"
    )
    assert "STALL_MS = 60" in hook_fast, "constant substitution failed"

    harness = textwrap.dedent(
        """\
        // Shims for the React-hook environment the embedded code assumes.
        const dispatched = [];
        const dispatch = (ev) => dispatched.push(ev);
        let abortCalled = 0;
        globalThis.KEY = "test-key";
        globalThis.makeSseParser = () => () => {};
        globalThis.useRef = (initial) => ({current: initial});
        // useEffect: run the effect synchronously; ignore the cleanup
        // for this test — we assert the watchdog's own cleanup path.
        globalThis.useEffect = (fn) => { fn(); };

        // Mock fetch: return a reader that yields one chunk, then hangs
        // forever. The ac.abort() from the watchdog surfaces as an
        // AbortError on the pending read; the catch arm in the hook
        // swallows it, enters the backoff path, and the test ends.
        let firstRead = true;
        const body = {
          getReader() {
            return {
              async read() {
                if (firstRead) {
                  firstRead = false;
                  return {value: new Uint8Array([58, 32, 112, 105, 110, 103, 10]), done: false};  // ": ping\\n"
                }
                return await new Promise(() => {});  // hang forever
              },
            };
          },
        };
        globalThis.fetch = async () => ({ok: true, status: 200, body});

        // Observe ac.abort() by wrapping AbortController.
        const RealAC = globalThis.AbortController;
        globalThis.AbortController = class extends RealAC {
          abort(...args) {
            abortCalled += 1;
            return super.abort(...args);
          }
        };

        __HOOK_SOURCE__

        // Run the hook — useEffect-shim executes `connect()` immediately.
        useEventStream(dispatch);

        // Give the watchdog > STALL_MS + a few interval ticks to fire.
        await new Promise((res) => setTimeout(res, 400));

        const stalled = dispatched.find(
          (ev) => ev && ev.type === "__status" && ev.connected === false && ev.error === "stalled"
        );

        const connected = dispatched.find(
          (ev) => ev && ev.type === "__status" && ev.connected === true
        );

        process.stdout.write(JSON.stringify({
          connectedSeen: !!connected,
          stalledSeen: !!stalled,
          abortCalled,
          dispatched: dispatched.slice(0, 10),
        }));
        """
    ).replace("__HOOK_SOURCE__", hook_fast)

    script = tmp_path / "run.mjs"
    script.write_text(f"await (async () => {{\n{harness}\n}})();\n")

    proc = subprocess.run(
        ["node", str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, f"node failed:\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    result = json.loads(proc.stdout)
    assert result["connectedSeen"], f"never dispatched connected:true: {result}"
    assert result["stalledSeen"], f"watchdog never fired stalled dispatch: {result}"
    assert result["abortCalled"] >= 1, f"ac.abort() was never called: {result}"
