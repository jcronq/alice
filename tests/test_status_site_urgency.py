"""Tests for the urgency-rank classifier (``status_site/urgency.py``).

Covers the classifier precedence chain: explicit tag → inline bold
prefix → priority/text keywords → age bump → UNSET fallback. Also
exercises the stable sort (NOW rises above UNSET rises above TODAY,
oldest-first within a rank) and the HTML helpers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from status_site import urgency as urg


# ─── classify(): precedence ────────────────────────────────────────


def test_explicit_tag_wins_over_keywords():
    rec = {"urgency": "FYI", "text": "urgent blocking critical asap"}
    assert urg.classify(rec) == "FYI"


def test_inline_bold_prefix_sets_rank_and_wins_over_keywords():
    rec = {"text": "**NOW**: the kitchen faucet drips"}
    assert urg.classify(rec) == "NOW"


def test_inline_prefix_variants_dash_and_emdash():
    for text in (
        "**TODAY**: ship the PR",
        "**TODAY** - ship the PR",
        "**TODAY** — ship the PR",
    ):
        assert urg.classify({"text": text}) == "TODAY"


def test_priority_field_keywords_classify():
    assert urg.classify({"priority": "P0", "text": "something"}) == "NOW"
    assert urg.classify({"priority": "high", "text": "something"}) == "TODAY"
    assert urg.classify({"priority": "low", "text": "something"}) == "WHEN"
    assert urg.classify({"priority": "fyi", "text": "something"}) == "FYI"


def test_text_keyword_fallbacks_when_no_priority_field():
    assert urg.classify({"text": "please fix the ASAP build error"}) == "NOW"
    assert urg.classify({"text": "we're blocked on the deploy"}) == "TODAY"
    assert urg.classify({"text": "do this whenever"}) == "WHEN"


def test_age_bump_to_now_above_72h():
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    rec = {
        # Keyword-free so the age bump is what drives the rank.
        "text": "an old ask with vague prose",
        "created": (now - timedelta(days=5)).isoformat(),
    }
    assert urg.classify(rec, now=now) == "NOW"


def test_age_bump_to_today_above_24h():
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    rec = {
        # Deliberately keyword-free prose so the classifier falls
        # through to the age bump.
        "text": "quiet reminder prose",
        "created": (now - timedelta(hours=36)).isoformat(),
    }
    assert urg.classify(rec, now=now) == "TODAY"


def test_fresh_untagged_record_is_unset():
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    rec = {
        "text": "ambiguous untagged prose",
        "created": now.isoformat(),
    }
    assert urg.classify(rec, now=now) == "UNSET"


def test_future_dated_record_never_bumps():
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    rec = {
        "text": "scheduled",
        "created": (now + timedelta(days=5)).isoformat(),
    }
    assert urg.classify(rec, now=now) == "UNSET"


def test_malformed_timestamp_falls_through_to_unset():
    rec = {"text": "no clock", "created": "not-a-timestamp"}
    assert urg.classify(rec) == "UNSET"


# ─── sort_key(): stable rank + age ─────────────────────────────────


def test_sort_key_places_now_above_today_above_fyi():
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    records = [
        {"id": "fyi", "urgency": "FYI", "text": "x", "created": now.isoformat()},
        {"id": "today", "urgency": "TODAY", "text": "x", "created": now.isoformat()},
        {"id": "now", "urgency": "NOW", "text": "x", "created": now.isoformat()},
        {"id": "unset", "text": "x", "created": now.isoformat()},
    ]
    records.sort(key=lambda r: urg.sort_key(r, now=now))
    assert [r["id"] for r in records] == ["now", "unset", "today", "fyi"]


def test_sort_key_oldest_first_within_a_rank():
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    records = [
        {
            "id": "new",
            "urgency": "NOW",
            "text": "x",
            "created": (now - timedelta(minutes=5)).isoformat(),
        },
        {
            "id": "old",
            "urgency": "NOW",
            "text": "x",
            "created": (now - timedelta(hours=6)).isoformat(),
        },
    ]
    records.sort(key=lambda r: urg.sort_key(r, now=now))
    assert [r["id"] for r in records] == ["old", "new"]


# ─── helpers ───────────────────────────────────────────────────────


def test_pill_html_emits_fx_urg_span():
    assert urg.pill_html("NOW") == '<span class="fx-urg NOW">NOW</span>'
    assert urg.pill_html("Today") == '<span class="fx-urg TODAY">TODAY</span>'
    # Unknown rank defaults to UNSET so callers can't leak garbage into
    # the DOM.
    assert urg.pill_html("gibberish") == '<span class="fx-urg UNSET">UNSET</span>'


def test_blocked_mark_html_scrubs_label():
    assert urg.blocked_mark_html() == '<span class="fx-mark blocked">blocked</span>'
    assert (
        urg.blocked_mark_html("waiting on #641")
        == '<span class="fx-mark blocked">waiting on 641</span>'
    )


def test_parse_inline_tag_strips_prefix():
    rank, body = urg.parse_inline_tag("**TODAY**: ship the PR")
    assert rank == "TODAY"
    assert body == "ship the PR"


def test_parse_inline_tag_leaves_text_untouched_when_no_tag():
    rank, body = urg.parse_inline_tag("no tag here")
    assert rank is None
    assert body == "no tag here"


def test_normalize_rank_collapses_when_variants():
    assert urg.normalize_rank("WHEN READY") == "WHEN"
    assert urg.normalize_rank("when convenient") == "WHEN"
    assert urg.normalize_rank("NoW") == "NOW"
    assert urg.normalize_rank("") is None
    assert urg.normalize_rank(None) is None
    assert urg.normalize_rank("gibberish") is None
