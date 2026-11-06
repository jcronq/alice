"""status_site.reconciler: dedupe merged items into Done, surface open asks."""

from __future__ import annotations

import json
from pathlib import Path

from status_site import reconciler as R

MERGED = "https://github.com/jcronq/alice/pull/601"
MERGED2 = "https://github.com/jcronq/alice/pull/602"
OPEN = "https://github.com/jcronq/alice/pull/603"

PAGE = f"""# Status

## Running now

- [ ] ⏳ **Merged thing**: shipped in {MERGED}
- [ ] ⏳ **Open thing**: in review at {OPEN}
- [ ] ⏳ **Mixed thing**: {MERGED} and {OPEN}
- [ ] ⏳ **Multi thing**: {MERGED} then {MERGED2}
  continued line for task-0007
- [ ] ⏳ **Task thing**: tracked as task-0042
- [ ] **No refs**: nothing to check

## Waiting on you

- [ ] **Existing ask**: pick a color

## Done today

- [x] **Older win**: finished earlier
"""

PRS = {MERGED: "MERGED", MERGED2: "CLOSED", OPEN: "OPEN"}
TASKS = {"task-0007": "done", "task-0042": "done"}


def run_reconcile(text: str, status_dir: Path, prs=PRS, tasks=TASKS) -> str:
    return R.reconcile(text, status_dir, prs.get, tasks.get)


def section(text: str, name: str) -> str:
    return text.split(f"## {name}")[1].split("\n## ")[0]


def thread(status_dir: Path, slug: str, *senders: str) -> None:
    d = status_dir / "threads"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{slug}.jsonl", "a") as f:
        for s in senders:
            f.write(json.dumps({"from": s, "text": "x"}) + "\n")


def test_merged_items_move_to_done(tmp_path):
    out = run_reconcile(PAGE, tmp_path)
    done = section(out, "Done today")
    running = section(out, "Running now")
    assert "- [x] ⏳ **Merged thing**" in done
    assert "- [x] ⏳ **Task thing**" in done
    assert "**Merged thing**" not in running
    assert "**Older win**" in done


def test_open_mixed_and_refless_items_stay(tmp_path):
    running = section(run_reconcile(PAGE, tmp_path), "Running now")
    assert "**Open thing**" in running
    assert "**Mixed thing**" in running
    assert "**No refs**" in running


def test_multi_ref_block_moves_with_continuation(tmp_path):
    done = section(run_reconcile(PAGE, tmp_path), "Done today")
    assert "- [x] ⏳ **Multi thing**" in done
    assert "  continued line for task-0007" in done


def test_unresolved_ref_leaves_item(tmp_path):
    out = run_reconcile(PAGE, tmp_path, tasks={})
    assert "**Task thing**" in section(out, "Running now")
    assert "**Multi thing**" in section(out, "Running now")
    assert "**Merged thing**" in section(out, "Done today")


def test_open_ask_surfaces_pointer(tmp_path):
    thread(tmp_path, "open-thing", "alice", "jason")
    out = run_reconcile(PAGE, tmp_path)
    waiting = section(out, "Waiting on you")
    assert "**Reply needed: Open thing**" in waiting
    assert "**Existing ask**" in waiting
    assert "**Open thing**" in section(out, "Running now")


def test_answered_thread_no_pointer(tmp_path):
    thread(tmp_path, "open-thing", "jason", "alice")
    out = run_reconcile(PAGE, tmp_path)
    assert "Reply needed" not in out


def test_item_already_waiting_gets_no_pointer(tmp_path):
    thread(tmp_path, "existing-ask", "jason")
    assert "Reply needed" not in run_reconcile(PAGE, tmp_path)


def test_stale_pointer_removed_after_reply(tmp_path):
    thread(tmp_path, "open-thing", "jason")
    once = run_reconcile(PAGE, tmp_path)
    assert "Reply needed: Open thing" in once
    thread(tmp_path, "open-thing", "alice")
    twice = run_reconcile(once, tmp_path)
    assert "Reply needed" not in twice
    assert "**Existing ask**" in twice


def test_idempotent(tmp_path):
    thread(tmp_path, "open-thing", "jason")
    once = run_reconcile(PAGE, tmp_path)
    assert once != PAGE
    assert run_reconcile(once, tmp_path) == once


