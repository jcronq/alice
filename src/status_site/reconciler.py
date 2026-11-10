"""Status-page janitor: reconcile ``inner/status/index.md`` with ground truth.

One idempotent pass per invocation (CLI / cron, same shape as ``watchers/github.py``):

* **Dedupe** — a top-level list item whose ``pull/NNN`` / bare ``#NNN`` and ``task-NNNN`` refs are *all*
  finished (PR merged/closed, task done/rejected) moves into ``## Done today`` as ``[x]``.
  Mixed, open or unresolvable refs (gh timeout, unknown task) leave the item untouched.
* **Question-surface** — an item whose ``threads/<slug>.jsonl`` ends on an unanswered
  message from Jason gets a ``Reply needed: <title>`` pointer line under
  ``## Waiting on you``. The pointer is dropped once Alice replies or the item is done.

Unparseable lines are never rewritten. The server's Tracker picks the edit up on its
next scan and records the ``moved`` events; nothing here touches the history files.
"""

from __future__ import annotations

from core.forge_repos import primary_repo as _primary_repo
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from status_site.history import (
    _FENCE_RE,
    _HEAD_RE,
    _LIST_RE,
    Item,
    parse_page,
    read_jsonl,
    title_slug,
)

DONE_SECTION = "done today"
WAITING_SECTION = "waiting on you"
POINTER_PREFIX = "Reply needed: "
GH_TIMEOUT = 10
DEFAULT_REPO = _primary_repo()

_PR_URL_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_BARE_PR_RE = re.compile(r"(?<![\w/&#])#(\d+)\b")
_TASK_RE = re.compile(r"\btask-\d{4,}\b")
_PR_FINISHED = {"MERGED", "CLOSED"}
_TASK_FINISHED = {"done", "rejected"}

PrState = Callable[[str], "str | None"]
TaskState = Callable[[str], "str | None"]


