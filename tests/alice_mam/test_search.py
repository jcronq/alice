"""Tests for :mod:`alice_mam.search`.

Fixture-only — no live MAM calls. The ``_FakeOpener`` pattern mirrors
``tests/test_alice_speaking_presence.py`` so the HTTP stub stays in a
shape the codebase already understands.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from alice_mam.search import (
    Candidate,
    _extract_mam_id_from_text,
    _normalise_row,
    _parse_authors,
    _parse_filetypes,
    load_mam_cookie,
    pick_candidate,
    search_mam,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_response.json"


# ─── HTTP stubs ────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class _FakeOpener:
    def __init__(self, resp=None, exc=None, capture: dict | None = None):
        self._resp = resp
        self._exc = exc
        self._capture = capture

    def open(self, req, timeout=None):  # noqa: D401 — opener protocol
        if self._capture is not None:
            self._capture["url"] = req.full_url
            self._capture["headers"] = dict(req.header_items())
            self._capture["method"] = req.get_method()
            self._capture["body"] = req.data
            self._capture["timeout"] = timeout
        if self._exc is not None:
            raise self._exc
        return self._resp


def _fixture_body() -> bytes:
    return FIXTURE_PATH.read_bytes()


def _opener_for_fixture(capture: dict | None = None) -> _FakeOpener:
    return _FakeOpener(resp=_FakeResp(200, _fixture_body()), capture=capture)


# ─── search_mam: filter + sort ─────────────────────────────────────────


def test_search_returns_only_english_results():
    """Language filter drops ``lang_code='fra'`` even if the title matches."""
    cands = search_mam(
        "Twice Dead King Ruin",
        cookie="fake",
        opener=_opener_for_fixture(),
        emit_audit=False,
    )
    # Fixture has 5 rows; 1 French drops, 1 mp3 (audiobook), 1 cbz
    # (unsupported for ebook main_cat) drop → 2 ebook rows remain.
    assert all(c.id != 7781236 for c in cands), "French result not filtered"
    assert len(cands) == 2
    ids = {c.id for c in cands}
    assert ids == {7781234, 7781235}


def test_search_drops_filetypes_outside_ebook_allowlist():
    """``filetype=cbz`` and ``filetype=mp3`` drop for the ebook main_cat."""
    cands = search_mam(
        "Ruin",
        cookie="fake",
        opener=_opener_for_fixture(),
        emit_audit=False,
    )
    ids = {c.id for c in cands}
    assert 7781237 not in ids, "mp3 audiobook leaked into ebook results"
    assert 7781238 not in ids, "cbz scan leaked into ebook results"


def test_search_sorts_freeleech_first_then_by_seeders():
    """Freeleech candidate wins over a higher-seeder non-free candidate."""
    cands = search_mam(
        "Ruin",
        cookie="fake",
        opener=_opener_for_fixture(),
        prefer_freeleech=True,
        emit_audit=False,
    )
    # 7781234 is free with 42 seeders; 7781235 is non-free with 128
    # seeders. With prefer_freeleech, the free one must land first.
    assert cands[0].id == 7781234
    assert cands[0].free is True
    assert cands[1].id == 7781235


def test_search_without_freeleech_preference_sorts_by_seeders():
    cands = search_mam(
        "Ruin",
        cookie="fake",
        opener=_opener_for_fixture(),
        prefer_freeleech=False,
        emit_audit=False,
    )
    # 7781235 has 128 seeders, 7781234 has 42 → seeder-desc order.
    assert cands[0].id == 7781235
    assert cands[1].id == 7781234


def test_search_audiobook_main_cat_accepts_mp3():
    """main_cat=13 (AudioBooks) accepts mp3/m4b; drop ebook formats."""
    cands = search_mam(
        "Ruin",
        cookie="fake",
        main_cat=13,
        opener=_opener_for_fixture(),
        emit_audit=False,
    )
    ids = {c.id for c in cands}
    # Only the mp3 row is in the audiobook allowlist AND English; the
    # epub rows (7781234/7781235) drop under main_cat=13.
    assert ids == {7781237}


# ─── search_mam: empty / error paths ───────────────────────────────────


def test_search_empty_data_returns_empty_list():
    body = json.dumps({"data": [], "total": 0, "total_found": 0}).encode()
    cands = search_mam(
        "Ruin",
        cookie="fake",
        opener=_FakeOpener(resp=_FakeResp(200, body)),
        emit_audit=False,
    )
    assert cands == []


def test_search_non_200_returns_empty_list():
    op = _FakeOpener(resp=_FakeResp(502, b""))
    assert search_mam("Ruin", cookie="fake", opener=op, emit_audit=False) == []


def test_search_transport_error_returns_empty_list():
    op = _FakeOpener(exc=urllib.error.URLError("boom"))
    assert search_mam("Ruin", cookie="fake", opener=op, emit_audit=False) == []


def test_search_bad_json_returns_empty_list():
    op = _FakeOpener(resp=_FakeResp(200, b"not-json"))
    assert search_mam("Ruin", cookie="fake", opener=op, emit_audit=False) == []


def test_search_blank_text_short_circuits_without_opener():
    # Even with an opener that would raise, blank input returns [] before
    # any HTTP call is made.
    op = _FakeOpener(exc=urllib.error.URLError("should not be called"))
    assert search_mam("   ", cookie="fake", opener=op, emit_audit=False) == []


def test_search_sends_mam_cookie_header_and_json_body():
    cap: dict = {}
    search_mam(
        "Ruin",
        cookie="xyz-cookie-value",
        opener=_opener_for_fixture(cap),
        emit_audit=False,
    )
    headers = {k.lower(): v for k, v in cap["headers"].items()}
    assert headers.get("Cookie".lower()) == "mam_id=xyz-cookie-value"
    assert "application/json" in headers.get("Content-type".lower(), "")
    body = json.loads(cap["body"].decode())
    assert body["tor"]["text"] == "Ruin"
    assert body["tor"]["main_cat"] == [14]
    assert cap["method"] == "POST"


# ─── pick_candidate ────────────────────────────────────────────────────


def _mk(id_: int, *, free: bool, seeders: int) -> Candidate:
    return Candidate(
        id=id_,
        title=f"c-{id_}",
        seeders=seeders,
        free=free,
        dl_hash="h",
        download_url=f"https://example/{id_}",
    )


def test_pick_prefers_freeleech_over_higher_seeder_non_free():
    free_low = _mk(1, free=True, seeders=3)
    paid_high = _mk(2, free=False, seeders=999)
    chosen = pick_candidate([free_low, paid_high], require_freeleech=True)
    assert chosen is not None and chosen.id == 1


def test_pick_returns_none_when_require_freeleech_and_none_free():
    chosen = pick_candidate(
        [_mk(1, free=False, seeders=50), _mk(2, free=False, seeders=10)],
        require_freeleech=True,
    )
    assert chosen is None


def test_pick_without_freeleech_requirement_returns_first():
    # Caller controls order; pick_candidate doesn't re-sort.
    cands = [_mk(2, free=False, seeders=50), _mk(1, free=True, seeders=3)]
    chosen = pick_candidate(cands, require_freeleech=False)
    assert chosen is not None and chosen.id == 2


def test_pick_on_empty_list_returns_none():
    assert pick_candidate([], require_freeleech=True) is None
    assert pick_candidate([], require_freeleech=False) is None


# ─── Cookie loader ─────────────────────────────────────────────────────


def test_cookie_env_var_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("MAM_COOKIE", "env-cookie-value")
    missing = tmp_path / "nowhere.json"
    assert load_mam_cookie(paths=[missing]) == "env-cookie-value"


def test_cookie_env_var_accepts_prefixed_form(monkeypatch, tmp_path):
    monkeypatch.setenv("MAM_COOKIE", "mam_id=prefixed-value; path=/")
    missing = tmp_path / "nowhere.json"
    assert load_mam_cookie(paths=[missing]) == "prefixed-value"


def test_cookie_loaded_from_jackett_style_json(monkeypatch, tmp_path):
    monkeypatch.delenv("MAM_COOKIE", raising=False)
    config = tmp_path / "myanonamouse.json"
    config.write_text(
        '{"cookieheader":{"Value":"mam_id=COOKIE_FROM_FILE_abc123; path=/"}}'
    )
    assert load_mam_cookie(paths=[config]) == "COOKIE_FROM_FILE_abc123"


def test_cookie_missing_raises_with_paths_in_message(monkeypatch, tmp_path):
    monkeypatch.delenv("MAM_COOKIE", raising=False)
    nowhere = tmp_path / "nowhere.json"
    with pytest.raises(FileNotFoundError) as exc:
        load_mam_cookie(paths=[nowhere])
    assert str(nowhere) in str(exc.value)


# ─── Normaliser + parsers ──────────────────────────────────────────────


def test_normalise_row_builds_download_url():
    row = json.loads(_fixture_body())["data"][0]
    cand = _normalise_row(row)
    assert cand is not None
    assert cand.id == 7781234
    assert cand.authors == ["Nate Crowley"]
    assert "epub" in cand.filetypes
    assert cand.free is True
    assert cand.dl_hash == "AAA111FREELEECHHASH"
    assert (
        cand.download_url
        == "https://www.myanonamouse.net/tor/download.php/AAA111FREELEECHHASH?tid=7781234"
    )


def test_normalise_row_drops_rows_without_id():
    assert _normalise_row({"title": "no id"}) is None


def test_normalise_row_uses_tid_only_fallback_when_dl_missing():
    """Live MAM responses often omit ``dl``; a tid-only URL must still work."""
    cand = _normalise_row({
        "id": 761464, "title": "x", "filetype": "epub", "lang_code": "eng"
    })
    assert cand is not None
    assert cand.dl_hash == ""
    assert cand.download_url == "https://www.myanonamouse.net/tor/download.php?tid=761464"


def test_parse_int_handles_mam_human_readable_size():
    """MAM's live Search JSON returns size as e.g. "2.0 MiB"."""
    from alice_mam.search import _parse_int
    assert _parse_int("2.0 MiB") == 2 * 1024 * 1024
    assert _parse_int("550 KB") == 550_000
    assert _parse_int("1.3 GiB") == int(1.3 * 1024 ** 3)
    # Pure int-string path still works.
    assert _parse_int("123456") == 123456


