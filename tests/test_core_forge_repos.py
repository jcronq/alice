import json

import pytest

from core import forge_repos as fr


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("ALICE_FORGE_REPOS", "ALICE_FORGE_REPO", "ALICE_BUG_FIX_REPO"):
        monkeypatch.delenv(k, raising=False)


def _cfg(tmp_path, forge):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "alice.config.json").write_text(json.dumps({"forge": forge}))
    return tmp_path


def test_defaults_when_nothing_configured(tmp_path):
    assert fr.forge_repos(tmp_path) == fr.UPSTREAM_REPOS
    assert fr.primary_repo(tmp_path) == "jcronq/alice"
    assert fr.bug_fix_repo(tmp_path) == "jcronq/modern-mud"


def test_config_block(tmp_path):
    m = _cfg(tmp_path, {"repos": ["me/a", "me/b"], "bug_fix_repo": "me/mud"})
    assert fr.forge_repos(m) == ("me/a", "me/b")
    assert fr.primary_repo(m) == "me/a"
    assert fr.bug_fix_repo(m) == "me/mud"


def test_env_beats_config(tmp_path, monkeypatch):
    m = _cfg(tmp_path, {"repos": ["me/a"], "primary": "me/a"})
    monkeypatch.setenv("ALICE_FORGE_REPOS", "x/y, z/w")
    monkeypatch.setenv("ALICE_FORGE_REPO", "z/w")
    assert fr.forge_repos(m) == ("x/y", "z/w")
    assert fr.primary_repo(m) == "z/w"


def test_invalid_entries_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_FORGE_REPOS", "bad, ok/repo, ../x")
    assert fr.forge_repos(tmp_path) == ("ok/repo",)


def test_malformed_config_falls_back(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "alice.config.json").write_text("{nope")
    assert fr.primary_repo(tmp_path) == "jcronq/alice"
