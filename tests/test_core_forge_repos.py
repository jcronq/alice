import json

import pytest

from core import forge_repos as fr


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("ALICE_CONFIG", str(tmp_path / "no-such-alice.env"))
    for k in ("ALICE_FORGE_REPOS", "ALICE_FORGE_REPO", "ALICE_BUG_FIX_REPO"):
        monkeypatch.delenv(k, raising=False)


def _cfg(tmp_path, forge):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "alice.config.json").write_text(json.dumps({"forge": forge}))
    return tmp_path


def test_unconfigured_fails_loud(tmp_path):
    for fn in (fr.forge_repos, fr.primary_repo, fr.bug_fix_repo):
        with pytest.raises(fr.ForgeNotConfigured):
            fn(tmp_path)


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
    monkeypatch.setenv("ALICE_FORGE_REPOS", "bad")
    with pytest.raises(fr.ForgeNotConfigured):
        fr.forge_repos(tmp_path)


def test_malformed_config_falls_back(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "alice.config.json").write_text("{nope")
    with pytest.raises(fr.ForgeNotConfigured):
        fr.primary_repo(tmp_path)


def test_alice_env_file_fallback(tmp_path, monkeypatch):
    env = tmp_path / "alice.env"
    env.write_text(
        "# c\nALICE_FORGE_REPOS=me/a,me/b\nexport ALICE_FORGE_REPO=\"me/b\"\n"
        "ALICE_BUG_FIX_REPO='me/mud'\n"
    )
    monkeypatch.setenv("ALICE_CONFIG", str(env))
    assert fr.forge_repos(tmp_path) == ("me/a", "me/b")
    assert fr.primary_repo(tmp_path) == "me/b"
    assert fr.bug_fix_repo(tmp_path) == "me/mud"


def test_process_env_beats_alice_env_file(tmp_path, monkeypatch):
    env = tmp_path / "alice.env"
    env.write_text("ALICE_FORGE_REPOS=file/repo\n")
    monkeypatch.setenv("ALICE_CONFIG", str(env))
    monkeypatch.setenv("ALICE_FORGE_REPOS", "proc/repo")
    assert fr.forge_repos(tmp_path) == ("proc/repo",)
