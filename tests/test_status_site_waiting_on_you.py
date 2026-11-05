"""Tests for the Waiting-on-you source (hand-zone + structured asks).

Covers:

* Hand-zone content rendered verbatim, with structured asks under it.
* Resolved asks are excluded (they land under Done-today).
* Unresolved asks are ordered oldest-first.
* Missing hand-zone file degrades gracefully to just the structured
  list.
* Fully empty case shows a "nothing waiting" placeholder but still
  carries a staleness footer.
"""

from __future__ import annotations


from status_site.sources import waiting_on_you as woy


def test_hand_zone_and_structured_concatenated(tmp_path):
    hand = tmp_path / "waiting-on-you.md"
    hand.write_text(
        "Hey — can you bless the pricing redesign this week?\n\n"
        "Also: should we merge #628 before #638?\n"
    )
    records = [
        {
            "id": "a",
            "text": "decide SM v2 cutover date",
            "created": "2026-11-01T00:00:00+00:00",
            "resolved": False,
        },
        {
            "id": "b",
            "text": "review new persona rules",
            "created": "2026-11-03T00:00:00+00:00",
            "resolved": False,
        },
    ]
    out = woy.render(hand, records)
    # Hand-zone appears first.
    hand_idx = out.index("Hey — can you bless")
    struct_idx = out.index("- [ ] decide SM v2")
    assert hand_idx < struct_idx
    # Older ask first.
    assert out.index("- [ ] decide SM v2") < out.index(
        "- [ ] review new persona"
    )
    # Staleness footer present.
    assert out.rstrip().splitlines()[-1].startswith("*last_rendered:")


def test_resolved_asks_excluded(tmp_path):
    hand = tmp_path / "waiting-on-you.md"
    hand.write_text("")
    records = [
        {
            "id": "a",
            "text": "still pending",
            "created": "2026-11-01T00:00:00+00:00",
            "resolved": False,
        },
        {
            "id": "b",
            "text": "already done",
            "created": "2026-11-02T00:00:00+00:00",
            "resolved": True,
            "resolved_at": "2026-11-03T12:00:00+00:00",
        },
    ]
    out = woy.render(hand, records)
    assert "still pending" in out
    assert "already done" not in out


def test_missing_hand_zone_file_degrades_gracefully(tmp_path):
    records = [
        {"id": "a", "text": "still here", "created": "2026-11-01T00:00:00+00:00",
         "resolved": False},
    ]
    out = woy.render(tmp_path / "does-not-exist.md", records)
    assert "still here" in out
    # Even without a hand-zone, there's no leading-whitespace garbage.
    assert not out.startswith("\n\n")


def test_empty_input_shows_placeholder_and_footer(tmp_path):
    out = woy.render(tmp_path / "missing.md", [])
    assert "nothing waiting" in out
    assert "last_rendered" in out
    assert "source: asks-jsonl" in out


def test_unresolved_asks_sorted_oldest_first():
    records = [
        {"id": "z", "text": "newer", "created": "2026-11-04T00:00:00+00:00",
         "resolved": False},
        {"id": "a", "text": "oldest", "created": "2026-01-01T00:00:00+00:00",
         "resolved": False},
        {"id": "m", "text": "middle", "created": "2026-05-01T00:00:00+00:00",
         "resolved": False},
    ]
    pending = woy.unresolved_asks(records)
    assert [r["id"] for r in pending] == ["a", "m", "z"]


def test_render_from_asks_path_reads_jsonl(tmp_path):
    hand = tmp_path / "waiting-on-you.md"
    hand.write_text("")
    asks_path = tmp_path / "asks.jsonl"
    asks_path.write_text(
        '{"id":"a","text":"first","created":"2026-11-01T00:00:00+00:00","resolved":false}\n'
        '{"id":"b","text":"second","created":"2026-11-02T00:00:00+00:00","resolved":false}\n'
    )
    out = woy.render(hand, asks_path=asks_path)
    assert "- [ ] first" in out
    assert "- [ ] second" in out