def test_nothing_to_do_returns_input_unchanged(tmp_path):
    page = "# S\n\n## Running now\n\n- [ ] **A**: x\n"
    assert run_reconcile(page, tmp_path) == page


def test_malformed_lines_are_kept(tmp_path):
    page = (
        f"## Running now\n\n- [ ] no bold but {MERGED}\nstray paragraph {MERGED}\n"
        f"- [ ] **Real**: {MERGED}\n"
    )
    out = run_reconcile(page, tmp_path)
    assert "- [ ] no bold but" in out
    assert "stray paragraph" in out
    assert "- [x] **Real**" in section(out, "Done today")


def test_missing_sections_are_created(tmp_path):
    page = f"## Running now\n\n- [ ] **Real**: {MERGED}\n"
    out = run_reconcile(page, tmp_path)
    assert "## Done today" in out
    assert "- [x] **Real**" in section(out, "Done today")
    assert run_reconcile(out, tmp_path) == out


def test_fenced_items_and_headings_ignored(tmp_path):
    page = f"## Running now\n\n```\n## Done today\n- [ ] **Fenced**: {MERGED}\n```\n"
    assert run_reconcile(page, tmp_path) == page


def test_run_writes_atomically_and_second_pass_is_noop(tmp_path):
    (tmp_path / "index.md").write_text(PAGE)
    assert R.run(tmp_path, PRS.get, TASKS.get) is True
    written = (tmp_path / "index.md").read_text()
    assert "- [x] ⏳ **Merged thing**" in section(written, "Done today")
    assert R.run(tmp_path, PRS.get, TASKS.get) is False
    assert (tmp_path / "index.md").read_text() == written
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_dry_run_prints_diff_and_writes_nothing(tmp_path, capsys):
    (tmp_path / "index.md").write_text(PAGE)
    assert R.run(tmp_path, PRS.get, TASKS.get, dry_run=True) is True
    assert "+- [x] ⏳ **Merged thing**" in capsys.readouterr().out
    assert (tmp_path / "index.md").read_text() == PAGE


def test_gh_failure_is_ambiguous(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("gh", 10)

    monkeypatch.setattr(R.subprocess, "run", boom)
    assert R.gh_pr_state(MERGED) is None


def test_store_task_state(tmp_path):
    from alice_forge.task_store import TaskStore

    store = TaskStore(tmp_path)
    rec = store.create(title="t", artifact_type="code", actor="alice")
    lookup = R.store_task_state(tmp_path)
    assert lookup(rec.id) == rec.status
    assert lookup("task-9999") is None


def test_main_missing_index_is_clean(tmp_path, capsys):
    mind = tmp_path
    (mind / "inner" / "status").mkdir(parents=True)
    assert R.main(["--mind", str(mind)]) == 0
    assert "no changes" in capsys.readouterr().out


BARE_PAGE = """# Status

## Running now

- [ ] ⏳ **Bare merged**: shipped as #601
- [ ] ⏳ **Bare open**: in review at #603
- [ ] ⏳ **Bare issue**: tracking issue #700
- [ ] ⏳ **Anchor only**: see https://github.com/jcronq/alice/pull/603#issuecomment-1

## Done today
"""


def test_bare_pr_refs_resolve_against_repo(tmp_path):
    seen: list[str] = []

    def prs(url: str):
        seen.append(url)
        return {MERGED: "MERGED", OPEN: "OPEN"}.get(url)  # #700 is an issue → None

    out = R.reconcile(BARE_PAGE, tmp_path, prs, {}.get)
    assert "- [x] ⏳ **Bare merged**" in section(out, "Done today")
    running = section(out, "Running now")
    for title in ("Bare open", "Bare issue", "Anchor only"):
        assert title in running
    assert "https://github.com/jcronq/alice/pull/700" in seen


def test_bare_pr_refs_honor_repo_option(tmp_path):
    other = "https://github.com/jcronq/other/pull/601"
    out = R.reconcile(BARE_PAGE, tmp_path, {other: "MERGED"}.get, {}.get, repo="jcronq/other")
    assert "Bare merged" in section(out, "Done today")
