"""GBrain-style deterministic typed-edge extraction (Phase 1).

Design spec: cortex-memory/research/2026-07-07-gbrain-predicate-extraction-design.md

Runs as an operation after Stage B routing. Scans vault notes newer
than the last run for ``[[wikilink]]`` mentions, applies a schema-pack
of verb regexes to the ±80-char context window around each mention,
and writes typed edges into the ``typed_edges`` table of the FTS index
(``~/alice-mind/inner/state/cortex-index.db``).

Zero LLM calls. Regex-only. First match wins by declaration order in
the YAML schema pack. A cumulative 50ms ReDoS budget per note bounds
worst-case runtime; patterns skipped past the budget don't produce
edges but don't fail extraction either.

Key contracts:
  - No vault mutation. Notes are read-only.
  - Idempotent. UNIQUE(from_slug, to_slug, link_type, link_source)
    on ``typed_edges`` means a re-run produces the same edges.
  - Incremental. ``last_run_at`` in ``typed_edges_state.json`` gates
    which notes get re-scanned; ``null`` = full-vault sweep.
  - Batched writes: 500 rows per commit to bound transaction size.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import re
import sqlite3
import time
from typing import Any, Iterable, Optional


logger = logging.getLogger(__name__)


# Context window around each [[wikilink]] mention in the note body.
# Matches GBrain's CONTEXT_WINDOW_CHARS = 80.
CONTEXT_WINDOW_CHARS = 80

# Per-note cumulative regex budget in seconds. Overridden by the
# schema pack's regex_budget_ms field.
DEFAULT_REGEX_BUDGET_MS = 50

# Minimum wikilink target length (short names skipped unless slug
# resolves to a real note). Overridden by schema pack.
DEFAULT_MIN_NAME_LENGTH = 4

# Batch size for typed_edges INSERT commits.
BATCH_SIZE = 500

# Wikilink regex reused from the indexer's yaml_lite conventions.
# We match [[slug]] optionally followed by |display alias.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\|]+?)(?:\|[^\[\]]*?)?\]\]")

# Fenced code block stripper. State machine over lines; identical to
# indexer.yaml_lite._strip_code but inlined so predicate extraction
# never re-imports internals from a sibling module.
_FENCE_PREFIXES = ("```", "~~~")


@dataclasses.dataclass
class LinkType:
    """One verb from the schema pack."""

    name: str
    description: str
    regex_pattern: str
    compiled: re.Pattern[str]
    target_types: list[str]


@dataclasses.dataclass
class SchemaPack:
    """Parsed typed_edges_schema.yaml."""

    schema_version: int
    regex_budget_ms: int
    min_name_length: int
    ignore_list: frozenset[str]
    link_types: list[LinkType]


@dataclasses.dataclass
class ExtractionReport:
    """One-shot summary returned by :func:`extract_all`."""

    notes_scanned: int = 0
    notes_skipped: int = 0
    edges_written: int = 0
    edges_by_type: dict[str, int] = dataclasses.field(default_factory=dict)
    warnings: list[str] = dataclasses.field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------- schema pack loader ----------


class SchemaPackError(ValueError):
    """Raised when the schema pack YAML is malformed or has a bad regex."""


def load_schema_pack(path: pathlib.Path) -> SchemaPack:
    """Parse the typed_edges_schema.yaml file.

    Uses a minimal hand-rolled parser (yaml_lite in the indexer only
    handles flat frontmatter; the schema pack has nested list-of-maps
    under ``link_types:``). Rejects malformed regex patterns with a
    clear :class:`SchemaPackError`.
    """
    text = path.read_text(encoding="utf-8")
    parsed = _parse_schema_yaml(text)

    try:
        schema_version = int(parsed.get("schema_version", 1))
    except (TypeError, ValueError) as exc:
        raise SchemaPackError(f"invalid schema_version: {exc}") from exc

    try:
        regex_budget_ms = int(parsed.get("regex_budget_ms", DEFAULT_REGEX_BUDGET_MS))
    except (TypeError, ValueError) as exc:
        raise SchemaPackError(f"invalid regex_budget_ms: {exc}") from exc

    try:
        min_name_length = int(parsed.get("min_name_length", DEFAULT_MIN_NAME_LENGTH))
    except (TypeError, ValueError) as exc:
        raise SchemaPackError(f"invalid min_name_length: {exc}") from exc

    ignore_raw = parsed.get("ignore_list") or []
    if not isinstance(ignore_raw, list):
        raise SchemaPackError("ignore_list must be a YAML list")
    ignore_list = frozenset(str(x) for x in ignore_raw)

    link_types_raw = parsed.get("link_types") or []
    if not isinstance(link_types_raw, list):
        raise SchemaPackError("link_types must be a YAML list")

    link_types: list[LinkType] = []
    for i, entry in enumerate(link_types_raw):
        if not isinstance(entry, dict):
            raise SchemaPackError(f"link_types[{i}] must be a mapping")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SchemaPackError(f"link_types[{i}].name missing or not a string")
        pattern = entry.get("regex")
        if not isinstance(pattern, str) or not pattern.strip():
            raise SchemaPackError(
                f"link_types[{i}] ({name}).regex missing or not a string"
            )
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise SchemaPackError(
                f"link_types[{i}] ({name}).regex invalid: {exc}"
            ) from exc
        target_types = entry.get("target_types") or []
        if not isinstance(target_types, list):
            raise SchemaPackError(
                f"link_types[{i}] ({name}).target_types must be a list"
            )
        link_types.append(
            LinkType(
                name=name.strip(),
                description=str(entry.get("description") or ""),
                regex_pattern=pattern,
                compiled=compiled,
                target_types=[str(t) for t in target_types],
            )
        )

    if not link_types:
        raise SchemaPackError("link_types must not be empty")

    return SchemaPack(
        schema_version=schema_version,
        regex_budget_ms=regex_budget_ms,
        min_name_length=min_name_length,
        ignore_list=ignore_list,
        link_types=link_types,
    )


def _parse_schema_yaml(text: str) -> dict[str, Any]:
    """Minimal YAML parser for the schema-pack shape.

    Supports:
      - top-level scalars (``key: value``)
      - top-level flow-style lists (``key: [a, b, c]``)
      - top-level block-style scalar lists (``key:\\n  - item``)
      - top-level block-style mapping lists (``key:\\n  - name: x\\n    ...``)

    Not a general YAML parser. Rejects shapes it can't handle by raising
    :class:`SchemaPackError`.
    """
    lines = text.splitlines()
    out: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        # Skip blank lines and comments.
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        # Must be at column 0.
        if raw[0] in (" ", "\t"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z_][\w\-]*)\s*:\s*(.*?)\s*$", raw)
        if not m:
            i += 1
            continue
        key = m.group(1)
        rest = _strip_inline_comment(m.group(2))
        if rest == "":
            # Block list follows — could be scalar items or mapping items.
            items, next_i = _consume_block_list(lines, i + 1)
            if items is None:
                out[key] = ""
                i = next_i
                continue
            out[key] = items
            i = next_i
            continue
        # Inline scalar or flow list.
        out[key] = _parse_scalar(rest)
        i += 1
    return out


def _consume_block_list(
    lines: list[str], start: int
) -> tuple[Optional[list[Any]], int]:
    """Consume a block-style list starting at ``lines[start]``.

    Returns ``(items, next_index)``. Items may be scalars or mappings.
    ``items is None`` means the block was empty (no ``-`` marker found)
    and the caller should treat the parent key as an empty scalar.
    """
    items: list[Any] = []
    i = start
    marker_indent: Optional[int] = None
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        # A non-indented, non-comment line terminates the block.
        if raw and raw[0] not in (" ", "\t"):
            break
        indent = len(raw) - len(raw.lstrip(" \t"))
        if not stripped.startswith("- "):
            # Continuation of a previous mapping item — handled inside
            # _consume_mapping_item; if we hit one at the top level, we
            # bail because the block already ended.
            break
        if marker_indent is None:
            marker_indent = indent
        elif indent != marker_indent:
            # Different indent level = different block; bail.
            break
        # First item — decide scalar vs mapping.
        first_content = stripped[2:].strip()
        # Inline comment on the marker line strips off the trailing #.
        first_content_clean = _strip_inline_comment(first_content)
        # Mapping item: "- key: value" (any non-empty rest after key:).
        km = re.match(r"^([A-Za-z_][\w\-]*)\s*:\s*(.*)$", first_content_clean)
        if km:
            item, next_i = _consume_mapping_item(
                lines, i, marker_indent, km.group(1), km.group(2)
            )
            items.append(item)
            i = next_i
            continue
        # Scalar item.
        items.append(_parse_scalar(first_content_clean))
        i += 1
    if not items:
        return None, i
    return items, i


def _consume_mapping_item(
    lines: list[str],
    marker_line_idx: int,
    marker_indent: int,
    first_key: str,
    first_rest: str,
) -> tuple[dict[str, Any], int]:
    """Consume one mapping item in a block list.

    The item begins on ``lines[marker_line_idx]`` with a ``- key: value``
    marker at column ``marker_indent``. Continuation keys are indented
    further (typically ``marker_indent + 2``).
    """
    item: dict[str, Any] = {}
    first_rest_clean = _strip_inline_comment(first_rest.strip())
    item[first_key] = _parse_scalar(first_rest_clean) if first_rest_clean else ""
    i = marker_line_idx + 1
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        # New list marker at the same indent = next item; stop.
        if stripped.startswith("- ") and indent == marker_indent:
            break
        # Un-indented top-level key = end of block.
        if indent <= marker_indent:
            break
        # Continuation key: `key: value`.
        km = re.match(r"^([A-Za-z_][\w\-]*)\s*:\s*(.*?)\s*$", stripped)
        if not km:
            i += 1
            continue
        key = km.group(1)
        rest = _strip_inline_comment(km.group(2))
        item[key] = _parse_scalar(rest) if rest else ""
        i += 1
    return item, i


def _strip_inline_comment(s: str) -> str:
    """Remove a trailing YAML inline comment ("` # foo`") from a scalar
    or flow-list line, respecting quoted strings and flow brackets.

    A ``#`` counts as a comment start iff it's preceded by whitespace and
    is outside quotes and outside any ``[..]`` flow context.
    """
    if not s:
        return s
    depth = 0
    quote: Optional[str] = None
    for i, c in enumerate(s):
        if quote:
            if c == "\\" and i + 1 < len(s):
                continue
            if c == quote:
                quote = None
            continue
        if c in ('"', "'"):
            quote = c
            continue
        if c == "[":
            depth += 1
            continue
        if c == "]":
            depth -= 1
            continue
        if c == "#" and depth == 0 and (i == 0 or s[i - 1] in (" ", "\t")):
            return s[:i].rstrip()
    return s.rstrip()


def _parse_scalar(raw: str) -> Any:
    """Parse a scalar YAML value: strings, ints, bools, flow lists."""
    s = raw.strip()
    if s == "":
        return ""
    # Flow list.
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        parts = _split_flow(inner)
        return [_unquote(p.strip()) for p in parts]
    # Quoted scalar — delegate to _unquote so YAML escapes are processed.
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return _unquote(s)
    # Int.
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except ValueError:
            pass
    # Bool.
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if s.lower() in ("null", "~"):
        return None
    return s


def _unquote(s: str) -> str:
    """Strip surrounding quotes and process YAML string escapes.

    Double-quoted YAML strings process backslash escapes (``\\\\`` → ``\\``,
    ``\\n`` → newline, etc.). Single-quoted strings only process ``''`` →
    ``'``. This matters for regex patterns in the schema pack: YAML source
    ``"\\\\[\\\\[([\\\\w-]+)\\\\]\\\\]"`` decodes to the regex string
    ``\\[\\[([\\w-]+)\\]\\]``, which is what ``re.compile`` expects.
    """
    s = s.strip()
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        inner = s[1:-1]
        return _decode_double_quoted(inner)
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        inner = s[1:-1]
        return inner.replace("''", "'")
    return s


def _decode_double_quoted(s: str) -> str:
    """Process YAML double-quoted string escapes.

    Supported escapes (subset of YAML 1.2 spec):
      - ``\\\\`` → single backslash
      - ``\\n`` / ``\\t`` / ``\\r`` / ``\\0``
      - ``\\"`` → double quote
      - Any other ``\\X`` is passed through as ``X`` (loose interpretation
        keeps regex character classes like ``\\w`` working when the pattern
        was already single-escaped in the source).
    """
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "0":
                out.append("\0")
            elif nxt == "\\":
                out.append("\\")
            elif nxt == '"':
                out.append('"')
            else:
                # Unknown escape: preserve the backslash so regex
                # classes like \w survive when the pack author used a
                # single backslash.
                out.append("\\")
                out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _split_flow(inner: str) -> list[str]:
    """Split flow-list content on commas not inside brackets or quotes."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: Optional[str] = None
    for c in inner:
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
        elif c in ('"', "'"):
            quote = c
            buf.append(c)
        elif c == "[":
            depth += 1
            buf.append(c)
        elif c == "]":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    if buf:
        parts.append("".join(buf))
    return parts


