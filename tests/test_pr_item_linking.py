"""Tests for the ``Closes item-<id>`` PR→item linking plumbing (#828, PR-5).

Three acceptance tests:

1. :func:`watchers.github.extract_closes_item_ref` — pure extractor
   over PR bodies (both-lines case, miss case, ``None`` body).
2. :func:`alice_forge.sm.store.record_pr_ref` — idempotent INSERT
   OR IGNORE semantics, actor-trust check, distinct ``pr_url`` → new
   row.
3. :func:`alice_forge.self_merge.record_pr_item_ref` — end-to-end
   "extract + resolve + record" path (hit case + miss case +
   ``caplog`` warning).
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from alice_forge import self_merge
from alice_forge.sm import store
from alice_state_db.schema import apply_schema
from watchers.github import extract_closes_item_ref


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_item(conn: sqlite3.Connection, item_id: str) -> None:
    """Minimal ``items`` row — FK target for ``item_external_refs``."""
    conn.execute(
        """
        INSERT INTO items (item_id, item_type, title, created_at, updated_at)
        VALUES (?, 'gh:issue', ?, '2026-11-07T00:00:00+00:00',
                '2026-11-07T00:00:00+00:00')
        """,
        (item_id, f"test seed for {item_id}"),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path):
    """Return a fresh sqlite connection with the SM v2 schema applied."""
    conn = sqlite3.connect(str(tmp_path / "alice-state.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 1. Extractor
# ---------------------------------------------------------------------------


def test_extract_closes_item_ref_both_lines() -> None:
    body = (
        "## Summary\n"
        "\n"
        "Some words.\n"
        "\n"
        "Closes #821\n"
        "Closes item-0821\n"
        "\n"
        "More words.\n"
    )
    url = "https://github.com/jcronq/alice/pull/821"
    assert extract_closes_item_ref(body, url) == ("item-0821", url)


def test_extract_closes_item_ref_missing_line_returns_none() -> None:
    body = "## Summary\n\nCloses #821\n\nno item alias here.\n"
    url = "https://github.com/jcronq/alice/pull/821"
    assert extract_closes_item_ref(body, url) is None


def test_extract_closes_item_ref_none_body_returns_none() -> None:
    url = "https://github.com/jcronq/alice/pull/821"
    assert extract_closes_item_ref(None, url) is None
    assert extract_closes_item_ref("", url) is None


# ---------------------------------------------------------------------------
# 2. record_pr_ref — idempotency, actor trust, distinct-url append
# ---------------------------------------------------------------------------


def test_record_pr_ref_is_idempotent(db: sqlite3.Connection) -> None:
    item_id = "gh:jcronq/alice#821"
    _seed_item(db, item_id)
    pr_url = "https://github.com/jcronq/alice/pull/821"

    store.record_pr_ref(conn=db, item_id=item_id, pr_url=pr_url, actor="speaking")
    store.record_pr_ref(conn=db, item_id=item_id, pr_url=pr_url, actor="speaking")

    (count,) = db.execute(
        "SELECT COUNT(*) FROM item_external_refs WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    assert count == 1


def test_record_pr_ref_rejects_untrusted_actor(db: sqlite3.Connection) -> None:
    item_id = "gh:jcronq/alice#821"
    _seed_item(db, item_id)
    with pytest.raises(store.ActorNotTrustedError):
        store.record_pr_ref(
            conn=db,
            item_id=item_id,
            pr_url="https://github.com/jcronq/alice/pull/821",
            actor="random-bot",
        )
    # Ensure nothing was written.
    (count,) = db.execute(
        "SELECT COUNT(*) FROM item_external_refs"
    ).fetchone()
    assert count == 0


def test_record_pr_ref_distinct_url_appends(db: sqlite3.Connection) -> None:
    item_id = "gh:jcronq/alice#821"
    _seed_item(db, item_id)
    store.record_pr_ref(
        conn=db,
        item_id=item_id,
        pr_url="https://github.com/jcronq/alice/pull/821",
        actor="speaking",
    )
    store.record_pr_ref(
        conn=db,
        item_id=item_id,
        pr_url="https://github.com/jcronq/alice/pull/999",
        actor="speaking",
    )
    (count,) = db.execute(
        "SELECT COUNT(*) FROM item_external_refs WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    assert count == 2


# ---------------------------------------------------------------------------
# 3. self_merge.record_pr_item_ref — integration
# ---------------------------------------------------------------------------


def test_record_pr_item_ref_hit(db: sqlite3.Connection) -> None:
    item_id = "gh:jcronq/alice#821"
    _seed_item(db, item_id)
    url = "https://github.com/jcronq/alice/pull/821"
    body = (
        "## Summary\n\n"
        "Some fix.\n\n"
        "Closes #821\n\n"
        "Closes item-0821\n"
    )
    result = self_merge.record_pr_item_ref(
        conn=db,
        repo="jcronq/alice",
        pr_body=body,
        pr_url=url,
        actor="speaking",
    )
    assert result == item_id
    rows = db.execute(
        """
        SELECT item_id, ref_type, ref_target_item_id
        FROM item_external_refs
        WHERE item_id = ?
        """,
        (item_id,),
    ).fetchall()
    assert rows == [(item_id, "gh:pr", url)]


def test_record_pr_item_ref_miss_logs_warning_and_writes_nothing(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    item_id = "gh:jcronq/alice#821"
    _seed_item(db, item_id)
    body = "## Summary\n\nCloses #821\n\nno item alias at all.\n"
    with caplog.at_level(logging.WARNING, logger="alice_forge.self_merge"):
        result = self_merge.record_pr_item_ref(
            conn=db,
            repo="jcronq/alice",
            pr_body=body,
            pr_url="https://github.com/jcronq/alice/pull/821",
            actor="speaking",
        )
    assert result is None
    (count,) = db.execute(
        "SELECT COUNT(*) FROM item_external_refs"
    ).fetchone()
    assert count == 0
    assert any(
        "no `Closes item-<N>` line in PR body" in rec.message
        for rec in caplog.records
    )