def gh_pr_state(pr_url: str) -> str | None:
    """``gh pr view <url> --json state`` → MERGED/CLOSED/OPEN, None on any failure."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "state"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            return None
        state = json.loads(proc.stdout).get("state")
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return None
    return state if isinstance(state, str) else None


def store_task_state(root: Path) -> TaskState:
    """Task-status lookup over an ``inner/tasks/`` directory; None if unknown."""
    from alice_forge.task_store import TaskStore, TaskStoreError

    store = TaskStore(root)

    def lookup(task_id: str) -> str | None:
        try:
            return store.load(task_id).status
        except (TaskStoreError, OSError, ValueError):
            return None

    return lookup


def _cached(fn: Callable[[str], "str | None"]) -> Callable[[str], "str | None"]:
    seen: dict[str, str | None] = {}

    def wrapper(key: str) -> str | None:
        if key not in seen:
            seen[key] = fn(key)
        return seen[key]

    return wrapper


@dataclass
class _Block:
    lines: list[str]
    item: Item | None  # first titled item in the block; None → unparseable, left alone

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class _Section:
    heading: str | None  # raw heading line; None for the preamble
    name: str
    body: list[str] = field(default_factory=list)


def _split_sections(lines: list[str]) -> list[_Section]:
    sections = [_Section(None, "")]
    in_fence = False
    for raw in lines:
        if _FENCE_RE.match(raw.expandtabs(4)):
            in_fence = not in_fence
        h = None if in_fence else _HEAD_RE.match(raw)
        if h and not _FENCE_RE.match(raw):
            name = re.sub(r"[*_`~]", "", h.group(2)).strip().lower()
            sections.append(_Section(raw, name))
        else:
            sections[-1].body.append(raw)
    return sections


def _blocks(body: list[str]) -> list[list[str] | str]:
    """Body → ordered pieces: top-level list-item blocks (``list[str]``) and loose
    lines (``str``). Children and continuation lines stay inside their parent block."""
    out: list[list[str] | str] = []
    cur: list[str] | None = None
    blanks: list[str] = []
    in_fence = False
    for raw in body:
        line = raw.expandtabs(4)
        fence = bool(_FENCE_RE.match(line))
        m = None if fence or in_fence else _LIST_RE.match(line)
        if fence:
            in_fence = not in_fence
        if m and not m.group(1):  # top-level list item → new block
            if cur is not None:
                out.append(cur)
            out.extend(blanks)
            blanks, cur = [], [raw]
        elif cur is not None and not line.strip():
            blanks.append(raw)
        elif cur is not None and (line[:1].isspace() or not blanks):
            cur.extend(blanks)
            blanks = []
            cur.append(raw)
        else:
            if cur is not None:
                out.append(cur)
                cur = None
            out.extend(blanks)
            blanks = []
            out.append(raw)
    if cur is not None:
        out.append(cur)
    out.extend(blanks)
    return out


def _make_block(lines: list[str], page: str) -> _Block:
    items = parse_page("\n".join(lines), page)
    return _Block(lines, items[0] if items else None)


def _refs_finished(
    text: str, pr_state: PrState, task_state: TaskState, repo: str = DEFAULT_REPO
) -> bool:
    """True iff the text has refs and every one of them resolves to finished.

    Bare ``#NNN`` refs are looked up as PRs in ``repo``; an issue number makes ``gh pr view``
    fail, which reads as ambiguous (item stays put).
    """
    prs = [f"https://github.com/{m.group(1)}/pull/{m.group(2)}" for m in _PR_URL_RE.finditer(text)]
    prs += [
        f"https://github.com/{repo}/pull/{n}" for n in _BARE_PR_RE.findall(_PR_URL_RE.sub("", text))
    ]
    tasks = _TASK_RE.findall(text)
    if not prs and not tasks:
        return False
    return all(pr_state(u) in _PR_FINISHED for u in dict.fromkeys(prs)) and all(
        task_state(t) in _TASK_FINISHED for t in dict.fromkeys(tasks)
    )


def _unanswered(status_dir: Path, slug: str) -> bool:
    thread = read_jsonl(status_dir / "threads" / f"{slug}.jsonl")
    return bool(thread) and thread[-1].get("from") == "jason"


def _checked(lines: list[str]) -> list[str]:
    """Rewrite the item's first line to ``[x]`` (adding a checkbox if it had none)."""
    first = re.sub(
        r"^((?:[-*+]|\d+[.)])\s+)(?:\[[ xX]\]\s+)?", r"\1[x] ", lines[0], count=1
    )
    return [first, *lines[1:]]


def _append(section: _Section, new_lines: list[str]) -> None:
    trailing: list[str] = []
    while section.body and not section.body[-1].strip():
        trailing.insert(0, section.body.pop())
    section.body.extend(new_lines)
    section.body.extend(trailing or [""])


