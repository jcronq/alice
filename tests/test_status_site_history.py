"""Item-history tracker, /api/history, and the alice-status-drop extra/<slug> receiver."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from status_site import history as H
from status_site.server import Config, make_server, slugify

RECEIVER = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "claude-code-bridge"
    / "aimax1"
    / "alice-status-drop"
)

PAGE = """# Overview

## Running now

- [ ] ⏳ **In-game bug reports**: *being built*
- [x] **Wave A staging**: live at staging
  continued on the next line
- [ ] ⏳ [**Howl Wave A build**](?p=wave-a) (Bazzite): chapters in
  - ~~**Ch7B (the first night)**~~ ✅ *rescued*
  - plain child without a title

  Then the gate. [Live →](?p=wave-a)
- [ ] no bold here

```
- [ ] **Not an item**: inside a fence
```

## Waiting on you

- [ ] **PFL: confirm the payout.** *Approval letter*
1. **Numbered**: works too
"""


def items(md, page="index"):
    return {i.slug: i for i in H.parse_page(md, page)}


# ---------------------------------------------------------------- parser


def test_parse_titles_checkboxes_sections():
    it = items(PAGE)
    assert list(it) == [
        "in-game-bug-reports",
        "wave-a-staging",
        "howl-wave-a-build",
        "ch7b-the-first-night",
        "pfl-confirm-the-payout",
        "numbered",
    ]
    assert it["in-game-bug-reports"].checked is False
    assert it["in-game-bug-reports"].text == "⏳ *being built*"
    assert it["wave-a-staging"].checked is True
    assert it["wave-a-staging"].text == "live at staging continued on the next line"
    assert it["ch7b-the-first-night"].checked is None
    assert it["ch7b-the-first-night"].title == "Ch7B (the first night)"
    assert it["ch7b-the-first-night"].text == "✅ *rescued*"
    assert it["numbered"].checked is None
    assert it["pfl-confirm-the-payout"].section == "Waiting on you"
    assert it["in-game-bug-reports"].section == "Running now"


def test_parse_parent_keeps_own_text_and_indented_paragraph():
    h = items(PAGE)["howl-wave-a-build"]
    # Link wrapper dropped; children excluded; the indented paragraph after a blank joins.
    assert h.text == "⏳ (Bazzite): chapters in Then the gate. [Live →](?p=wave-a)"
    assert "rescued" not in h.text


def test_slug_matches_frontend_and_server():
    # Frontend: slug(st.textContent.replace(/[:.]+$/,'')); server: slugify().
    for title in [
        "PFL: confirm the payout.",
        "CH06B-the-hearth-and-packing",
        "Kingsbury (4A)",
    ]:
        assert H.title_slug(title) == slugify(title.rstrip(":."))
    assert (
        H.title_slug("CH06B-the-hearth-and-packing") == "ch06b-the-hearth-and-packing"
    )


# ---------------------------------------------------------------- diff + tracker


@pytest.fixture
def status(tmp_path):
    d = tmp_path / "status"
    d.mkdir()
    return d


def history(status, slug):
    return H.read_jsonl(status / "item-history" / f"{slug}.jsonl")


def test_seed_then_changes(status):
    (status / "index.md").write_text(
        "## Running now\n\n- [ ] **Alpha**: first\n- [ ] **Beta**: b\n"
    )
    (status / ".hidden.md").write_text("- [ ] **Ignored**: dotfile\n")
    t = H.Tracker(status)
    ev = t.step(at="2026-10-27T10:00:00-04:00")
    assert sorted(s for s, _ in ev) == ["alpha", "beta"]
    assert all(e["type"] == "appeared" and e["seeded"] for _, e in ev)
    assert (status / ".item-state.json").is_file()

    # A quiet rescan writes nothing.
    assert t.step() == []

    (status / "index.md").write_text(
        "## Running now\n\n- [x] **Alpha**: first, now done\n- [ ] **Beta**: b\n"
        "- [ ] **Gamma**: new\n"
    )
    ev = t.step(at="2026-10-27T10:01:00-04:00")
    kinds = [(s, e["type"]) for s, e in ev]
    assert ("alpha", "checked") in kinds and ("alpha", "text_changed") in kinds
    gamma = [e for s, e in ev if s == "gamma"]
    assert gamma[0]["type"] == "appeared" and "seeded" not in gamma[0]
    assert gamma[0]["section"] == "Running now" and gamma[0]["page"] == "index"
    tc = next(e for s, e in ev if e["type"] == "text_changed")
    assert tc["old"] == "first" and tc["new"] == "first, now done"

    # Move Alpha to a new section and uncheck it.
    (status / "index.md").write_text(
        "## Running now\n\n- [ ] **Beta**: b\n- [ ] **Gamma**: new\n\n"
        "## Done today\n\n- [ ] **Alpha**: first, now done\n"
    )
    ev = t.step()
    a = [e for s, e in ev if s == "alpha"]
    assert [e["type"] for e in a] == ["moved", "unchecked"]
    assert a[0]["from"] == {"page": "index", "section": "Running now"}
    assert a[0]["to"] == {"page": "index", "section": "Done today"}

    # Moving to another page is also a move.
    (status / "index.md").write_text(
        "## Running now\n\n- [ ] **Beta**: b\n- [ ] **Gamma**: new\n"
    )
    (status / "other.md").write_text(
        "## Done today\n\n- [ ] **Alpha**: first, now done\n"
    )
    ev = t.step()
    assert [(s, e["type"]) for s, e in ev] == [("alpha", "moved")]
    assert ev[0][1]["to"]["page"] == "other"

    alpha = history(status, "alpha")
    assert [e["type"] for e in alpha] == [
        "appeared",
        "checked",
        "text_changed",
        "moved",
        "unchecked",
        "moved",
    ]
    assert alpha[0]["seeded"] is True and alpha[0]["at"] == "2026-10-27T10:00:00-04:00"


def test_remove_needs_two_scans_and_readd_is_not_seeded(status):
    (status / "index.md").write_text("- [ ] **Alpha**: a\n- [ ] **Beta**: b\n")
    t = H.Tracker(status)
    t.step()
    (status / "index.md").write_text("- [ ] **Beta**: b\n")
    assert t.step() == []  # first miss: grace
    ev = t.step()
    assert [(s, e["type"]) for s, e in ev] == [("alpha", "removed")]
    assert t.step() == []  # tombstoned, no repeat
    (status / "index.md").write_text("- [ ] **Alpha**: a\n- [ ] **Beta**: b\n")
    ev = t.step()
    assert [(s, e["type"]) for s, e in ev] == [("alpha", "appeared")]
    assert "seeded" not in ev[0][1]
    assert [e["type"] for e in history(status, "alpha")] == [
        "appeared",
        "removed",
        "appeared",
    ]


def test_transient_disappearance_is_ignored(status):
    (status / "index.md").write_text("- [ ] **Alpha**: a\n")
    t = H.Tracker(status)
    t.step()
    (status / "index.md").write_text("")  # half-written page
    assert t.step() == []
    (status / "index.md").write_text("- [ ] **Alpha**: a\n")
    assert t.step() == []
    assert [e["type"] for e in history(status, "alpha")] == ["appeared"]


def test_now_iso_has_colon_offset():
    s = H.now_iso()
    assert len(s) == 25 and s[19] in "+-" and s[22] == ":"


# ---------------------------------------------------------------- /api/history


@pytest.fixture
def site(tmp_path):
    st = tmp_path / "status"
    (st / "threads").mkdir(parents=True)
    (st / "item-extra").mkdir()
    (st / "index.md").write_text("## Done\n\n- [x] **Design agreed**: yes\n")
    (st / "threads" / "design-agreed.jsonl").write_text(
        json.dumps({"from": "jason", "at": "2026-10-27T21:00:00-0400", "text": "ok?"})
        + "\n"
    )
    (st / "item-extra" / "design-agreed.jsonl").write_text(
        json.dumps(
            {
                "type": "commit",
                "sha": "abc123",
                "at": "2026-10-27T20:00:00-04:00",
                "subject": "feat: bug",
            }
        )
        + "\nnot json\n"
    )
    H.Tracker(st).step()
    cfg = Config(
        status_dir=st,
        surface_dir=tmp_path / "s",
        upload_dir=tmp_path / "u",
        key="k",
        port=0,
        host="127.0.0.1",
    )
    srv = make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield st, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    out = r.status, r.read()
    c.close()
    return out


def test_api_history_shape(site):
    _, port = site
    code, body = get(port, "/api/history?slug=design-agreed")
    assert code == 200
    d = json.loads(body)
    assert set(d) == {"slug", "events", "thread", "extra"}
    assert d["events"][0]["type"] == "appeared" and d["events"][0]["seeded"] is True
    assert d["events"][0]["section"] == "Done" and d["events"][0]["checked"] is True
    assert d["thread"][0]["text"] == "ok?"
    assert d["extra"] == [
        {
            "type": "commit",
            "sha": "abc123",
            "at": "2026-10-27T20:00:00-04:00",
            "subject": "feat: bug",
        }
    ]
    code, body = get(port, "/api/history?slug=nothing-here")
    assert code == 200 and json.loads(body) == {
        "slug": "nothing-here",
        "events": [],
        "thread": [],
        "extra": [],
    }


def test_api_history_slug_sanitized(site):
    st, port = site
    (st.parent / "secret.jsonl").write_text('{"leak": true}\n')
    for bad in ["../secret", "..%2F..%2Fsecret", "%2E%2E/secret", ""]:
        code, body = get(port, "/api/history?slug=" + bad)
        assert b"leak" not in body
        assert code in (200, 400)
    assert get(port, "/api/history")[0] == 400
    # Private tracker state is never served as content.
    assert get(port, "/.item-state.json")[0] == 404
    assert get(port, "/item-history/design-agreed.jsonl")[0] == 404


# ---------------------------------------------------------------- receiver


def drop(tmp_path, cmd, data: bytes):
    env = {
        **os.environ,
        "ALICE_MIND": str(tmp_path / "mind"),
        "SSH_ORIGINAL_COMMAND": cmd,
    }
    (tmp_path / "mind" / "inner" / "status").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(RECEIVER)],
        input=data,
        env=env,
        capture_output=True,
        timeout=10,
    )


def test_receiver_writes_extra(tmp_path):
    rows = b'{"type":"commit","sha":"abc123","subject":"x"}\n{"type":"commit","sha":"def456"}\n'
    r = drop(tmp_path, "extra/ch06b-the-hearth-and-packing", rows)
    assert r.returncode == 0, r.stderr
    out = tmp_path / "mind/inner/status/item-extra/ch06b-the-hearth-and-packing.jsonl"
    assert out.read_bytes() == rows
    assert out.stat().st_mode & 0o777 == 0o644
    # Replaced whole on the next push.
    assert (
        drop(tmp_path, "extra/ch06b-the-hearth-and-packing", b'{"a":1}\n').returncode
        == 0
    )
    assert out.read_text() == '{"a":1}\n'


@pytest.mark.parametrize(
    "cmd",
    [
        "extra/../../evil",
        "extra/../index",
        "extra/a/b",
        "extra/",
        "extra/-lead",
        "extra/UPPER",
        "extra/" + "a" * 61,
        "extra/ok;rm -rf ~",
        "extra/ok\nother",
    ],
)
def test_receiver_rejects_bad_slugs(tmp_path, cmd):
    r = drop(tmp_path, cmd, b'{"a":1}\n')
    assert r.returncode != 0
    assert not (tmp_path / "mind/inner/status/item-extra").exists()
    assert not list((tmp_path / "mind").rglob("evil*"))


@pytest.mark.parametrize(
    "data",
    [
        b"not json\n",
        b'["a list"]\n',
        b"\xff\xfe\n",
        b'{"a":1}\n' * 501,
        b"x" * (64 * 1024 + 1),
    ],
)
def test_receiver_rejects_bad_bodies(tmp_path, data):
    r = drop(tmp_path, "extra/ok", data)
    assert r.returncode != 0
    assert not (tmp_path / "mind/inner/status/item-extra/ok.jsonl").exists()


def test_receiver_pages_still_work(tmp_path):
    assert drop(tmp_path, "wave-a", b"# Wave A\n").returncode == 0
    assert (tmp_path / "mind/inner/status/wave-a.md").read_text() == "# Wave A\n"
    assert drop(tmp_path, "index", b"# x\n").returncode != 0
    assert drop(tmp_path, "../x", b"# x\n").returncode != 0
