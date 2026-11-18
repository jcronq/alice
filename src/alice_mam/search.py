"""Read-only MAM (MyAnonamouse) JSON Search API wrapper — Slice 1.

MAM runs a private tracker with a strict ASN-locked session cookie
(``mam_id``). This module hits the public JSON Search endpoint and
returns normalised :class:`Candidate` rows for a free-text query.

Slice-1 invariants (bright lines, enforced by review):

* **Read-only.** No download fetch, no torrent handoff, no cookie
  refresh. The returned ``download_url`` is a *reference*, not an
  action — callers in later slices will decide when to pull it.
* **Fail-soft.** Non-200 / JSON parse / empty-data all return ``[]``
  with a log line on stderr. Exceptions never cross the API boundary,
  because the only consumer so far is a Signal command path that
  should quietly say "no match" rather than ping Jason on noise.
* **Audit everything.** Every real call writes one JSON line to
  ``~/alice-mind/inner/state/mam-events.jsonl`` so Jason can see the
  tracker interaction surface over time.
* **No live network in tests.** The fixture-based tests inject an
  ``opener`` and never touch the real endpoint — Jason's ratio must
  not be at risk from an exploratory curl.

Stdlib only (``urllib.request`` + ``json``), matching the pattern in
:mod:`alice_speaking.infra.presence` and
:mod:`alice_forge.dispatcher.verify`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


# ─── Protocol constants ────────────────────────────────────────────────

#: MAM JSON Search endpoint. Documented at the member docs page Jason
#: linked in the slice-1 briefing; lives on the main site host.
SEARCH_URL: str = "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"

#: Per-user, cookie-less download URL template. MAM issues a per-row
#: ``dl`` hash when the response includes it; this URL is safe to hand
#: off to qBit later without mounting the session cookie.
DOWNLOAD_URL_TEMPLATE: str = "https://www.myanonamouse.net/tor/download.php/{dl}?tid={tid}"

#: tid-only fallback: when the MAM search response omits the ``dl``
#: hash (observed live for the loadSearchJSONbasic endpoint on some
#: rows / account states), the cookied endpoint at ``?tid=`` still
#: returns the .torrent bytes. The cookie is required for this one,
#: so it's not safely handoff-able to Jackett — but Slice 2's acquire
#: step holds the cookie anyway.
DOWNLOAD_URL_TID_ONLY_TEMPLATE: str = "https://www.myanonamouse.net/tor/download.php?tid={tid}"

#: MAM ``main_cat`` enum. 14 = E-Books is the default — most of
#: Jason's requests are ebook titles.
MAIN_CAT_AUDIOBOOKS: int = 13
MAIN_CAT_EBOOKS: int = 14
MAIN_CAT_MUSICOLOGY: int = 15
MAIN_CAT_RADIO: int = 16

#: Per-``main_cat`` filetype allowlist. Anything outside drops. Values
#: are compared lowercased against the ``filetype`` substring list on
#: the MAM response row.
ALLOWED_FILETYPES_BY_MAIN_CAT: dict[int, frozenset[str]] = {
    MAIN_CAT_EBOOKS: frozenset({"epub", "pdf", "mobi"}),
    MAIN_CAT_AUDIOBOOKS: frozenset({"mp3", "m4b"}),
    # Unknown main_cat: allow everything through so the caller can
    # still inspect; downstream slices will tighten.
}

#: Default audit log target. Written one JSON line per real call.
_DEFAULT_EVENT_LOG = Path.home() / "alice-mind" / "inner" / "state" / "mam-events.jsonl"

#: HTTP timeout, seconds. MAM's search handler is usually sub-second
#: on an in-ASN request; the padding is for the slow-day case.
DEFAULT_TIMEOUT_S: float = 8.0

#: Cookie search path order. First readable match wins.
_COOKIE_SOURCE_PATHS: tuple[Path, ...] = (
    Path(
        os.environ.get(
            "MAM_COOKIE_FILE",
            str(Path.home() / "alice-mind" / "inner" / "state" / "mam-cookie"),
        )
    ),
    # Jackett's indexer config (on the media server). The alice
    # container does not normally mount this; it's listed so a local
    # run on 10.20.30.52 can still load the cookie without wiring.
    Path("/home/cronqj/media/jackett-priv/config/Jackett/Indexers/myanonamouse.json"),
    Path("/mnt/plex/downloads/config-jackett-priv/Jackett/Indexers/myanonamouse.json"),
)


# ─── Normalised row ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """A single normalised MAM search hit.

    Field order intentionally groups identity first, then user-facing
    descriptors, then flags, then the download handle. ``download_url``
    is precomputed so a caller doesn't need to know the URL shape.
    """

    id: int
    title: str
    authors: list[str] = field(default_factory=list)
    seeders: int = 0
    size_bytes: int = 0
    filetypes: list[str] = field(default_factory=list)
    free: bool = False
    personal_freeleech: bool = False
    my_snatched: bool = False
    dl_hash: str = ""
    download_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Cookie loader ─────────────────────────────────────────────────────


def _extract_mam_id_from_text(text: str) -> str | None:
    """Pull the ``mam_id=...`` cookie value from free-form text.

    Jackett stores it inside a larger JSON config under a field like
    ``cookieheader``. We don't need to parse the whole config shape —
    the token is always formatted as ``mam_id=<urlencoded>`` on a
    single contiguous run.
    """
    needle = "mam_id="
    idx = text.find(needle)
    if idx < 0:
        return None
    rest = text[idx + len(needle):]
    end = len(rest)
    for i, ch in enumerate(rest):
        # The cookie body is URL-encoded, so any of these terminates
        # it on the wire: whitespace, ``"`` (JSON string boundary),
        # ``;`` (next cookie), ``,`` (JSON array), ``\``, newline.
        if ch in (' ', '"', "'", ';', ',', '\\', '\n', '\r', '\t'):
            end = i
            break
    value = rest[:end].strip()
    return value or None


def load_mam_cookie(
    *,
    env_var: str = "MAM_COOKIE",
    paths: Iterable[Path] | None = None,
) -> str:
    """Return the current MAM session cookie value (no ``mam_id=`` prefix).

    Resolution order:
      1. ``$MAM_COOKIE`` environment variable (highest priority — lets
         Jason override on a one-off without touching disk).
      2. Each path in :data:`_COOKIE_SOURCE_PATHS` (override via
         ``paths=``). Raw ``mam_id`` text or Jackett-style JSON both
         work; the extractor is lenient.

    Raises :class:`FileNotFoundError` with every path we looked at so
    the error message is actionable.
    """
    env_val = os.environ.get(env_var, "").strip()
    if env_val:
        # Accept either raw value or ``mam_id=...`` form.
        extracted = _extract_mam_id_from_text(env_val)
        return extracted if extracted else env_val

    candidate_paths = tuple(paths) if paths is not None else _COOKIE_SOURCE_PATHS
    tried: list[str] = []
    for p in candidate_paths:
        tried.append(str(p))
        try:
            text = p.read_text()
        except (FileNotFoundError, PermissionError, IsADirectoryError, OSError):
            continue
        value = _extract_mam_id_from_text(text)
        if value:
            return value

    raise FileNotFoundError(
        "mam_id cookie not found. Set $MAM_COOKIE or place the cookie "
        "value at one of: " + ", ".join(tried)
    )


# ─── Audit log ─────────────────────────────────────────────────────────


def _emit_audit_event(
    *,
    text: str,
    main_cat: int,
    result_count: int,
    first_result_id: int | None,
    chose_freeleech: bool,
    log_path: Path = _DEFAULT_EVENT_LOG,
) -> None:
    """Append one JSON line describing a real MAM call.

    Fail-soft: if the log path can't be written (missing parent in an
    unusual environment, FS full, etc.) we log to stderr but never
    raise — the caller's result is still valid.
    """
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "text": text,
        "main_cat": main_cat,
        "result_count": result_count,
        "first_result_id": first_result_id,
        "chose_freeleech": chose_freeleech,
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError as exc:
        print(
            f"alice_mam: audit log write failed ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )


# ─── Parsing helpers ──────────────────────────────────────────────────


def _parse_authors(raw: Any) -> list[str]:
    """MAM returns ``author_info`` as a JSON-encoded string of ``{id: name}``.

    Handles the common degenerate shapes (empty string, None, already-
    dict) without raising.
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [str(v) for v in raw.values() if v]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, dict):
            return [str(v) for v in parsed.values() if v]
    return []


