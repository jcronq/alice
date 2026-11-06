"""Unit tests for the pure chat-composer helpers in ``static/chat-ux.js`` (#655).

Run under node (ES module, no deps); skipped when node is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from status_site.server import STATIC_DIR

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

SCRIPT = r"""
import * as ux from %(mod)s;
const out = {};
const key = (o) => ({ shouldSubmitOnKey: ux.shouldSubmitOnKey(o) });
out.enter = ux.shouldSubmitOnKey({key: "Enter", shiftKey: false, isComposing: false, keyCode: 13});
out.shiftEnter = ux.shouldSubmitOnKey({key: "Enter", shiftKey: true, isComposing: false, keyCode: 13});
out.ime = ux.shouldSubmitOnKey({key: "Enter", shiftKey: false, isComposing: true, keyCode: 229});
out.imeKeyCode = ux.shouldSubmitOnKey({key: "Enter", shiftKey: false, isComposing: false, keyCode: 229});
out.letter = ux.shouldSubmitOnKey({key: "a", shiftKey: false});

const png = {name: "image.png", type: "image/png"};
const items = [
  {kind: "string", type: "text/plain", getAsFile: () => null},
  {kind: "file", type: "image/png", getAsFile: () => png},
  {kind: "file", type: "application/zip", getAsFile: () => ({name: "x.zip"})},
];
out.pasted = ux.imageFilesFromClipboard({items}).map(f => f.name);
out.pastedNone = ux.imageFilesFromClipboard({items: [items[0]]}).length;
out.pastedNull = ux.imageFilesFromClipboard(null).length;

const when = new Date("2026-03-04T05:06:07.890Z");
out.nameGeneric = ux.attachmentName(png, 0, when);
out.nameSecond = ux.attachmentName(png, 1, when);
out.nameJpeg = ux.attachmentName({name: "", type: "image/jpeg"}, 0, when);
out.nameKept = ux.attachmentName({name: "report.pdf", type: "application/pdf"});

const el = (top, h, c) => ({scrollTop: top, scrollHeight: h, clientHeight: c});
out.atBottom = ux.isNearBottom(el(500, 1000, 500));
out.near = ux.isNearBottom(el(461, 1000, 500));
out.scrolledUp = ux.isNearBottom(el(100, 1000, 500));

out.tooMany = ux.validateAttachment({name: "a.png", size: 1}, ux.MAX_FILES);
out.tooBig = ux.validateAttachment({name: "a.png", size: ux.MAX_PER_FILE + 1}, 0);
out.ok = ux.validateAttachment({name: "a.png", size: 10}, 0);

out.nearTop = ux.isNearTop({scrollTop: 150});
out.notNearTop = ux.isNearTop({scrollTop: 400});
out.rowKeyMsg = ux.chatRowKey({from: "jason", at: "t1", text: "x"});
out.rowKeyGroup = ux.chatRowKey({variant: "working-group", turn_id: "T"});
out.earliest = ux.earliestAt([
  {variant: "working-group", at: "2026-11-06T00:00:00+00:00"},
  {from: "a", at: "2026-11-06T02:00:00+00:00"},
  {from: "b", at: "2026-11-06T01:00:00+00:00"},
  {from: "c", at: "garbage"},
]);
out.earliestNone = ux.earliestAt([{variant: "working-group", at: "2026-11-06T00:00:00+00:00"}]);

globalThis.FileReader = class {
  readAsDataURL(f) { this.result = "data:image/png;base64,UE5H"; setTimeout(() => this.onload(), 0); }
};
out.b64 = await ux.readAsBase64({});
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    mod = json.dumps((STATIC_DIR / "chat-ux.js").as_uri())
    script = tmp_path_factory.mktemp("node") / "t.mjs"
    script.write_text(SCRIPT % {"mod": mod})
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_enter_sends_but_shift_enter_and_ime_do_not(result):
    assert result["enter"] is True
    assert result["shiftEnter"] is False
    assert result["ime"] is False
    assert result["imeKeyCode"] is False
    assert result["letter"] is False


def test_paste_extracts_only_image_blobs(result):
    assert result["pasted"] == ["image.png"]
    assert result["pastedNone"] == 0
    assert result["pastedNull"] == 0


def test_pasted_images_get_unique_allowlisted_names(result):
    assert result["nameGeneric"] == "pasted-20260304T050607.png"
    assert result["nameSecond"] == "pasted-20260304T050607-1.png"
    assert result["nameJpeg"] == "pasted-20260304T050607.jpg"
    assert result["nameKept"] == "report.pdf"


def test_sticky_bottom_threshold(result):
    assert result["atBottom"] is True
    assert result["near"] is True
    assert result["scrolledUp"] is False


def test_attachment_validation_and_base64(result):
    assert "At most" in result["tooMany"]
    assert "over" in result["tooBig"]
    assert result["ok"] == ""
    assert result["b64"] == "UE5H"


def test_chat_thread_scrolls_to_bottom_on_mount():
    html = (Path(STATIC_DIR) / "index.html").read_text()
    body = html[html.index("function ChatThread") : html.index("function renderChatMessages")]
    assert "el.scrollTop = el.scrollHeight" in body
    assert "useRef(true)" in body and "isNearBottom" in body


def test_load_older_helpers(result):
    assert result["nearTop"] is True
    assert result["notNearTop"] is False
    assert result["rowKeyMsg"] == "jason|t1|x"
    assert result["rowKeyGroup"] == "wg:T"
    assert result["earliest"] == "2026-11-06T01:00:00+00:00"
    assert result["earliestNone"] is None


def test_chat_thread_wires_infinite_scroll():
    html = (Path(STATIC_DIR) / "index.html").read_text()
    body = html[html.index("function ChatThread") : html.index("function renderChatMessages")]
    assert "/api/chat?before=" in body
    assert "loadingRef" in body and "isNearTop" in body
    assert "loading older" in body
    assert "useLayoutEffect" in body and "anchor" in body
