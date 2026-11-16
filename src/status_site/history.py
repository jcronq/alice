"""Item-history tracker: deterministic diffs of the status pages' list items.

Every ``SCAN_INTERVAL`` seconds the server scans each top-level ``STATUS_DIR/*.md``,
parses its list items (checkbox, bold title, description, page, section) and diffs
them against the last snapshot in ``STATUS_DIR/.item-state.json``. Changes are
appended as events to ``STATUS_DIR/item-history/<slug>.jsonl``:

    appeared      {page, section, title, text, checked[, seeded]}
    checked / unchecked
    text_changed  {old, new}
    moved         {from: {page, section}, to: {page, section}}
    removed       {page, section}

Items are keyed by slug (the bold title, slugified exactly like the frontend), so a
title that moves between pages keeps its history. The first scan (no state file)
records ``appeared`` with ``seeded: true`` so the timeline can say "already there".
An item must be missing for ``REMOVE_AFTER`` consecutive scans before ``removed``
fires, so a half-written page can't flood remove/re-appear pairs. No AI involved.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

SCAN_INTERVAL = 10.0
REMOVE_AFTER = 2
STATE_FILE = ".item-state.json"
HISTORY_DIR = "item-history"

_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])(\s+)(?:\[([ xX])\]\s+)?(.*)$")
_HEAD_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# First bold span, optionally wrapped in a link ``[**x**](url)`` or strikethrough ``~~**x**~~``.
_BOLD_RE = re.compile(r"(?P<pre>\[|~~)?\*\*(?P<t>[^*]+?)\*\*(?P<post>\]\([^)]*\)|~~)?")
# ``<!--deps: slug-a, slug-b-->`` on a bullet: items this one waits on. Hidden by marked.js.
_DEPS_RE = re.compile(r"<!--\s*deps:(.*?)-->", re.S)
# Inline markdown link ``[text](url)`` → the visible text. We strip these
# from a bold title before slugifying so the backend sees what the
# frontend's ``strong.textContent`` sees (the rendered link text, no URL).
# Covers only the common ``[text](url)`` form — reference-style links
# don't appear in WoY titles today.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def slugify(text: str, limit: int = 60) -> str:
    """Same slug the frontend computes: lowercase, non-alnum runs -> '-', trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit]


def _strip_md_links(text: str) -> str:
    """Reduce every ``[visible](url)`` to just ``visible``.

    Mirrors what marked.js renders into the DOM: a bold title wrapping
    a link becomes ``<strong>…<a>visible</a>…</strong>``, and
    ``strong.textContent`` yields only ``visible``. Slugging the raw
    markdown would smear the URL into the slug; slugging the stripped
    form stays in sync with the frontend's ``titleSlug`` (which reads
    ``textContent``). Slice-2 fix for the mismatch Slice-1 surfaced
    (title like ``**foo ([#641](URL))**`` → frontend slug ``foo-641``,
    backend slug used to carry the URL).
    """
    return _MD_LINK_RE.sub(r"\1", text)


def title_slug(title: str) -> str:
    """Frontend leaf slug: bold text with inline ``[text](url)`` links
    collapsed to their visible text, trailing ``:``/``.`` dropped, then
    slugified. Mirrors ``static/index.html::titleSlug`` so
    ``item-history/<slug>.jsonl`` and ``threads/<slug>.jsonl`` line up
    with what the frontend writes when it opens the WoY thread row."""
    stripped = _strip_md_links(title.strip())
    return slugify(re.sub(r"[:.]+$", "", stripped.strip()))


def legacy_title_slug(title: str) -> str:
    """The pre-Slice-2 ``title_slug`` — kept only so
    :func:`migrate_stale_slugs` can detect ``item-history`` / ``threads``
    files written under the old algorithm and rename them in-place.

    Do not call this from anywhere else; new writes go through
    :func:`title_slug`."""
    return slugify(re.sub(r"[:.]+$", "", title.strip()))


def safe_slug(slug: object) -> str:
    return re.sub(r"[^a-z0-9-]", "", str(slug or "").lower())[:60].strip("-")