# ---------- code-block stripping ----------


def strip_code_blocks(body: str) -> str:
    """Remove fenced code blocks so [[wikilinks]] inside them don't match.

    Inline backticks are preserved — the design spec's context-window
    matching happens on prose, and inline code is rarely a spurious
    typed-edge source. If false positives from inline code become an
    issue in Phase 2, this can be tightened.
    """
    out: list[str] = []
    fence: Optional[str] = None
    for line in body.splitlines():
        stripped = line.lstrip()
        if fence is None and any(stripped.startswith(p) for p in _FENCE_PREFIXES):
            fence = stripped[:3]
            continue
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        out.append(line)
    return "\n".join(out)


# ---------- state file ----------


def load_state(path: pathlib.Path) -> dict[str, Any]:
    """Load typed_edges_state.json, tolerating missing/corrupt files."""
    if not path.exists():
        return {"last_run_at": None, "schema_version": 1, "total_edges_extracted": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"last_run_at": None, "schema_version": 1, "total_edges_extracted": 0}


def save_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    """Write typed_edges_state.json atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------- vault iteration ----------


def _slug_exists(vault_root: pathlib.Path, slug: str) -> bool:
    """Cheap best-effort check: is ``slug.md`` present anywhere in the vault?

    We don't need full wikilink resolution here — the goal is to override
    ``ignore_list`` / ``min_name_length`` filters when the user has clearly
    made ``[[X]]`` a real note. False negatives are safe (edge just gets
    filtered out); false positives keep noise edges.
    """
    for hit in vault_root.rglob(f"{slug}.md"):
        # Skip dotfile directories.
        if any(part.startswith(".") for part in hit.relative_to(vault_root).parts):
            continue
        return True
    return False


def _iter_vault_notes(
    vault_root: pathlib.Path, since_epoch: Optional[float]
) -> Iterable[pathlib.Path]:
    """Yield markdown files under ``vault_root`` newer than ``since_epoch``.

    ``since_epoch=None`` yields every file. Uses filesystem mtime because
    the frontmatter's ``updated`` field isn't reliably present.
    """
    for md in vault_root.rglob("*.md"):
        rel = md.relative_to(vault_root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if since_epoch is not None:
            try:
                if md.stat().st_mtime <= since_epoch:
                    continue
            except OSError:
                continue
        yield md


# ---------- extraction core ----------


def extract_from_body(
    pack: SchemaPack,
    body: str,
    *,
    slug_exists_fn,
) -> tuple[list[tuple[str, str, str]], bool]:
    """Extract typed edges from a single note body.

    Returns ``(edges, budget_tripped)`` where ``edges`` is a list of
    ``(to_slug, link_type, context)`` tuples deduplicated by
    ``(to_slug, link_type)`` (last-wins) and ``budget_tripped`` is True
    if the ReDoS budget was exhausted during extraction.

    Algorithm (from design spec):
      a. Strip fenced code blocks from body.
      b. Find all [[wikilink]] mentions.
      c. Extract ±80 char context window around each.
      d. Run schema-pack regexes in declaration order — first match wins.
      e. Match → emit edge with verb as link_type.
      f. No verb match + real wikilink → fall through to catch-all
         (the ``mentions`` verb from the schema pack).
      g. 50ms cumulative regex budget per note; abort remaining
         patterns when exceeded.
      h. Skip ignore_list wikilinks unless the slug exists.
      i. Skip short wikilinks (< min_name_length) unless the slug exists.
    """
    cleaned = strip_code_blocks(body)
    edges_map: dict[tuple[str, str], tuple[str, str, str]] = {}
    budget_ns = pack.regex_budget_ms * 1_000_000
    consumed_ns = 0
    budget_tripped = False

    for m in _WIKILINK_RE.finditer(cleaned):
        target = m.group(1).strip()
        # Strip section anchors, folder prefixes.
        if "#" in target:
            target = target.split("#", 1)[0].strip()
        if "|" in target:
            target = target.split("|", 1)[0].strip()
        if not target:
            continue
        # Basename resolution — [[folder/slug]] → "slug".
        basename = target.rsplit("/", 1)[-1]

        # Filter passes: ignore_list, min_name_length.
        # Both override when the slug is a real note.
        slug_exists = None  # lazy

        def _resolve_exists() -> bool:
            nonlocal slug_exists
            if slug_exists is None:
                slug_exists = slug_exists_fn(basename)
            return slug_exists

        if basename in pack.ignore_list and not _resolve_exists():
            continue
        if len(basename) < pack.min_name_length and not _resolve_exists():
            continue

        # Context window: ±80 chars around the [[wikilink]] span.
        start = max(0, m.start() - CONTEXT_WINDOW_CHARS)
        end = min(len(cleaned), m.end() + CONTEXT_WINDOW_CHARS)
        window = cleaned[start:end]

        matched_verb: Optional[str] = None
        for lt in pack.link_types:
            if consumed_ns >= budget_ns:
                budget_tripped = True
                break
            t0 = time.perf_counter_ns()
            try:
                hit = lt.compiled.search(window)
            except re.error:
                consumed_ns += time.perf_counter_ns() - t0
                continue
            consumed_ns += time.perf_counter_ns() - t0
            if not hit:
                continue
            # Confirm the regex captured OUR wikilink target — otherwise
            # a nearby [[other]] within the same window would spuriously
            # count as a match for our mention. If the regex has capture
            # group 1, it should equal `basename` (or resolve to it via
            # basename). If no capture group, accept the match (generic
            # broad-body regexes).
            captured = hit.group(1) if hit.groups else None
            if captured is not None:
                captured_base = captured.rsplit("/", 1)[-1].strip()
                if captured_base != basename:
                    # Regex matched a different wikilink in the window;
                    # don't attribute this verb to our mention. Keep
                    # searching remaining verbs.
                    continue
            matched_verb = lt.name
            break

        if matched_verb is None:
            # No verb caught it; drop. (The ``mentions`` catch-all in the
            # schema pack matches any [[wikilink]] so the fall-through
            # is handled by that pattern; no extra logic here.)
            continue
        key = (basename, matched_verb)
        edges_map[key] = (basename, matched_verb, target)

    return list(edges_map.values()), budget_tripped


def extract_all(
    vault_root: pathlib.Path,
    db_path: pathlib.Path,
    schema_path: pathlib.Path,
    state_path: pathlib.Path,
    *,
    force_full: bool = False,
) -> ExtractionReport:
    """Run predicate extraction over the vault.

    Loads the schema pack, opens the FTS DB, iterates notes newer than
    ``last_run_at`` (or all notes when ``force_full=True`` or state is
    empty), extracts typed edges, and batch-writes them into the
    ``typed_edges`` table. Updates ``typed_edges_state.json`` on
    success.

    Returns an :class:`ExtractionReport` with per-verb counts and
    timing.
    """
    started = time.time()
    report = ExtractionReport()

    pack = load_schema_pack(schema_path)
    state = load_state(state_path)
    since_epoch: Optional[float]
    if force_full or state.get("last_run_at") is None:
        since_epoch = None
    else:
        try:
            since_epoch = float(state["last_run_at"])
        except (TypeError, ValueError):
            since_epoch = None

    conn = sqlite3.connect(str(db_path))
    try:
        # Sanity: the typed_edges table must exist. It's created by the
        # indexer on rebuild. If missing, tell the caller and bail —
        # extracting into a non-existent table is a hard error, not a
        # silent no-op.
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='typed_edges'"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "typed_edges table missing from cortex-index.db — "
                "run build_index.py to migrate to schema_version=2 first"
            )

        # Cheap slug-exists cache from the notes table (avoids per-mention
        # filesystem probes for the common path).
        vault_slugs: set[str] = set()
        for (slug,) in conn.execute("SELECT slug FROM notes"):
            vault_slugs.add(slug)

        def slug_exists(slug: str) -> bool:
            return slug in vault_slugs or _slug_exists(vault_root, slug)

        batch: list[tuple[str, str, str, str]] = []

        def flush() -> None:
            nonlocal batch
            if not batch:
                return
            conn.executemany(
                "INSERT OR IGNORE INTO typed_edges "
                "(from_slug, to_slug, link_type, context) VALUES (?, ?, ?, ?)",
                batch,
            )
            conn.commit()
            report.edges_written += conn.total_changes - report.edges_written
            batch = []

        # Reset total_changes counter tracking; SQLite's counter is
        # connection-lifetime cumulative, so we sample and diff.
        baseline_changes = conn.total_changes

        for md in _iter_vault_notes(vault_root, since_epoch):
            report.notes_scanned += 1
            try:
                raw = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                report.warnings.append(f"read failed: {md.name}: {exc}")
                report.notes_skipped += 1
                continue

            # Split frontmatter cheaply — reuse the yaml_lite splitter
            # so we operate only on the body.
            from indexer.yaml_lite import split_frontmatter

            try:
                _fm, body = split_frontmatter(raw)
            except Exception as exc:  # noqa: BLE001
                report.warnings.append(f"frontmatter parse failed: {md.name}: {exc}")
                report.notes_skipped += 1
                continue

            from_slug = md.stem
            edges, budget_tripped = extract_from_body(
                pack, body, slug_exists_fn=slug_exists
            )
            if budget_tripped:
                report.warnings.append(f"regex budget exhausted: {md.name}")

            for to_slug, link_type, context in edges:
                batch.append((from_slug, to_slug, link_type, context))
                report.edges_by_type[link_type] = (
                    report.edges_by_type.get(link_type, 0) + 1
                )
                if len(batch) >= BATCH_SIZE:
                    flush()

        flush()
        report.edges_written = conn.total_changes - baseline_changes
    finally:
        conn.close()

    report.elapsed_seconds = round(time.time() - started, 3)

    # Update state.
    state["last_run_at"] = started
    state["schema_version"] = pack.schema_version
    state["total_edges_extracted"] = (
        int(state.get("total_edges_extracted") or 0) + report.edges_written
    )
    save_state(state_path, state)

    return report