def _parse_filetypes(raw: Any) -> list[str]:
    """``filetype`` is a space-separated string like ``"epub mobi"``.

    Lowercased and deduped (preserving order) so downstream set
    operations are stable.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = [str(x).lower() for x in raw if x]
    else:
        parts = [p.lower() for p in str(raw).split() if p]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


#: Binary size units MAM uses in the "2.0 MiB" string form.
_SIZE_UNITS: dict[str, int] = {
    "b": 1,
    "kib": 1024,
    "mib": 1024 ** 2,
    "gib": 1024 ** 3,
    "tib": 1024 ** 4,
    "kb": 1000,
    "mb": 1000 ** 2,
    "gb": 1000 ** 3,
    "tb": 1000 ** 4,
}


def _parse_int(raw: Any, default: int = 0) -> int:
    """MAM mixes int and str-of-int for ``size``, ``seeders`` etc.

    Also accepts the human-readable size form live MAM uses on the
    JSON Search endpoint (``"2.0 MiB"``) — fixture had raw bytes, but
    the live response ships formatted strings for ``size``.
    """
    if isinstance(raw, bool):  # bool is int — reject explicitly
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        try:
            return int(s)
        except (ValueError, AttributeError):
            pass
        # "2.0 MiB" / "550 KB" / "1.3 GiB" form
        parts = s.split()
        if len(parts) == 2:
            num_s, unit_s = parts
            unit = unit_s.lower()
            mult = _SIZE_UNITS.get(unit)
            if mult is not None:
                try:
                    return int(float(num_s) * mult)
                except (ValueError, TypeError):
                    return default
        return default
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_bool(raw: Any) -> bool:
    """MAM returns bool flags as actual bools, but some endpoints use ``"1"``."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "y")
    return False


