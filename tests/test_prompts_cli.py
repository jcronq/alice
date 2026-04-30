"""Phase 8 of plan 04: bin/alice-prompts inventory CLI.

Tests against ``alice_prompts.cli.main`` directly (rather than
shelling out to the bash wrapper) — the wrapper just selects a
Python interpreter and exec's the same entry point. Coverage
matches the three subcommands.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from alice_prompts import cli


# ---------------------------------------------------------------------------
# list


def test_list_includes_every_shipped_template(capsys):
    rc = cli.main(["list"])
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    # Anchor a few known names so a future rename surfaces.
    for expected in (
        "thinking.quick",
        "speaking.compact",
        "speaking.turn.cli",
        "viewer.narrative.window",
        "meta.sanity",
    ):
        assert expected in out, f"{expected} not listed: {out}"
    # Sorted output (the loader returns sorted; pin so that contract
    # doesn't drift).
    assert out == sorted(out)


# ---------------------------------------------------------------------------
# render


def test_render_basic_template(capsys):
    rc = cli.main(["render", "thinking.quick"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "Reply exactly: QUICK-OK"


def test_render_with_yaml_context(capsys, tmp_path: pathlib.Path):
    """Render the compact template with a custom personae context
    via a YAML context file. The override should reach the
    template through the loader's per-call context."""
    ctx_file = tmp_path / "ctx.yaml"
    ctx_file.write_text(
        "agent:\n  name: Bob\nuser:\n  name: Alice's Owner\n"
    )

    rc = cli.main(
        [
            "render",
            "speaking.compact",
            "--context-file",
            str(ctx_file),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Alice's Owner" in out
    # The package-level loader's persona-placeholder defaults
    # should be overridden by the per-call context, so the
    # original "the operator" stand-in shouldn't appear.
    assert "the operator" not in out


def test_render_with_json_context(capsys, tmp_path: pathlib.Path):
    """Same shape with a JSON context file."""
    ctx_file = tmp_path / "ctx.json"
    ctx_file.write_text(
        json.dumps({"caps": {"max_message_bytes": 999}})
    )

    rc = cli.main(
        [
            "render",
            "speaking.capability.cli",
            "--context-file",
            str(ctx_file),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "999 bytes" in out


def test_render_rejects_non_mapping_context(tmp_path: pathlib.Path):
    """Top-level YAML/JSON has to be a mapping — anything else is
    a usage error, not a silent surprise."""
    ctx_file = tmp_path / "ctx.json"
    ctx_file.write_text("[1, 2, 3]")
    with pytest.raises(SystemExit, match="must contain a top-level mapping"):
        cli.main(
            [
                "render",
                "thinking.quick",
                "--context-file",
                str(ctx_file),
            ]
        )


# ---------------------------------------------------------------------------
# validate


def test_validate_all_shipped_templates(capsys):
    """Every template in ``src/alice_prompts/templates/`` must
    parse cleanly. Recurrence guard for syntax bugs that ship
    through to runtime."""
    rc = cli.main(["validate"])
    err = capsys.readouterr().err
    assert rc == 0, f"validate failed: {err}"


def test_validate_catches_syntax_error(tmp_path: pathlib.Path, monkeypatch, capsys):
    """A template with broken Jinja syntax surfaces in stderr and
    exits non-zero. We point the package-level loader at a tmp
    tree containing one bad template."""
    import alice_prompts as ap

    # Reset the package-level loader so it picks up the tmp
    # defaults dir on next access.
    monkeypatch.setattr(ap, "_default_loader", None)
    bad_dir = tmp_path / "templates"
    (bad_dir / "broken").mkdir(parents=True)
    (bad_dir / "broken" / "syntax.md.j2").write_text("{% if missing_endif %}\n")

    monkeypatch.setattr(ap, "DEFAULTS_DIR", bad_dir)

    rc = cli.main(["validate"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "broken.syntax" in err
