"""Tests for bin/codex-to-pi-auth (the auth-format translator).

Subprocess the script with a synthetic codex auth.json and verify
the resulting pi auth.json has the right shape (typed JWT
fields, expires_ms from JWT exp claim, accountId from
chatgpt_account_id).

A real RS256 JWT signature is irrelevant — pi only decodes the
payload to extract claims, and the bridge does the same.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import time

import pytest


BRIDGE = pathlib.Path(__file__).resolve().parents[1] / "bin" / "codex-to-pi-auth"


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def _make_jwt(*, account_id: str, exp_sec: int) -> str:
    """Build a minimal three-part JWT-shaped string (no signature
    verification — the bridge only decodes the payload)."""
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": "https://auth.openai.com",
        "exp": exp_sec,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }

    def _b64(obj):
        return (
            base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8"))
            .rstrip(b"=")
            .decode("ascii")
        )

    return f"{_b64(header)}.{_b64(payload)}.fake-signature"


def _run_bridge(*, codex_path: pathlib.Path, pi_path: pathlib.Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["node", str(BRIDGE), "--codex", str(codex_path), "--pi", str(pi_path)],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(pi_path.parent.parent)},
    )
    return proc.returncode, proc.stderr


def test_bridge_writes_pi_auth_with_decoded_jwt_fields(tmp_path) -> None:
    codex_path = tmp_path / "codex" / "auth.json"
    codex_path.parent.mkdir()
    pi_path = tmp_path / "pi-home" / ".pi" / "agent" / "auth.json"

    exp_sec = int(time.time()) + 7 * 86400  # 7 days
    access = _make_jwt(account_id="acct-abc", exp_sec=exp_sec)
    codex_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "id_token": "eyJh.id.tok",
                    "access_token": access,
                    "refresh_token": "rt-secret",
                    "account_id": "acct-abc",
                },
            }
        )
    )

    rc, _stderr = _run_bridge(codex_path=codex_path, pi_path=pi_path)
    assert rc == 0
    assert pi_path.exists()

    written = json.loads(pi_path.read_text())
    cred = written["openai-codex"]
    assert cred["type"] == "oauth"
    assert cred["access"] == access
    assert cred["refresh"] == "rt-secret"
    assert cred["accountId"] == "acct-abc"
    # expires is in milliseconds.
    assert abs(cred["expires"] - exp_sec * 1000) < 1


def test_bridge_preserves_existing_non_codex_entries(tmp_path) -> None:
    codex_path = tmp_path / "codex" / "auth.json"
    codex_path.parent.mkdir()
    pi_dir = tmp_path / "pi-home" / ".pi" / "agent"
    pi_dir.mkdir(parents=True)
    pi_path = pi_dir / "auth.json"
    # Pre-existing anthropic API key should survive the bridge run.
    pi_path.write_text(
        json.dumps({"anthropic": {"type": "api_key", "key": "sk-ant-keepme"}})
    )

    access = _make_jwt(
        account_id="acct-1", exp_sec=int(time.time()) + 86400
    )
    codex_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": access,
                    "refresh_token": "rt",
                    "account_id": "acct-1",
                }
            }
        )
    )

    rc, _ = _run_bridge(codex_path=codex_path, pi_path=pi_path)
    assert rc == 0

    written = json.loads(pi_path.read_text())
    assert written["anthropic"]["key"] == "sk-ant-keepme"
    assert "openai-codex" in written


def test_bridge_exits_nonzero_on_missing_codex_file(tmp_path) -> None:
    pi_path = tmp_path / "pi-home" / ".pi" / "agent" / "auth.json"
    rc, stderr = _run_bridge(
        codex_path=tmp_path / "does-not-exist.json", pi_path=pi_path
    )
    assert rc == 1
    assert "not readable" in stderr.lower()


def test_bridge_exits_nonzero_on_token_without_account_id(tmp_path) -> None:
    codex_path = tmp_path / "codex" / "auth.json"
    codex_path.parent.mkdir()
    pi_path = tmp_path / "pi-home" / ".pi" / "agent" / "auth.json"

    # JWT payload without the ChatGPT auth claim.
    header = (
        base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}')
        .rstrip(b"=")
        .decode("ascii")
    )
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"iss": "https://auth.openai.com", "exp": 9999999999}).encode()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    bad_token = f"{header}.{payload}.sig"
    codex_path.write_text(
        json.dumps(
            {"tokens": {"access_token": bad_token, "refresh_token": "rt"}}
        )
    )

    rc, stderr = _run_bridge(codex_path=codex_path, pi_path=pi_path)
    assert rc == 2
    assert "chatgpt_account_id" in stderr