def _normalise_row(row: dict[str, Any]) -> Candidate | None:
    """Turn a raw MAM response row into a :class:`Candidate`. ``None`` on skip."""
    tid = _parse_int(row.get("id"), default=0)
    if tid <= 0:
        return None
    dl_hash = str(row.get("dl") or "").strip()
    if dl_hash:
        download_url = DOWNLOAD_URL_TEMPLATE.format(dl=dl_hash, tid=tid)
    else:
        # Live MAM responses commonly omit ``dl``. The cookied tid-only
        # endpoint still delivers the .torrent bytes — Slice 2's acquire
        # path mounts the cookie anyway.
        download_url = DOWNLOAD_URL_TID_ONLY_TEMPLATE.format(tid=tid)
    return Candidate(
        id=tid,
        title=str(row.get("title") or "").strip(),
        authors=_parse_authors(row.get("author_info")),
        seeders=_parse_int(row.get("seeders"), default=0),
        size_bytes=_parse_int(row.get("size"), default=0),
        filetypes=_parse_filetypes(row.get("filetype")),
        free=_parse_bool(row.get("free")) or _parse_bool(row.get("fl_vip")),
        personal_freeleech=_parse_bool(row.get("personal_freeleech")),
        my_snatched=_parse_bool(row.get("my_snatched")),
        dl_hash=dl_hash,
        download_url=download_url,
    )


# ─── Filtering + sorting ──────────────────────────────────────────────


def _language_matches(row: dict[str, Any], want: str) -> bool:
    """Check language against the 3-letter ``lang_code`` field.

    Falls back to ``True`` when the row lacks a code — MAM's e-book
    corpus is English-dominant and missing values are common on older
    uploads; dropping them would be too aggressive for Slice 1.
    """
    want_norm = (want or "").strip().lower()
    if not want_norm:
        return True
    code = str(row.get("lang_code") or "").strip().lower()
    if not code:
        return True
    return code == want_norm


def _filetype_matches(cand: Candidate, main_cat: int) -> bool:
    allowed = ALLOWED_FILETYPES_BY_MAIN_CAT.get(main_cat)
    if allowed is None:
        return True
    if not cand.filetypes:
        return False
    return any(ft in allowed for ft in cand.filetypes)


def _sort_candidates(
    cands: list[Candidate], *, prefer_freeleech: bool
) -> list[Candidate]:
    def key(c: Candidate) -> tuple[int, int]:
        # Negative so higher seeders sort first under ascending sort.
        freeleech_rank = 0 if (prefer_freeleech and (c.free or c.personal_freeleech)) else 1
        return (freeleech_rank, -c.seeders)

    return sorted(cands, key=key)


# ─── HTTP + main entrypoint ───────────────────────────────────────────


