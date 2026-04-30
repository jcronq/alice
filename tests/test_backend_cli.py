"""Phase N of plan 06: ``alice-backend show`` CLI smoke."""

from __future__ import annotations

import io
import pathlib
from contextlib import redirect_stdout

from alice_core.config.cli import main as backend_cli


def test_show_handles_missing_model_yml(tmp_path: pathlib.Path) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = backend_cli(["--mind", str(tmp_path), "show"])
    assert rc == 0
    out = buf.getvalue()
    assert "subscription default" in out
    assert "speaking: backend=subscription" in out
    assert "thinking: backend=subscription" in out
    assert "viewer: backend=subscription" in out


def test_show_outputs_per_hemisphere_config(tmp_path: pathlib.Path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "model.yml").write_text(
        "speaking:\n"
        "  backend: api\n"
        "  model: claude-test\n"
        "thinking:\n"
        "  backend: bedrock\n"
        "  model: anthropic.test-v1:0\n"
        "  region: eu-west-1\n"
        "backends:\n"
        "  api:\n"
        "    base_url: https://litellm.example.com/v1\n"
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = backend_cli(["--mind", str(tmp_path), "show"])
    assert rc == 0
    out = buf.getvalue()
    assert "speaking: backend=api" in out
    assert "model=claude-test" in out
    assert "base_url=https://litellm.example.com/v1" in out
    assert "thinking: backend=bedrock" in out
    assert "region=eu-west-1" in out
