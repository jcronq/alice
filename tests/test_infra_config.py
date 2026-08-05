"""Regression tests for :mod:`alice_speaking.infra.config`.

Focus: partial user overrides in ``alice.config.json`` must NOT wipe
the sibling default keys inside a nested sub-dict (see task-0632).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_speaking.infra import config as infra_config
from alice_speaking.infra.config import (
    SPEAKING_DEFAULTS,
    _deep_merge,
    load,
)


# ---------- helpers ----------------------------------------------------------


@pytest.fixture
def env_and_mind(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Point ``load()`` at a tmp ``alice.env`` + tmp ``alice-mind`` so no
    test touches the real user's files.

    Returns ``(env_path, mind_dir)`` — the caller writes an
    ``alice.config.json`` under ``mind_dir/config/`` when it needs one.
    """
    env_path = tmp_path / "alice.env"
    mind_dir = tmp_path / "mind"
    (mind_dir / "config").mkdir(parents=True)
    env_path.write_text(
        f"WORK_DIR={mind_dir}\n"
        f"ALICE_MIND_DIR={mind_dir}\n"
        f"STATE_DIR={tmp_path / 'state'}\n"
    )
    monkeypatch.setenv("ALICE_CONFIG", str(env_path))
    # Isolate any ambient env that would flip auth-mode branches.
    for var in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "SIGNAL_ACCOUNT",
    ):
        monkeypatch.delenv(var, raising=False)
    return env_path, mind_dir


def _write_speaking_json(mind_dir: pathlib.Path, speaking_overrides: dict) -> None:
    (mind_dir / "config" / "alice.config.json").write_text(
        json.dumps({"speaking": speaking_overrides})
    )


# ---------- _deep_merge unit tests ------------------------------------------


def test_deep_merge_partial_sub_dict_preserves_sibling_defaults() -> None:
    """The core bug: user overrides one key inside a nested dict; the
    other keys under that dict must survive."""
    defaults = {"cue_runner": {"enabled": False, "top_n": 3, "timeout_ms": 500}}
    user = {"cue_runner": {"enabled": True}}
    merged = _deep_merge(defaults, user)
    assert merged["cue_runner"] == {
        "enabled": True,
        "top_n": 3,
        "timeout_ms": 500,
    }


def test_deep_merge_user_scalar_overrides_default_scalar() -> None:
    defaults = {"cue_runner": {"top_n": 3}}
    user = {"cue_runner": {"top_n": 10}}
    assert _deep_merge(defaults, user)["cue_runner"]["top_n"] == 10


def test_deep_merge_recurses_into_grandchild_dict() -> None:
    """cue_runner has its own nested sub-dicts (reranker, hebbian,
    typed_edge). Partial overrides at that depth must also preserve
    siblings."""
    defaults = {
        "cue_runner": {
            "enabled": False,
            "reranker": {"enabled": False, "model": "", "timeout_ms": 1500},
        }
    }
    user = {"cue_runner": {"reranker": {"enabled": True}}}
    merged = _deep_merge(defaults, user)
    assert merged["cue_runner"]["reranker"] == {
        "enabled": True,
        "model": "",
        "timeout_ms": 1500,
    }
    # cue_runner top-level defaults preserved too.
    assert merged["cue_runner"]["enabled"] is False


def test_deep_merge_does_not_mutate_inputs() -> None:
    """Called twice with the same defaults dict must not leak first
    call's overrides into second call's baseline."""
    defaults = {"cue_runner": {"enabled": False, "top_n": 3}}
    user1 = {"cue_runner": {"enabled": True}}
    _deep_merge(defaults, user1)
    # defaults is unchanged
    assert defaults == {"cue_runner": {"enabled": False, "top_n": 3}}
    # second merge sees the pristine defaults
    user2 = {"cue_runner": {"top_n": 7}}
    merged2 = _deep_merge(defaults, user2)
    assert merged2["cue_runner"] == {"enabled": False, "top_n": 7}


def test_deep_merge_user_dict_replaces_non_dict_default() -> None:
    """When default is a scalar and user sends a dict, user wins whole."""
    defaults = {"foo": ""}
    user = {"foo": {"a": 1}}
    assert _deep_merge(defaults, user) == {"foo": {"a": 1}}


def test_deep_merge_user_scalar_replaces_dict_default() -> None:
    """Symmetric: user overrides a dict-typed default with a scalar."""
    defaults = {"foo": {"a": 1}}
    user = {"foo": None}
    assert _deep_merge(defaults, user) == {"foo": None}


