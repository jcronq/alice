"""Tests for the multi-repo dispatcher wiring (issue #261).

Covers :func:`alice_forge.dispatcher.load_dispatcher_repos` parsing across
the three documented config shapes (missing, malformed, present) and
the relaxed-label mode in :func:`alice_forge.dispatcher.run` for repos
flagged ``labels_configured=False`` in the config block.

The label-relax branch is a behavior change inside ``run()`` itself:
when an issue lacks a canonical ``sm:*`` label and the call passed
``labels_configured=False``, the per-issue rejection log + the
``report.skipped_trust`` increment are suppressed. Tests assert both
the silent-skip path and the unchanged strict-mode path.
"""

from __future__ import annotations

import json
import pathlib


from alice_forge import dispatcher as sm
from alice_forge.dispatcher.constants import (
    DEFAULT_REPO,
    WORKER_REPO_PATH,
    load_dispatcher_repos,
)


# ---------------------------------------------------------------------------
# load_dispatcher_repos — config parsing
# ---------------------------------------------------------------------------


def test_load_dispatcher_repos_missing_file_returns_fallback(
    tmp_path: pathlib.Path,
) -> None:
    """No config file ⇒ single-repo fallback to DEFAULT_REPO + WORKER_REPO_PATH.

    This is the backwards-compatibility guarantee: pre-#261 single-repo
    behavior must hold whenever the config is absent.

    Field-by-field comparison (not ``repos == [RepoConfig(...)]``) because
    the package __init__ reloads ``constants`` when re-imported, which
    rebinds the ``RepoConfig`` class. Two instances built from the
    pre-reload and post-reload classes carry identical fields but fail
    ``==`` because the dataclass-generated ``__eq__`` checks ``type(self)``.
    """
    missing = tmp_path / "does-not-exist.json"
    repos = load_dispatcher_repos(config_path=missing, log=lambda _msg: None)
    assert len(repos) == 1
    assert repos[0].slug == DEFAULT_REPO
    assert repos[0].checkout_path == WORKER_REPO_PATH
    assert repos[0].labels_configured is True


def test_load_dispatcher_repos_missing_block_returns_fallback(
    tmp_path: pathlib.Path,
) -> None:
    """Config file present but no ``sm_dispatcher`` block ⇒ fallback.

    Avoids penalizing a config that only carries github_watcher /
    speaking / thinking blocks — we don't want the dispatcher to crash
    if someone hasn't added the new section yet.
    """
    cfg = tmp_path / "alice.config.json"
    cfg.write_text(json.dumps({"github_watcher": {"repos": ["jcronq/alice"]}}))
    repos = load_dispatcher_repos(config_path=cfg, log=lambda _msg: None)
    assert len(repos) == 1
    assert repos[0].slug == DEFAULT_REPO
    assert repos[0].checkout_path == WORKER_REPO_PATH
    assert repos[0].labels_configured is True


def test_load_dispatcher_repos_corrupt_json_returns_fallback(
    tmp_path: pathlib.Path,
) -> None:
    """Corrupt JSON ⇒ log + fallback. Refuse to silently swallow."""
    cfg = tmp_path / "alice.config.json"
    cfg.write_text("{not valid json")
    seen: list[str] = []
    repos = load_dispatcher_repos(config_path=cfg, log=seen.append)
    assert repos[0].slug == DEFAULT_REPO
    assert any("failed to read" in s for s in seen)


def test_load_dispatcher_repos_parses_full_config(tmp_path: pathlib.Path) -> None:
    """Happy path: parses alice + cozyhem-engine entries with all fields."""
    cfg = tmp_path / "alice.config.json"
    cfg.write_text(
        json.dumps(
            {
                "sm_dispatcher": {
                    "repos": [
                        {
                            "slug": "jcronq/alice",
                            "checkout_path": "/home/alice/alice",
                            "labels_configured": True,
                        },
                        {
                            "slug": "jcronq/cozyhem-engine",
                            "checkout_path": "/home/alice/cozyhem-engine",
                            "labels_configured": False,
                        },
                    ]
                }
            }
        )
    )
    repos = load_dispatcher_repos(config_path=cfg, log=lambda _msg: None)
    # Field comparison rather than dataclass ``==`` to survive
    # ``alice_forge.dispatcher.__init__``'s reload-on-reimport of constants.
    assert len(repos) == 2
    assert repos[0].slug == "jcronq/alice"
    assert repos[0].checkout_path == pathlib.Path("/home/alice/alice")
    assert repos[0].labels_configured is True
    assert repos[1].slug == "jcronq/cozyhem-engine"
    assert repos[1].checkout_path == pathlib.Path("/home/alice/cozyhem-engine")
    assert repos[1].labels_configured is False


def test_load_dispatcher_repos_labels_configured_defaults_false(
    tmp_path: pathlib.Path,
) -> None:
    """Missing ``labels_configured`` ⇒ defaults to False (relaxed mode).

    The strict label gate is opt-in. A repo entry that only specifies
    slug + checkout_path is treated as cozyhem-engine-equivalent —
    safer default than accidentally putting an unlabeled repo through
    the strict trust filter.
    """
    cfg = tmp_path / "alice.config.json"
    cfg.write_text(
        json.dumps(
            {
                "sm_dispatcher": {
                    "repos": [
                        {
                            "slug": "jcronq/foo",
                            "checkout_path": "/home/alice/foo",
                        }
                    ]
                }
            }
        )
    )
    repos = load_dispatcher_repos(config_path=cfg, log=lambda _msg: None)
    assert len(repos) == 1
    assert repos[0].labels_configured is False


