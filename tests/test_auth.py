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

from core.config.auth import (
    AuthConfigError,
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


def test_no_creds_no_hint_returns_none_mode(clean_env, empty_env_file) -> None:
    auth = ensure_auth_env(empty_env_file)
    assert auth.mode == "none"


def test_find_auth_env_with_mode_hint_does_not_mutate_env(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    """``find_auth_env`` reports what would happen but doesn't mutate
    os.environ — only ``ensure_auth_env`` does. Pinning this so future
    refactors don't accidentally cross the wires."""
    auth = find_auth_env(empty_env_file, mode_hint="bedrock", aws_region="us-east-1")
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


def test_find_auth_env_base_url_kwarg_overrides_env(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    """Plan 06 fix: per-hemisphere ``base_url`` from ``model.yml`` was
    silently ignored before. With the kwarg threaded through, a caller
    passing ``base_url=...`` overrides whatever's in the process env."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    auth = find_auth_env(
        empty_env_file,
        mode_hint="api",
        base_url="https://override.example.com/v1",
    )
    assert auth.base_url == "https://override.example.com/v1"


def test_find_auth_env_base_url_falls_back_to_env_when_not_passed(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    """Without the ``base_url`` kwarg, existing env-derived behaviour
    is preserved."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.example.com/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    auth = find_auth_env(empty_env_file, mode_hint="api")
    assert auth.base_url == "https://env.example.com/v1"


def test_ensure_auth_env_base_url_kwarg_writes_override_into_env(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    ensure_auth_env(
        empty_env_file,
        mode_hint="api",
        base_url="https://override.example.com/v1",
    )
    import os

    assert os.environ.get("ANTHROPIC_BASE_URL") == "https://override.example.com/v1"


# Issue #427: subscription mode_hint with empty oauth_token.


def test_subscription_hint_with_api_creds_escalates_to_api(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file, caplog
) -> None:
    """The exact bug from #427: a fresh mind's env_aware default picks
    subscription, the daemon hands mode_hint='subscription' to
    ensure_auth_env, but the operator has wired api creds. Escalate
    to api rather than wiping ANTHROPIC_* + writing empty OAuth."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://litellm.example.com/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-abc")
    with caplog.at_level("ERROR", logger="core.config.auth"):
        auth = find_auth_env(empty_env_file, mode_hint="subscription")
    assert auth.mode == "api"
    assert auth.api_key == "key-abc"
    assert auth.base_url == "https://litellm.example.com/v1"
    # Log breadcrumb so the operator can find the mismatch.
    assert any(
        "escalating to api mode" in rec.message for rec in caplog.records
    ), caplog.records


def test_subscription_hint_with_only_auth_token_escalates_to_api(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    """ANTHROPIC_AUTH_TOKEN alone (bearer-style proxy auth) is enough
    to escalate — same as ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-xyz")
    auth = find_auth_env(empty_env_file, mode_hint="subscription")
    assert auth.mode == "api"
    assert auth.auth_token == "bearer-xyz"


def test_subscription_hint_with_no_creds_anywhere_raises(
    clean_env, empty_env_file
) -> None:
    """No oauth_token, no api creds, explicit subscription hint: this
    is a real config error — raise so the operator sees it rather than
    silently writing an empty token."""
    with pytest.raises(AuthConfigError, match="subscription mode requested"):
        find_auth_env(empty_env_file, mode_hint="subscription")


def test_subscription_hint_with_valid_oauth_token_does_not_escalate(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    """Sanity: an honest subscription user keeps the existing behaviour
    even when stale ANTHROPIC_* vars are also set in env."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-key")
    auth = find_auth_env(empty_env_file, mode_hint="subscription")
    assert auth.mode == "subscription"
    assert auth.oauth_token == "tok-real"


def test_ensure_subscription_with_api_creds_does_not_clear_anthropic_vars(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    """End-to-end: the daemon path that triggered #427. With api creds
    in env and an explicit subscription hint, ensure_auth_env should
    escalate to api and leave ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL
    populated in os.environ — not pop them silently."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://litellm.example.com/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-abc")
    auth = ensure_auth_env(empty_env_file, mode_hint="subscription")
    assert auth.mode == "api"
    import os

    assert os.environ.get("ANTHROPIC_API_KEY") == "key-abc"
    assert os.environ.get("ANTHROPIC_BASE_URL") == "https://litellm.example.com/v1"
    # Subscription-mode empty token must not be written.
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") is None


def test_ensure_subscription_with_valid_token_clears_anthropic_vars(
    clean_env, monkeypatch: pytest.MonkeyPatch, empty_env_file
) -> None:
    """Regression-guard for the existing subscription-clears-api
    behaviour: when the operator genuinely wants subscription and has
    a real OAuth token, the defensive escalation path must not fire."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-key")
    auth = ensure_auth_env(empty_env_file, mode_hint="subscription")
    assert auth.mode == "subscription"
    import os

    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-real"
    assert os.environ.get("ANTHROPIC_API_KEY") is None