def test_deep_merge_empty_user_returns_defaults_copy() -> None:
    defaults = {"a": {"b": 1}}
    merged = _deep_merge(defaults, {})
    assert merged == defaults
    assert merged is not defaults


# ---------- load() integration tests ----------------------------------------


def test_load_partial_cue_runner_preserves_default_siblings(env_and_mind) -> None:
    """The reported bug end-to-end: user config sets only
    ``cue_runner.enabled`` and every other default cue_runner key must
    stay populated."""
    _env, mind_dir = env_and_mind
    _write_speaking_json(mind_dir, {"cue_runner": {"enabled": True}})

    cfg = load()

    cue = cfg.speaking["cue_runner"]
    assert cue["enabled"] is True
    # Every other cue_runner default must survive.
    default_cue = SPEAKING_DEFAULTS["cue_runner"]
    for key in default_cue:
        assert key in cue, f"cue_runner.{key} was dropped by shallow merge"
    assert cue["observability"] == default_cue["observability"]
    assert cue["top_n"] == default_cue["top_n"]
    assert cue["per_note_line_cap"] == default_cue["per_note_line_cap"]
    assert cue["packet_token_ceiling"] == default_cue["packet_token_ceiling"]
    # Nested sub-dicts untouched.
    assert cue["reranker"] == default_cue["reranker"]
    assert cue["hebbian"] == default_cue["hebbian"]
    assert cue["typed_edge"] == default_cue["typed_edge"]


def test_load_full_cue_runner_override_wins_but_siblings_preserved(env_and_mind) -> None:
    """User fully specifies some cue_runner fields — their values win,
    and unspecified defaults still survive."""
    _env, mind_dir = env_and_mind
    _write_speaking_json(
        mind_dir, {"cue_runner": {"enabled": True, "top_n": 10}}
    )

    cfg = load()

    cue = cfg.speaking["cue_runner"]
    assert cue["enabled"] is True                                    # user wins
    assert cue["top_n"] == 10                                        # user wins
    assert cue["observability"] is True                              # default preserved
    assert cue["packet_token_ceiling"] == 1000                       # default preserved


def test_load_absent_user_cue_runner_leaves_defaults_intact(env_and_mind) -> None:
    """User config has no ``cue_runner`` at all → defaults verbatim."""
    _env, mind_dir = env_and_mind
    _write_speaking_json(mind_dir, {"model": "some-model"})

    cfg = load()

    assert cfg.speaking["cue_runner"] == SPEAKING_DEFAULTS["cue_runner"]
    assert cfg.speaking["model"] == "some-model"


def test_load_partial_nested_reranker_override_preserves_siblings(env_and_mind) -> None:
    """Recursion depth check: user overrides cue_runner.reranker.enabled
    only; reranker.model and reranker.timeout_ms must survive."""
    _env, mind_dir = env_and_mind
    _write_speaking_json(
        mind_dir, {"cue_runner": {"reranker": {"enabled": True}}}
    )

    cfg = load()

    reranker = cfg.speaking["cue_runner"]["reranker"]
    assert reranker["enabled"] is True
    default_reranker = SPEAKING_DEFAULTS["cue_runner"]["reranker"]
    assert "timeout_ms" in reranker
    assert reranker["timeout_ms"] == default_reranker["timeout_ms"]
    # cue_runner top-level defaults also survived.
    assert cfg.speaking["cue_runner"]["observability"] is True


def test_load_two_consecutive_loads_do_not_contaminate_defaults(
    env_and_mind,
) -> None:
    """Guard against SPEAKING_DEFAULTS mutation: load with a user
    override, then load again with a blank config — the second load
    must see pristine defaults."""
    _env, mind_dir = env_and_mind
    _write_speaking_json(mind_dir, {"cue_runner": {"top_n": 99}})
    load()

    # Overwrite user config with empty speaking, load again.
    _write_speaking_json(mind_dir, {})
    cfg2 = load()

    assert cfg2.speaking["cue_runner"]["top_n"] == SPEAKING_DEFAULTS["cue_runner"]["top_n"]
    # SPEAKING_DEFAULTS itself is unmutated at the module level.
    assert (
        infra_config.SPEAKING_DEFAULTS["cue_runner"]["top_n"]
        == SPEAKING_DEFAULTS["cue_runner"]["top_n"]
    )