def _build_request_body(
    *,
    text: str,
    main_cat: int,
    start_number: int = 0,
) -> bytes:
    body = {
        "tor": {
            "text": text,
            "srchIn": ["title", "author"],
            "searchType": "all",
            "searchIn": "torrents",
            "cat": ["0"],
            "main_cat": [main_cat],
            "sortType": "seedersDesc",
            "startNumber": str(start_number),
        },
        "thumbnail": "false",
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def _post_json(
    url: str,
    body: bytes,
    *,
    cookie: str,
    timeout: float,
    opener: Any | None,
) -> tuple[int, bytes]:
    """POST JSON with the MAM cookie, return ``(status, raw_body)``.

    ``opener`` mirrors :mod:`alice_speaking.infra.presence` — tests
    pass a fake with ``.open(req, timeout=...)``. Real callers leave
    it ``None`` and the stdlib opener handles the request.
    """
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Cookie": f"mam_id={cookie}",
            "User-Agent": "alice-mam/1 (read-only)",
            "Accept": "application/json",
        },
    )
    if opener is None:
        resp = urllib.request.urlopen(req, timeout=timeout)
    else:
        resp = opener.open(req, timeout=timeout)
    with resp:
        status = getattr(resp, "status", 200)
        raw = resp.read()
    return status, raw


def search_mam(
    text: str,
    *,
    main_cat: int = MAIN_CAT_EBOOKS,
    lang: str = "eng",
    prefer_freeleech: bool = True,
    cookie: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    opener: Any | None = None,
    audit_log: Path | None = None,
    emit_audit: bool = True,
) -> list[Candidate]:
    """Search MAM. Returns normalised :class:`Candidate` rows.

    Any failure mode (missing cookie, network error, non-200, bad
    JSON, empty ``data``) returns ``[]`` and logs to stderr. The
    caller is expected to treat an empty list as "no match" rather
    than "transport broke" — slice 2 will layer richer error signals.

    ``emit_audit`` defaults to ``True`` for real calls. The test suite
    passes ``emit_audit=False`` to avoid touching the audit log.
    """
    if not text or not text.strip():
        return []

    if cookie is None:
        try:
            cookie = load_mam_cookie()
        except FileNotFoundError as exc:
            print(f"alice_mam: {exc}", file=sys.stderr)
            return []

    body = _build_request_body(text=text, main_cat=main_cat)
    try:
        status, raw = _post_json(
            SEARCH_URL,
            body,
            cookie=cookie,
            timeout=timeout,
            opener=opener,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(
            f"alice_mam: search transport error ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return []

    if status != 200:
        print(f"alice_mam: non-200 from MAM: {status}", file=sys.stderr)
        return []

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        print(f"alice_mam: bad JSON from MAM: {exc}", file=sys.stderr)
        return []

    if not isinstance(payload, dict):
        print("alice_mam: MAM payload not a JSON object", file=sys.stderr)
        return []

    rows = payload.get("data") or []
    if not isinstance(rows, list) or not rows:
        if emit_audit:
            _emit_audit_event(
                text=text,
                main_cat=main_cat,
                result_count=0,
                first_result_id=None,
                chose_freeleech=False,
                log_path=audit_log or _DEFAULT_EVENT_LOG,
            )
        return []

    candidates: list[Candidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _language_matches(row, lang):
            continue
        cand = _normalise_row(row)
        if cand is None:
            continue
        if not _filetype_matches(cand, main_cat):
            continue
        candidates.append(cand)

    sorted_cands = _sort_candidates(candidates, prefer_freeleech=prefer_freeleech)

    if emit_audit:
        first_id = sorted_cands[0].id if sorted_cands else None
        chose_freeleech = bool(
            sorted_cands and (sorted_cands[0].free or sorted_cands[0].personal_freeleech)
        )
        _emit_audit_event(
            text=text,
            main_cat=main_cat,
            result_count=len(sorted_cands),
            first_result_id=first_id,
            chose_freeleech=chose_freeleech,
            log_path=audit_log or _DEFAULT_EVENT_LOG,
        )

    return sorted_cands


def pick_candidate(
    candidates: list[Candidate],
    *,
    require_freeleech: bool = True,
) -> Candidate | None:
    """Pick the best match from a sorted candidate list.

    * When ``require_freeleech=True``: return the first free candidate,
      or ``None`` if none are free. (The sort order from
      :func:`search_mam` puts free candidates first, so this is
      typically ``candidates[0]`` or ``None``.)
    * When ``require_freeleech=False``: return the first candidate
      regardless, or ``None`` on an empty list.
    """
    if not candidates:
        return None
    if require_freeleech:
        for c in candidates:
            if c.free or c.personal_freeleech:
                return c
        return None
    return candidates[0]


# ─── CLI entrypoint ────────────────────────────────────────────────────


def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            'usage: python -m alice_mam.search "<text>" [main_cat]',
            file=sys.stderr,
        )
        return 2
    text = argv[1]
    main_cat = int(argv[2]) if len(argv) > 2 else MAIN_CAT_EBOOKS
    cands = search_mam(text, main_cat=main_cat)
    json.dump([c.to_dict() for c in cands], sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
