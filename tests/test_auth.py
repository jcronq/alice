"""Phase D of plan 06: ``ensure_auth_env`` understands ``mode_hint``.

The previous behaviour (mode picked implicitly from env vars) is
preserved when ``mode_hint is None``. Bedrock is the new third mode;
api/subscription got minor cleanup so the bedrock vars are also
cleared on switch.

Each test runs against a clean ``os.environ`` (fixtures + patching)
so module-level state from earlier tests doesn't leak in.
"""

from __future__ import annotations

import pathlib

import pytest

from alice_core.config.auth import (
    ensure_auth_env,
    find_auth_env,
)


# Vars the tests touch — wiped between tests so no leakage from a
# real CLAUDE_CODE_OAUTH_TOKEN on the developer's machine bleeds in.
_AUTH_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "AWS_REGION",
    "AWS_PROFILE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every auth-related env var so tests start from a known
    blank slate. monkeypatch restores at teardown."""
    for var in _AUTH_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def empty_env_file(tmp_path: pathlib.Path) -> pathlib.Path:
    """An empty ``alice.env`` so ``ensure_auth_env`` doesn't pick up
    the developer's real ``~/.config/alice/alice.env`` on the host."""
    p = tmp_path / "alice.env"
    p.write_text("")
    return p


def test_subscription_mode_implicit_from_env(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    auth = ensure_auth_env(empty_env_file)
    assert auth.mode == "subscription"
    import os

    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-abc"
    # API + Bedrock vars cleared.
    assert os.environ.get("ANTHROPIC_BASE_URL") is None
    assert os.environ.get("CLAUDE_CODE_USE_BEDROCK") is None


def test_api_mode_implicit_from_env(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://litellm.example.com/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-xyz")
    auth = ensure_auth_env(empty_env_file)
    assert auth.mode == "api"
    import os

    assert os.environ.get("ANTHROPIC_BASE_URL") == "https://litellm.example.com/v1"
    assert os.environ.get("ANTHROPIC_API_KEY") == "key-xyz"
    # Subscription + Bedrock vars cleared.
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") is None
    assert os.environ.get("CLAUDE_CODE_USE_BEDROCK") is None


def test_bedrock_mode_via_mode_hint(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    auth = ensure_auth_env(
        empty_env_file,
        mode_hint="bedrock",
        aws_region="us-east-1",
        aws_profile="alice-prod",
    )
    assert auth.mode == "bedrock"
    assert auth.aws_region == "us-east-1"
    assert auth.aws_profile == "alice-prod"
    import os

    assert os.environ["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert os.environ["AWS_REGION"] == "us-east-1"
    assert os.environ["AWS_PROFILE"] == "alice-prod"
    # Subscription + API vars cleared.
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") is None
    assert os.environ.get("ANTHROPIC_API_KEY") is None


def test_bedrock_mode_preserves_aws_creds(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    """boto3's credential chain reads AWS_ACCESS_KEY_ID etc. directly;
    auth.py must not clear or set those — only the SDK-facing flag."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    auth = ensure_auth_env(empty_env_file, mode_hint="bedrock", aws_region="us-east-1")
    assert auth.mode == "bedrock"
    import os

    assert os.environ.get("AWS_ACCESS_KEY_ID") == "AKIA-test"
    assert os.environ.get("AWS_SECRET_ACCESS_KEY") == "secret"


def test_subscription_mode_via_mode_hint_clears_api_and_bedrock(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-key")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    auth = ensure_auth_env(empty_env_file, mode_hint="subscription")
    assert auth.mode == "subscription"
    import os

    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok"
    assert os.environ.get("ANTHROPIC_API_KEY") is None
    assert os.environ.get("CLAUDE_CODE_USE_BEDROCK") is None
    assert os.environ.get("AWS_REGION") is None


def test_api_mode_via_mode_hint_clears_bedrock(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    auth = ensure_auth_env(empty_env_file, mode_hint="api")
    assert auth.mode == "api"
    import os

    assert os.environ.get("CLAUDE_CODE_USE_BEDROCK") is None
    assert os.environ.get("AWS_REGION") is None


def test_no_creds_no_hint_returns_none_mode(
    clean_env, empty_env_file
) -> None:
    auth = ensure_auth_env(empty_env_file)
    assert auth.mode == "none"


def test_find_auth_env_with_mode_hint_does_not_mutate_env(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    """``find_auth_env`` reports what would happen but doesn't mutate
    os.environ — only ``ensure_auth_env`` does. Pinning this so future
    refactors don't accidentally cross the wires."""
    auth = find_auth_env(
        empty_env_file, mode_hint="bedrock", aws_region="us-east-1"
    )
    assert auth.mode == "bedrock"
    assert auth.aws_region == "us-east-1"
    import os

    assert os.environ.get("CLAUDE_CODE_USE_BEDROCK") is None
    assert os.environ.get("AWS_REGION") is None


def test_aws_region_falls_back_to_env_when_not_passed(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    auth = ensure_auth_env(empty_env_file, mode_hint="bedrock")
    assert auth.aws_region == "eu-west-1"
    import os

    assert os.environ.get("AWS_REGION") == "eu-west-1"
