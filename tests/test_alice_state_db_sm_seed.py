"""Tests for ``derive_sm_seed`` (issue #790, Step 1).

Covers:

* Idempotent seed — re-running against an unchanged mind produces zero
  new transition / amendment rows.
* GH-issue → ``state_machine`` mapping — ``sm:<state>`` label becomes
  the row's ``state`` column.
* ``task.yaml`` → ``state_machine`` mapping — the task's ``status``
  field becomes the item's state.
* ``transitions.jsonl`` → ``transitions`` table — one row per line,
  dedup-safe.
* [SM] comment extraction — the ``[SM] transition from=X to=Y`` body
  becomes a ``transitions`` row with ``source='gh-comment'``.
* External-refs extraction — a task's ``source: jcronq/alice#784`` +
  ``tags: [github-issue:jcronq/alice#784]`` + ``merge_ref`` produces
  ``item_external_refs`` rows pointing at the GH item.
* CLI — ``task db item`` and ``task db transitions`` round-trip the
  seeded data.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_forge import task_cli
from alice_state_db import read_api, sm_seed


# ---------------------------------------------------------------------------
# Fixture helpers — shape the tmp mind directory the seed walks.


@pytest.fixture
def mind(tmp_path: pathlib.Path) -> pathlib.Path:
    """Tmp alice-mind tree with the directory shape the seed expects."""
    (tmp_path / "inner" / "tasks").mkdir(parents=True)
    (tmp_path / "inner" / "state").mkdir(parents=True)
    (tmp_path / "cortex-memory" / "gh-state" / "jcronq").mkdir(parents=True)
    return tmp_path


def _write_task(
    mind: pathlib.Path,
    task_id: str,
    *,
    status: str = "draft",
    title: str = "x",
    source: str = "speaking",
    tags: list[str] | None = None,
    merge_ref: str | None = None,
    transitions: list[dict] | None = None,
) -> pathlib.Path:
    """Write ``task.yaml`` + optional ``transitions.jsonl``."""
    task_dir = mind / "inner" / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = task_dir / "task.yaml"
    lines = [
        f"id: {task_id}",
        f"title: {json.dumps(title)}",
        "created: '2026-11-06T00:00:00+00:00'",
        "updated: '2026-11-06T00:00:00+00:00'",
        f"status: {status}",
        "actor: alice",
        "artifact_type: code",
        f"source: {json.dumps(source)}",
    ]
    if tags:
        lines.append("tags:")
        for tag in tags:
            lines.append(f"- {json.dumps(tag)}")
    else:
        lines.append("tags: []")
    if merge_ref:
        lines.append(f"merge_ref: {merge_ref}")
    yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if transitions is not None:
        tr_path = task_dir / "transitions.jsonl"
        with tr_path.open("w", encoding="utf-8") as fh:
            for row in transitions:
                fh.write(json.dumps(row) + "\n")
    return yaml_path


def _write_gh_mirror(
    mind: pathlib.Path,
    repo: str,
    number: int,
    *,
    kind: str = "issue",
    title: str = "x",
    state: str = "open",
) -> pathlib.Path:
    """Emit a gh-state note shaped like ``gh_state_mirror.write_note_atomic``."""
    org, _, name = repo.partition("/")
    out = mind / "cortex-memory" / "gh-state" / org / f"{name}-{number}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    if kind == "issue":
        body = (
            "---\n"
            f"slug: gh-state-{repo}-{number}\n"
            f"title: {repo}#{number} — {title}\n"
            "tags: [gh-state]\n"
            "note_type: gh-state\n"
            f"repo: {repo}\n"
            f"number: {number}\n"
            "type: issue\n"
            f"state: {state}\n"
            'updated_at: "2026-11-06T00:00:00+00:00"\n'
            "---\n\n"
            f"# {repo}#{number} — {title}\n"
        )
    else:
        body = (
            "---\n"
            f"slug: gh-state-{repo}-{number}\n"
            f"title: {repo}#{number} — {title}\n"
            "tags: [gh-state]\n"
            "note_type: gh-state\n"
            f"repo: {repo}\n"
            f"number: {number}\n"
            "type: pr\n"
            f"state: {state}\n"
            "merged: false\n"
            "draft: false\n"
            "base_branch: master\n"
            'updated_at: "2026-11-06T00:00:00+00:00"\n'
            "---\n"
        )
    out.write_text(body, encoding="utf-8")
    return out


def _db_path(mind: pathlib.Path) -> pathlib.Path:
    return mind / "inner" / "state" / "alice-state.db"


# ---------------------------------------------------------------------------
# Tests — core behaviour.


def test_schema_adds_sm_tables(mind: pathlib.Path) -> None:
    sm_seed.derive_sm_seed(mind)

    with read_api.connect(_db_path(mind)) as conn:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "items",
        "item_external_refs",
        "state_machine",
        "transitions",
        "amendments",
    } <= names


def test_local_task_seeds_item_and_state(mind: pathlib.Path) -> None:
    _write_task(
        mind,
        "task-0019",
        status="building",
        title="refresh celestial_normals on read",
        source="jcronq/cozyhem-engine#61",
        tags=["github-issue:jcronq/cozyhem-engine#61", "jcronq/cozyhem-engine"],
        merge_ref="https://github.com/jcronq/cozyhem-engine/pull/61",
    )

    report = sm_seed.derive_sm_seed(mind)
    assert report.items_upserted >= 1
    assert report.local_items_seen == 1

    with read_api.connect(_db_path(mind)) as conn:
        item = conn.execute(
            "SELECT * FROM items WHERE item_id = 'local:task-0019'"
        ).fetchone()
        assert item is not None
        assert item["item_type"] == "local:task"
        assert "refresh celestial_normals" in item["title"]

        state = conn.execute(
            "SELECT state FROM state_machine WHERE item_id = 'local:task-0019'"
        ).fetchone()
        assert state["state"] == "building"

        refs = conn.execute(
            """
            SELECT ref_type, ref_target_item_id, ref_source
            FROM item_external_refs
            WHERE item_id = 'local:task-0019'
            ORDER BY ref_source
            """
        ).fetchall()
        kinds = {(r["ref_type"], r["ref_target_item_id"]) for r in refs}
        assert ("links_to", "gh:jcronq/cozyhem-engine#61") in kinds
        assert (
            "closed_by",
            "gh:pr:jcronq/cozyhem-engine#61",
        ) in kinds


def test_transitions_jsonl_seeds_transitions(mind: pathlib.Path) -> None:
    _write_task(
        mind,
        "task-0100",
        status="done",
        transitions=[
            {
                "ts": "2026-11-06T00:00:00+00:00",
                "from": None,
                "to": "draft",
                "actor": "alice",
                "reason": "created",
            },
            {
                "ts": "2026-11-06T00:01:00+00:00",
                "from": "draft",
                "to": "building",
                "actor": "alice",
                "reason": "spawned worker",
            },
            {
                "ts": "2026-11-06T00:02:00+00:00",
                "from": "building",
                "to": "done",
                "actor": "alice",
                "reason": "pr merged",
            },
        ],
    )

    sm_seed.derive_sm_seed(mind)

    with read_api.connect(_db_path(mind)) as conn:
        rows = conn.execute(
            """
            SELECT from_state, to_state, actor, source, source_ref
            FROM transitions
            WHERE item_id = 'local:task-0100'
            ORDER BY transition_id ASC
            """
        ).fetchall()
    assert len(rows) == 3
    assert rows[0]["from_state"] is None
    assert rows[0]["to_state"] == "draft"
    assert rows[-1]["to_state"] == "done"
    assert all(r["source"] == "task-transitions-jsonl" for r in rows)


def test_gh_mirror_without_fetcher_seeds_neutral_state(
    mind: pathlib.Path,
) -> None:
    """Mirror-only (no gh_fetcher) seeds an item + a neutral state row.

    The mirror doesn't record labels, so we can't name a real
    ``sm:*`` state. Open issues land as ``sm:unknown``; closed ones
    land as ``done`` so the status-site projection can still show
    the right bucket for merged work.
    """
    _write_gh_mirror(mind, "jcronq/alice", 100, state="open", title="open one")
    _write_gh_mirror(
        mind, "jcronq/alice", 101, state="closed", title="closed one"
    )

    sm_seed.derive_sm_seed(mind)

    with read_api.connect(_db_path(mind)) as conn:
        open_state = conn.execute(
            "SELECT state FROM state_machine WHERE item_id = 'gh:jcronq/alice#100'"
        ).fetchone()
        closed_state = conn.execute(
            "SELECT state FROM state_machine WHERE item_id = 'gh:jcronq/alice#101'"
        ).fetchone()
    assert open_state["state"] == "sm:unknown"
    assert closed_state["state"] == "done"


def test_gh_fetcher_populates_state_from_labels(mind: pathlib.Path) -> None:
    _write_gh_mirror(mind, "jcronq/alice", 790, title="SM substrate migration")

    def fake_fetch(repo: str, number: int) -> dict:
        assert (repo, number) == ("jcronq/alice", 790)
        return {
            "labels": [
                {"name": "art:code"},
                {"name": "sm:selected"},
            ],
            "author": {"login": "jcronq"},
            "body": "move SM to sqlite",
            "comments": [],
        }

    sm_seed.derive_sm_seed(mind, gh_fetcher=fake_fetch)

    with read_api.connect(_db_path(mind)) as conn:
        state = conn.execute(
            "SELECT state FROM state_machine WHERE item_id = 'gh:jcronq/alice#790'"
        ).fetchone()
        item = conn.execute(
            "SELECT author, summary FROM items WHERE item_id = 'gh:jcronq/alice#790'"
        ).fetchone()
    assert state["state"] == "selected"
    assert item["author"] == "jcronq"
    assert item["summary"] == "move SM to sqlite"


def test_sm_comment_extracted_as_transition(mind: pathlib.Path) -> None:
    _write_gh_mirror(mind, "jcronq/alice", 790, title="x")

    def fake_fetch(repo: str, number: int) -> dict:
        return {
            "labels": [{"name": "sm:building"}],
            "comments": [
                {
                    "id": "IC_1",
                    "author": {"login": "alice"},
                    "createdAt": "2026-11-06T00:30:00+00:00",
                    "body": (
                        "[SM] transition from=selected to=building "
                        "reason=spawn-dispatch-art-code"
                    ),
                },
                {
                    "id": "IC_2",
                    "author": {"login": "jcronq"},
                    "createdAt": "2026-11-06T00:35:00+00:00",
                    "body": "nice work on this one",
                },
                {
                    "id": "IC_3",
                    "author": {"login": "jcronq"},
                    "createdAt": "2026-11-06T00:40:00+00:00",
                    "body": "[SM] select",
                },
            ],
        }

    sm_seed.derive_sm_seed(mind, gh_fetcher=fake_fetch)

    with read_api.connect(_db_path(mind)) as conn:
        trs = conn.execute(
            """
            SELECT from_state, to_state, actor, reason, source, source_ref
            FROM transitions
            WHERE item_id = 'gh:jcronq/alice#790'
            """
        ).fetchall()
        am_rows = conn.execute(
            """
            SELECT author, body, source, source_ref
            FROM amendments
            WHERE item_id = 'gh:jcronq/alice#790'
            ORDER BY amendment_id
            """
        ).fetchall()

    assert len(trs) == 1
    assert trs[0]["from_state"] == "selected"
    assert trs[0]["to_state"] == "building"
    assert trs[0]["source"] == "gh-comment"
    assert trs[0]["source_ref"] == "IC_1"

    # Two amendments: one additive, one [SM]-verb (non-transition).
    assert len(am_rows) == 2
    bodies = [r["body"] for r in am_rows]
    assert "nice work on this one" in bodies
    assert "[SM] select" in bodies


def test_derive_is_idempotent(mind: pathlib.Path) -> None:
    _write_task(
        mind,
        "task-0200",
        status="draft",
        transitions=[
            {
                "ts": "2026-11-06T00:00:00+00:00",
                "from": None,
                "to": "draft",
                "actor": "alice",
                "reason": "created",
            }
        ],
    )
    _write_gh_mirror(mind, "jcronq/alice", 200, title="idem test")

    def fake_fetch(repo: str, number: int) -> dict:
        return {
            "labels": [{"name": "sm:draft"}],
            "comments": [
                {
                    "id": "IC_42",
                    "author": {"login": "jcronq"},
                    "createdAt": "2026-11-06T00:30:00+00:00",
                    "body": "[SM] transition from=draft to=selected",
                },
                {
                    "id": "IC_43",
                    "author": {"login": "jcronq"},
                    "createdAt": "2026-11-06T00:31:00+00:00",
                    "body": "ship it",
                },
            ],
        }

    sm_seed.derive_sm_seed(mind, gh_fetcher=fake_fetch)

    with read_api.connect(_db_path(mind)) as conn:
        counts_before = {
            table: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            for table in (
                "items",
                "item_external_refs",
                "state_machine",
                "transitions",
                "amendments",
            )
        }

    # Re-run with the SAME inputs.
    sm_seed.derive_sm_seed(mind, gh_fetcher=fake_fetch)

    with read_api.connect(_db_path(mind)) as conn:
        counts_after = {
            table: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            for table in (
                "items",
                "item_external_refs",
                "state_machine",
                "transitions",
                "amendments",
            )
        }
    assert counts_before == counts_after


def test_external_refs_dedup_across_source_and_tags(
    mind: pathlib.Path,
) -> None:
    # Source + tag both point at the same issue — should dedup.
    _write_task(
        mind,
        "task-0300",
        source="jcronq/alice#500",
        tags=["github-issue:jcronq/alice#500", "jcronq/alice"],
    )
    sm_seed.derive_sm_seed(mind)
    with read_api.connect(_db_path(mind)) as conn:
        refs = conn.execute(
            """
            SELECT COUNT(*) AS c FROM item_external_refs
            WHERE item_id = 'local:task-0300'
              AND ref_target_item_id = 'gh:jcronq/alice#500'
              AND ref_type = 'links_to'
            """
        ).fetchone()
    assert refs["c"] == 1


def test_gh_fetcher_failure_captured_in_report(mind: pathlib.Path) -> None:
    _write_gh_mirror(mind, "jcronq/alice", 900, title="will fail")

    def broken_fetch(repo: str, number: int) -> dict:
        raise RuntimeError("gh rate limit")

    report = sm_seed.derive_sm_seed(mind, gh_fetcher=broken_fetch)
    assert report.gh_fetch_failures == 1
    assert report.gh_fetch_calls == 0
    assert any(e["kind"] == "gh_fetch" for e in report.errors)
    # Item still seeded with mirror-only data (fallback behaviour).
    with read_api.connect(_db_path(mind)) as conn:
        item = conn.execute(
            "SELECT 1 FROM items WHERE item_id = 'gh:jcronq/alice#900'"
        ).fetchone()
    assert item is not None


def test_cli_db_item_returns_detail_payload(
    mind: pathlib.Path, capsys: pytest.CaptureFixture
) -> None:
    _write_task(
        mind,
        "task-0400",
        status="selected",
        transitions=[
            {
                "ts": "2026-11-06T00:00:00+00:00",
                "from": None,
                "to": "draft",
                "actor": "alice",
                "reason": "created",
            },
            {
                "ts": "2026-11-06T00:01:00+00:00",
                "from": "draft",
                "to": "selected",
                "actor": "alice",
                "reason": "selected",
            },
        ],
    )
    sm_seed.derive_sm_seed(mind)

    exit_code = task_cli.main(
        [
            "--root",
            str(mind / "inner" / "tasks"),
            "db",
            "--db-path",
            str(_db_path(mind)),
            "item",
            "local:task-0400",
        ]
    )
    assert exit_code == 0

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["item_id"] == "local:task-0400"
    assert payload["state"]["state"] == "selected"
    assert len(payload["recent_transitions"]) == 2
    # newest-first per item_detail
    assert payload["recent_transitions"][0]["to_state"] == "selected"


def test_cli_db_transitions_returns_full_history(
    mind: pathlib.Path, capsys: pytest.CaptureFixture
) -> None:
    _write_task(
        mind,
        "task-0500",
        status="done",
        transitions=[
            {
                "ts": "2026-11-06T00:00:00+00:00",
                "from": None,
                "to": "draft",
                "actor": "alice",
                "reason": "created",
            },
            {
                "ts": "2026-11-06T00:01:00+00:00",
                "from": "draft",
                "to": "building",
                "actor": "alice",
                "reason": "spawn",
            },
            {
                "ts": "2026-11-06T00:02:00+00:00",
                "from": "building",
                "to": "done",
                "actor": "alice",
                "reason": "merged",
            },
        ],
    )
    sm_seed.derive_sm_seed(mind)

    exit_code = task_cli.main(
        [
            "--root",
            str(mind / "inner" / "tasks"),
            "db",
            "--db-path",
            str(_db_path(mind)),
            "transitions",
            "local:task-0500",
        ]
    )
    assert exit_code == 0

    rows = json.loads(capsys.readouterr().out.strip())
    assert [r["to_state"] for r in rows] == ["draft", "building", "done"]


def test_cli_db_item_missing_returns_exit_2(
    mind: pathlib.Path, capsys: pytest.CaptureFixture
) -> None:
    sm_seed.derive_sm_seed(mind)

    exit_code = task_cli.main(
        [
            "--root",
            str(mind / "inner" / "tasks"),
            "db",
            "--db-path",
            str(_db_path(mind)),
            "item",
            "local:task-9999",
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not found" in err