def test_parse_authors_handles_json_string():
    assert _parse_authors('{"1":"Alice","2":"Bob"}') == ["Alice", "Bob"]
    assert _parse_authors("") == []
    assert _parse_authors(None) == []
    assert _parse_authors("not json") == []


def test_parse_filetypes_lowercases_and_dedupes():
    assert _parse_filetypes("EPUB mobi EPUB") == ["epub", "mobi"]


# ─── _extract_mam_id_from_text edge cases ──────────────────────────────


def test_extract_mam_id_handles_jackett_blob():
    blob = (
        '{"foo":"bar","cookieheader":{"Name":"mam","Value":'
        '"mam_id=ABCxyz%2F; path=/; HttpOnly"}}'
    )
    assert _extract_mam_id_from_text(blob) == "ABCxyz%2F"


def test_extract_mam_id_returns_none_when_missing():
    assert _extract_mam_id_from_text("no cookie here") is None


# ─── Audit log ─────────────────────────────────────────────────────────


def test_audit_log_is_written_on_real_call(tmp_path):
    log_path = tmp_path / "state" / "mam-events.jsonl"
    cands = search_mam(
        "Ruin",
        cookie="fake",
        opener=_opener_for_fixture(),
        audit_log=log_path,
        emit_audit=True,
    )
    assert cands  # sanity: fixture produced results
    assert log_path.exists()
    line = log_path.read_text().strip()
    event = json.loads(line)
    assert event["text"] == "Ruin"
    assert event["result_count"] == len(cands)
    assert event["first_result_id"] == cands[0].id
    # First candidate is the free one (prefer_freeleech default).
    assert event["chose_freeleech"] is True