def reconcile(
    text: str,
    status_dir: Path,
    pr_state: PrState,
    task_state: TaskState,
    page: str = "index",
    repo: str = DEFAULT_REPO,
) -> str:
    """Pure transform: new ``index.md`` text (identical to ``text`` if nothing to do)."""
    pr_state, task_state = _cached(pr_state), _cached(task_state)
    sections = _split_sections(text.split("\n"))

    def find(key: str) -> _Section | None:
        return next((s for s in sections if s.heading and key in s.name), None)

    done = find(DONE_SECTION)
    waiting = find(WAITING_SECTION)

    pieces = {id(s): _blocks(s.body) for s in sections}
    moved: list[list[str]] = []
    wanted: dict[str, str] = {}  # target slug → pointer title
    existing: dict[str, tuple[_Section, list[str]]] = {}  # target slug → pointer block

    for sec in sections:
        kept: list[list[str] | str] = []
        for piece in pieces[id(sec)]:
            if isinstance(piece, str):
                kept.append(piece)
                continue
            block = _make_block(piece, page)
            item = block.item
            if item is None:
                kept.append(piece)
                continue
            if item.title.startswith(POINTER_PREFIX):
                if sec is waiting:
                    existing[title_slug(item.title[len(POINTER_PREFIX):])] = (sec, piece)
                kept.append(piece)
                continue
            if sec is done:
                kept.append(piece)
                continue
            if _refs_finished(block.text, pr_state, task_state, repo):
                moved.append(_checked(piece))
                continue
            if sec is not waiting and _unanswered(status_dir, item.slug):
                wanted[item.slug] = item.title
            kept.append(piece)
        pieces[id(sec)] = kept

    stale_slugs = [slug for slug in existing if slug not in wanted]
    # Pointers whose ask was answered (or whose item is gone/done) are dropped.
    for slug in stale_slugs:
        sec, piece = existing[slug]
        pieces[id(sec)] = [p for p in pieces[id(sec)] if p is not piece]
    new_pointers = [
        [f"- [ ] **{POINTER_PREFIX}{title}**: Jason's last message in the thread is unanswered."]
        for slug, title in wanted.items()
        if slug not in existing
    ]

    if not (moved or new_pointers or stale_slugs):
        return text

    for sec in sections:
        sec.body = [ln for p in pieces[id(sec)] for ln in ([p] if isinstance(p, str) else p)]

    for key, heading, add in (
        (DONE_SECTION, "## Done today", [ln for b in moved for ln in b]),
        (WAITING_SECTION, "## Waiting on you", [ln for b in new_pointers for ln in b]),
    ):
        if not add:
            continue
        sec = find(key)
        if sec is None:
            sec = _Section(heading, key)
            if sections[-1].body and sections[-1].body[-1].strip():
                sections[-1].body.append("")
            sections.append(sec)
            sec.body.append("")
        _append(sec, add)

    out: list[str] = []
    for sec in sections:
        if sec.heading is not None:
            out.append(sec.heading)
        out.extend(sec.body)
    return "\n".join(out)


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def run(
    status_dir: Path,
    pr_state: PrState,
    task_state: TaskState,
    *,
    dry_run: bool = False,
    out=None,
    repo: str = DEFAULT_REPO,
) -> bool:
    """One pass. Returns True if ``index.md`` changed (or would, under dry-run)."""
    index = status_dir / "index.md"
    if not index.is_file():
        return False
    pr_state, task_state = _cached(pr_state), _cached(task_state)  # shared across both reads
    old = index.read_text()
    new = reconcile(old, status_dir, pr_state, task_state, repo=repo)
    if new == old:
        return False
    if dry_run:
        (out or sys.stdout).writelines(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                "index.md",
                "index.md (reconciled)",
            )
        )
        return True

    from alice_thinking import vault_lock

    with vault_lock.acquire(index, timeout=10):
        # Re-read under the lock: a hand-edit may have landed since the first read.
        # Ground-truth lookups are cached, so this costs no extra gh calls.
        old = index.read_text()
        new = reconcile(old, status_dir, pr_state, task_state, repo=repo)
        if new == old:
            return False
        write_atomic(index, new)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--mind",
        default=os.environ.get("ALICE_MIND") or os.path.expanduser("~/alice-mind"),
        help="alice-mind directory (status pages in inner/status, tasks in inner/tasks)",
    )
    ap.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help="repo that bare #NNN PR refs resolve against (default: %(default)s)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    mind = Path(args.mind)
    status_dir = Path(os.environ.get("STATUS_DIR") or mind / "inner" / "status")
    try:
        changed = run(
            status_dir,
            gh_pr_state,
            store_task_state(mind / "inner" / "tasks"),
            dry_run=args.dry_run,
            repo=args.repo,
        )
    except TimeoutError as e:
        print(f"status-reconciler: index.md is locked ({e}); try next cycle", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"status-reconciler: {e}", file=sys.stderr)
        return 1
    print("status-reconciler: " + ("changed" if changed else "no changes"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