def now_iso() -> str:
    """Local time with a ``-04:00`` style offset (container TZ is a POSIX rule)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Item:
    slug: str
    title: str
    text: str
    checked: bool | None
    page: str
    section: str
    depends_on: list[str] = field(default_factory=list)


def _extract_deps(text: str) -> tuple[str, list[str]]:
    """Strip every ``<!--deps: ...-->`` marker; return (text, deduped safe slugs)."""
    deps: list[str] = []
    for m in _DEPS_RE.finditer(text):
        for raw in m.group(1).split(","):
            s = safe_slug(raw.strip())
            if s and s not in deps:
                deps.append(s)
    return _DEPS_RE.sub(" ", text), deps


def unresolved_deps(depends_on: list[str], items: dict[str, dict]) -> list[str]:
    """Deps that are not a checked item in ``items`` (state-file shape). Unknown slugs
    count as unresolved so a typo blocks rather than silently passing."""
    return [d for d in depends_on if (items.get(d) or {}).get("checked") is not True]


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _split_title(body: str) -> tuple[str, str] | None:
    m = _BOLD_RE.search(body)
    if not m:
        return None
    title = _clean(m.group("t"))
    pre, post = m.group("pre"), m.group("post") or ""
    paired = (pre == "[" and post.startswith("](")) or (pre == "~~" and post == "~~")
    start, end = (m.start(), m.end()) if paired else (m.start("t") - 2, m.end("t") + 2)
    post = re.sub(r"^[\s:—–-]+", "", _clean(body[end:]))
    return title, _clean(body[:start] + " " + post)


def parse_page(markdown: str, page: str) -> list[Item]:
    """List items with a bold title, in document order. Children are their own items;
    continuation lines (indented under the item, or lazy right after it) join the
    description of the deepest item they belong to."""
    items: list[Item] = []
    section = ""
    # Open list items: (marker indent, content indent, index into items or -1 if untitled).
    stack: list[tuple[int, int, int]] = []
    in_fence = False
    blank = False
    for raw in markdown.splitlines():
        line = raw.expandtabs(4)
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip():
            blank = True
            continue
        h = _HEAD_RE.match(line)
        if h:
            section = _clean(re.sub(r"[*_`~]", "", h.group(2)))
            stack, blank = [], False
            continue
        indent = len(line) - len(line.lstrip())
        m = _LIST_RE.match(line)
        if m:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            box = m.group(4)
            body, deps = _extract_deps(m.group(5))
            checked = None if box is None else box.lower() == "x"
            idx = -1
            split = _split_title(body)
            if split and title_slug(split[0]):
                items.append(
                    Item(
                        title_slug(split[0]),
                        split[0],
                        split[1],
                        checked,
                        page,
                        section,
                        deps,
                    )
                )
                idx = len(items) - 1
            stack.append(
                (indent, len(m.group(1)) + len(m.group(2)) + len(m.group(3)), idx)
            )
            blank = False
            continue
        owner = next((idx for _, ci, idx in reversed(stack) if indent >= ci), None)
        if owner is None and stack and not blank:
            owner = stack[-1][2]  # lazy continuation
        if owner is None:
            stack = []  # unindented paragraph after a blank line ends the list
        elif owner >= 0:
            extra, more = _extract_deps(line.strip())
            items[owner].text = _clean(items[owner].text + " " + extra)
            items[owner].depends_on += [d for d in more if d not in items[owner].depends_on]
        blank = False
    return items


def scan(status_dir: Path) -> dict[str, Item]:
    """Every titled item on every top-level page, keyed by slug (first occurrence wins,
    pages in sorted order, so duplicates resolve the same way every scan)."""
    out: dict[str, Item] = {}
    for p in sorted(status_dir.glob("*.md")):
        if p.name.startswith("."):
            continue
        try:
            md = p.read_text(errors="replace")
        except OSError:
            continue
        for it in parse_page(md, p.stem):
            out.setdefault(it.slug, it)
    return out


def diff(
    old: dict[str, dict], new: dict[str, Item], *, seeded: bool, at: str
) -> tuple[list[tuple[str, dict]], dict[str, dict]]:
    """Return ([(slug, event), ...], new_state). ``old`` is the persisted state."""
    events: list[tuple[str, dict]] = []
    state: dict[str, dict] = {}
    for slug, it in new.items():
        cur = asdict(it)
        prev = old.get(slug)
        state[slug] = cur
        if prev is None or prev.get("gone"):
            ev = {
                "at": at,
                "type": "appeared",
                "page": it.page,
                "section": it.section,
                "title": it.title,
                "text": it.text,
                "checked": it.checked,
            }
            if seeded:
                ev["seeded"] = True
            events.append((slug, ev))
            continue
        if (prev.get("page"), prev.get("section")) != (it.page, it.section):
            events.append(
                (
                    slug,
                    {
                        "at": at,
                        "type": "moved",
                        "from": {
                            "page": prev.get("page"),
                            "section": prev.get("section"),
                        },
                        "to": {"page": it.page, "section": it.section},
                    },
                )
            )
        was = prev.get("checked") is True
        if it.checked is True and not was:
            events.append((slug, {"at": at, "type": "checked", "page": it.page}))
        elif was and it.checked is not True:
            events.append((slug, {"at": at, "type": "unchecked", "page": it.page}))
        if prev.get("text") != it.text:
            events.append(
                (
                    slug,
                    {
                        "at": at,
                        "type": "text_changed",
                        "old": prev.get("text"),
                        "new": it.text,
                    },
                )
            )
    for slug, prev in old.items():
        if slug in new or prev.get("gone"):
            continue
        missing = int(prev.get("missing", 0)) + 1
        if missing >= REMOVE_AFTER:
            events.append(
                (
                    slug,
                    {
                        "at": at,
                        "type": "removed",
                        "page": prev.get("page"),
                        "section": prev.get("section"),
                    },
                )
            )
            # Keep a tombstone so a later re-add reads as "appeared", not "seeded".
            state[slug] = {**prev, "missing": 0, "gone": True}
        else:
            state[slug] = {**prev, "missing": missing}
    return events, state


class Tracker:
    def __init__(self, status_dir: Path, lock: threading.Lock | None = None):
        self.status_dir = Path(status_dir)
        self.lock = lock or threading.Lock()

    @property
    def state_path(self) -> Path:
        return self.status_dir / STATE_FILE

    @property
    def history_dir(self) -> Path:
        return self.status_dir / HISTORY_DIR

    def _load(self) -> dict[str, dict] | None:
        try:
            d = json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None
        return d.get("items", {}) if isinstance(d, dict) else None

    def step(self, at: str | None = None) -> list[tuple[str, dict]]:
        """One scan + diff + persist. Returns the events written."""
        at = at or now_iso()
        with self.lock:
            old = self._load()
            seeded = old is None
            events, state = diff(old or {}, scan(self.status_dir), seeded=seeded, at=at)
            if events:
                self.history_dir.mkdir(parents=True, exist_ok=True)
                by: dict[str, list[dict]] = {}
                for slug, ev in events:
                    by.setdefault(slug, []).append(ev)
                for slug, evs in by.items():
                    with open(self.history_dir / f"{slug}.jsonl", "a") as f:
                        f.write("".join(json.dumps(e) + "\n" for e in evs))
            if events or seeded or state != (old or {}):
                tmp = self.status_dir / f".item-state.{os.getpid()}.tmp"
                tmp.write_text(json.dumps({"updated": at, "items": state}, indent=0))
                os.replace(tmp, self.state_path)
        return events

    def run(
        self, interval: float = SCAN_INTERVAL, stop: threading.Event | None = None
    ) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.step()
            except Exception as e:  # never let a bad page kill the thread
                print(f"status_site: item-history scan failed: {e!r}", flush=True)
            stop.wait(interval)

    def start(self, interval: float = SCAN_INTERVAL) -> threading.Thread:
        t = threading.Thread(
            target=self.run, args=(interval,), name="item-history", daemon=True
        )
        t.start()
        return t


def read_jsonl(path: Path, limit: int = 500) -> list[dict]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def read_history(status_dir: Path, slug: object) -> dict | None:
    """``{slug, events, thread, extra}`` for one item, or None for an unusable slug."""
    s = safe_slug(slug)
    if not s:
        return None
    d = Path(status_dir)
    return {
        "slug": s,
        "events": read_jsonl(d / HISTORY_DIR / f"{s}.jsonl"),
        "thread": read_jsonl(d / "threads" / f"{s}.jsonl"),
        "extra": read_jsonl(d / "item-extra" / f"{s}.jsonl"),
    }


# ---------------------------------------------------------------- migration


def _page_title_slug_pairs(status_dir: Path) -> list[tuple[str, str]]:
    """Scan every top-level ``*.md`` in ``status_dir`` and return
    ``[(legacy_slug, new_slug)]`` pairs for bold titles whose slug
    actually differs between the old and new algorithms. Pages with
    no mismatches contribute nothing.
    """
    pairs: list[tuple[str, str]] = []
    for p in sorted(status_dir.glob("*.md")):
        if p.name.startswith("."):
            continue
        try:
            md = p.read_text(errors="replace")
        except OSError:
            continue
        # We use parse_page to walk the same grammar the tracker uses;
        # it already does the hard work of ignoring fences, section
        # headings, and untitled items.
        for it in parse_page(md, p.stem):
            # ``it.title`` is the raw bold text — including any inline
            # ``[text](url)`` the title wraps. Compare both slugs; only
            # mismatches matter for migration.
            new = it.slug
            old = legacy_title_slug(it.title)
            if old and old != new:
                pairs.append((old, new))
    # Dedupe while preserving first-seen order; pages scanned in sorted
    # order so the choice is deterministic.
    seen: set[tuple[str, str]] = set()
    uniq: list[tuple[str, str]] = []
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        uniq.append(pair)
    return uniq


def _log_stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _rename_one(source: Path, dest: Path, *, log=_log_stderr) -> bool:
    """Rename ``source`` → ``dest`` if ``source`` exists and ``dest``
    does not, logging the outcome. Returns True on an actual rename.

    When both exist, the old file stays (nothing lost) and a warning
    is logged so the operator can merge by hand if they care. When
    neither exists, nothing happens. When only ``source`` exists, the
    rename is atomic (``os.rename`` on the same filesystem).
    """
    try:
        if not source.exists():
            return False
        if dest.exists():
            log(
                f"status_site: migrate-slug SKIP {source.parent.name}/"
                f"{source.name} (new slug file {dest.name} already present)"
            )
            return False
        os.rename(source, dest)
        log(
            f"status_site: migrate-slug {source.parent.name}/{source.name} → "
            f"{dest.name}"
        )
        return True
    except OSError as e:
        log(
            f"status_site: migrate-slug FAIL {source.parent.name}/{source.name}"
            f" → {dest.name}: {e!r}"
        )
        return False


def migrate_stale_slugs(
    status_dir: Path, *, subdirs: tuple[str, ...] = (HISTORY_DIR, "threads", "item-extra")
) -> int:
    """One-shot rename of ``<legacy_slug>.jsonl`` → ``<new_slug>.jsonl``
    for every titled item whose old and new slugs disagree.

    Idempotent: running it twice on a fresh checkout is a no-op because
    the second pass finds no source files. Called from
    :func:`status_site.server.main` at process start so Slice-2 picks
    up history that Slice-1 wrote under the pre-fix slug.

    Returns the number of files actually renamed (useful in tests).
    """
    d = Path(status_dir)
    if not d.is_dir():
        return 0
    pairs = _page_title_slug_pairs(d)
    renamed = 0
    for old, new in pairs:
        if not old or not new or old == new:
            continue
        for sub in subdirs:
            sub_path = d / sub
            if not sub_path.is_dir():
                continue
            src = sub_path / f"{old}.jsonl"
            dst = sub_path / f"{new}.jsonl"
            if _rename_one(src, dst):
                renamed += 1
    # Tombstone pass: items whose page entry was deleted still carry
    # a legacy-slug key in ``.item-state.json`` and in any orphan
    # ``item-history/<legacy>.jsonl`` file. Pair those up by walking
    # ``item-history/`` and testing each legacy stem against
    # :func:`legacy_title_slug` ⇆ :func:`title_slug` on the state
    # file's recorded titles.
    state_path = d / STATE_FILE
    tombstone_pairs: list[tuple[str, str]] = []
    try:
        raw = json.loads(state_path.read_text())
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict) and isinstance(raw.get("items"), dict):
        items = raw["items"]
        for legacy, rec in list(items.items()):
            if not isinstance(rec, dict):
                continue
            title = rec.get("title") or rec.get("last_title")
            if not isinstance(title, str) or not title:
                continue
            new = title_slug(title)
            if not new or new == legacy:
                continue
            # Only rename if the state key actually matches the
            # legacy algorithm — don't touch keys that already
            # correspond to the new algorithm but have drift.
            if legacy_title_slug(title) != legacy:
                continue
            tombstone_pairs.append((legacy, new))
    for old, new in tombstone_pairs:
        for sub in subdirs:
            sub_path = d / sub
            if not sub_path.is_dir():
                continue
            src = sub_path / f"{old}.jsonl"
            dst = sub_path / f"{new}.jsonl"
            if _rename_one(src, dst):
                renamed += 1
    if renamed or tombstone_pairs:
        pairs = pairs + tombstone_pairs
    if renamed:
        # A state-file tombstone under the old slug keeps the tracker
        # from treating the next scan's ``new`` slug as a brand-new
        # item (which would fire an ``appeared`` event and bust the
        # timeline). Load, rekey old→new, write back under cfg.lock
        # equivalent (one-shot startup, no writers racing yet).
        state_path = d / STATE_FILE
        try:
            raw = json.loads(state_path.read_text())
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict) and isinstance(raw.get("items"), dict):
            items = raw["items"]
            mutated = False
            for old, new in pairs:
                if old in items and new not in items:
                    items[new] = items.pop(old)
                    mutated = True
            if mutated:
                try:
                    tmp = state_path.with_name(
                        f".item-state.{os.getpid()}.migrate.tmp"
                    )
                    tmp.write_text(json.dumps(raw, indent=0))
                    os.replace(tmp, state_path)
                except OSError as e:
                    _log_stderr(
                        f"status_site: migrate-slug state rewrite failed: {e!r}"
                    )
    return renamed