def test_load_dispatcher_repos_skips_malformed_entries(
    tmp_path: pathlib.Path,
) -> None:
    """Malformed rows are logged + skipped; valid rows survive."""
    cfg = tmp_path / "alice.config.json"
    cfg.write_text(
        json.dumps(
            {
                "sm_dispatcher": {
                    "repos": [
                        {"slug": "jcronq/alice", "checkout_path": "/home/alice/alice"},
                        {"slug": "missing-checkout-path"},  # no checkout
                        {"checkout_path": "/tmp/x"},  # no slug
                        {"slug": "no-slash-in-slug", "checkout_path": "/tmp/y"},
                        42,  # not a dict or string
                        "jcronq/string-slug",  # bare-string shorthand OK
                    ]
                }
            }
        )
    )
    seen: list[str] = []
    repos = load_dispatcher_repos(config_path=cfg, log=seen.append)
    slugs = [r.slug for r in repos]
    assert "jcronq/alice" in slugs
    assert "jcronq/string-slug" in slugs
    # Three malformed entries should have been logged.
    assert sum("malformed repo entry" in s for s in seen) == 4


def test_load_dispatcher_repos_empty_repos_list_returns_fallback(
    tmp_path: pathlib.Path,
) -> None:
    """Empty ``repos`` list ⇒ fallback (not zero-config crash)."""
    cfg = tmp_path / "alice.config.json"
    cfg.write_text(json.dumps({"sm_dispatcher": {"repos": []}}))
    repos = load_dispatcher_repos(config_path=cfg, log=lambda _msg: None)
    assert repos[0].slug == DEFAULT_REPO


def test_load_dispatcher_repos_all_malformed_returns_fallback(
    tmp_path: pathlib.Path,
) -> None:
    """If every entry is bad, fall back rather than ship a zero-repo list."""
    cfg = tmp_path / "alice.config.json"
    cfg.write_text(
        json.dumps(
            {
                "sm_dispatcher": {
                    "repos": [
                        {"slug": "no-slash", "checkout_path": "/tmp/x"},
                        42,
                    ]
                }
            }
        )
    )
    repos = load_dispatcher_repos(config_path=cfg, log=lambda _msg: None)
    assert repos[0].slug == DEFAULT_REPO


# ---------------------------------------------------------------------------
# run() — relaxed-label mode
# ---------------------------------------------------------------------------


def _issue_without_sm_label(number: int) -> dict:
    """A trusted-author issue carrying no canonical sm:* label.

    This is the shape ``gh_list_sm_issues`` will hand back for a
    cozyhem-engine ticket once we wire a relaxed listing — the strict
    label gate would reject it; relaxed mode should skip silently.
    """
    return {
        "number": number,
        "title": "no labels here",
        "labels": [{"name": "art:code"}],  # art:* present, sm:* absent
        "author": {"login": "jcronq"},
        "createdAt": "2026-05-19T10:00:00Z",
    }


def test_run_strict_mode_rejects_issue_without_sm_label(
    tmp_path: pathlib.Path,
) -> None:
    """Pre-#261 behavior: missing sm:* label ⇒ logged rejection + skipped_trust."""
    state_path = tmp_path / "sm-dispatcher-state.json"
    issues = [_issue_without_sm_label(99)]
    logs: list[str] = []
    exit_code, report = sm.run(
        repo="jcronq/alice",
        state_path=state_path,
        enable_spawn=False,
        enable_verify=False,
        enable_cleanup=False,
        enable_rebase=False,
        list_issues=lambda _repo: issues,
        post_comment=lambda *args, **kw: None,
        labels_configured=True,
        log=logs.append,
    )

    assert exit_code == 0
    assert report.polled == 1
    assert report.skipped_trust == 1
    assert any("expected exactly one whitelisted sm:* label" in s for s in logs)


def test_run_relaxed_mode_skips_issue_without_sm_label_silently(
    tmp_path: pathlib.Path,
) -> None:
    """#261: missing sm:* label in relaxed mode ⇒ no log + skipped_trust unchanged.

    Issues without canonical labels go through silently; the dispatcher
    treats the label set as a Speaking/Thinking convenience for repos
    that haven't been migrated onto the SM v2 taxonomy.
    """
    state_path = tmp_path / "sm-dispatcher-state.json"
    issues = [_issue_without_sm_label(100)]
    logs: list[str] = []
    exit_code, report = sm.run(
        repo="jcronq/cozyhem-engine",
        state_path=state_path,
        enable_spawn=False,
        enable_verify=False,
        enable_cleanup=False,
        enable_rebase=False,
        list_issues=lambda _repo: issues,
        post_comment=lambda *args, **kw: None,
        labels_configured=False,
        log=logs.append,
    )

    assert exit_code == 0
    assert report.polled == 1
    # Relaxed-mode quietly drops the issue — no skipped_trust increment,
    # no per-issue rejection log.
    assert report.skipped_trust == 0
    assert not any("expected exactly one whitelisted sm:* label" in s for s in logs)
