"""Dispatcher ↔ pricing integration tests.

Covers:
* dispatch time: ``estimate_usd`` is computed from the rendered prompt
  and written to frontmatter alongside ``worker_model`` + token
  budget sidecars.
* pr-url adoption: ``actual_usd`` + ``actual_tokens`` land in the
  frontmatter when the worker emitted an SDK envelope with ``usage``.
* unparseable: ``actual_usd`` is set to ``null`` with a
  ``cost_attribution_note`` explaining what went wrong.
* tick log: the "done" line carries running P&L totals.
"""

from __future__ import annotations

import json

from alice_bug_fix_dispatcher import frontmatter as fm
from alice_bug_fix_dispatcher.main import (
    DispatcherConfig,
    _compute_actuals_patch,
    _read_config_pricing,
    _sum_pricing_totals,
    tick,
)


BUG_BODY = """# bug-{n} — {title}

**Status:** open.

## Report

> cake-box didn't swap for the empty tin after giving to michael

## Who / where

- Reported 2026-11-02 12:12 EST
- Build: 5ea667a61aaab3315ade5ddf046abf04b8565ee9

## Likely fix surface

Michael's accept-the-box node needs the inventory swap.
"""


def write_bug(status_dir, report_id, *, status="open", title="placeholder"):
    front = ["---", f"report_id: {report_id}", f'title: "{title}"', f"status: {status}", "---"]
    p = status_dir / f"bug-{report_id}.md"
    p.write_text("\n".join(front) + "\n\n" + BUG_BODY.format(n=report_id, title=title))
    return p


def _make_cfg(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    template_path = tmp_path / "template.md"
    template_path.write_text(
        "worker for bug-{{bug_id}} ({{title}})\n"
        "report: {{report_text}}\n"
        "state: {{captured_state}}\n"
    )
    return DispatcherConfig(
        status_dir=status,
        journal_path=tmp_path / "journal.log",
        pause_sentinel=tmp_path / ".paused",
        config_json=tmp_path / "config.json",
        worker_scratch_root=tmp_path / "scratch",
        worker_prompt_template=template_path,
        stall_hours=2,
    )


# ---------------------------------------------------------------- dispatch estimate


def test_dispatch_writes_estimate_usd_to_frontmatter(tmp_path):
    cfg = _make_cfg(tmp_path)
    write_bug(cfg.status_dir, 1, title="demo")
    spawned = {}

    def spawn(report_id, scratch, prompt):
        spawned["prompt"] = prompt
        return 12345

    report = tick(cfg, log=lambda _: None, spawn=spawn)
    assert report.dispatched == [1]
    text = (cfg.status_dir / "bug-1.md").read_text()
    assert "estimate_usd:" in text
    assert "worker_model: claude-opus-4-7" in text
    assert "estimate_prompt_tokens:" in text
    assert "estimate_output_tokens: 8000" in text
    # Round-trip the frontmatter and sanity-check the estimate is >0.
    fields, _, _ = fm.parse(text)
    assert float(fields["estimate_usd"]) > 0


def test_dispatch_log_line_includes_estimate_usd(tmp_path):
    cfg = _make_cfg(tmp_path)
    write_bug(cfg.status_dir, 1, title="demo")
    lines = []
    tick(cfg, log=lines.append, spawn=lambda *_: 42)
    joined = "\n".join(lines)
    assert "dispatched worker pid=42" in joined
    assert "model=claude-opus-4-7" in joined
    assert "estimate_usd=$" in joined


def test_tick_done_line_carries_pnl_totals(tmp_path):
    cfg = _make_cfg(tmp_path)
    write_bug(cfg.status_dir, 1, title="demo")
    lines = []
    tick(cfg, log=lines.append, spawn=lambda *_: 42)
    done_line = next(line for line in lines if "done — scanned=" in line)
    assert "total_estimate_usd=$" in done_line
    assert "total_actual_usd=" in done_line


def test_dispatch_pricing_uses_config_json_override(tmp_path):
    cfg = _make_cfg(tmp_path)
    cfg.config_json.write_text(
        json.dumps(
            {
                "bug_fix_dispatcher": {
                    "worker_model": "claude-sonnet-4-6",
                    "expected_output_tokens": 4000,
                }
            }
        )
    )
    write_bug(cfg.status_dir, 1, title="demo")
    tick(cfg, log=lambda _: None, spawn=lambda *_: 1)
    text = (cfg.status_dir / "bug-1.md").read_text()
    assert "worker_model: claude-sonnet-4-6" in text
    assert "estimate_output_tokens: 4000" in text


def test_read_config_pricing_defaults_to_dataclass(tmp_path):
    cfg = _make_cfg(tmp_path)
    # No config file.
    model, out = _read_config_pricing(cfg)
    assert model == "claude-opus-4-7"
    assert out == 8_000


def test_read_config_pricing_tolerates_malformed_json(tmp_path):
    cfg = _make_cfg(tmp_path)
    cfg.config_json.write_text("this is not { valid json")
    model, out = _read_config_pricing(cfg)
    assert model == "claude-opus-4-7"
    assert out == 8_000


# ---------------------------------------------------------------- actuals adoption


def test_adopt_pr_url_also_writes_actual_usd(tmp_path):
    cfg = _make_cfg(tmp_path)
    bug_path = write_bug(cfg.status_dir, 2, status="in-progress", title="live")
    fm.update(
        bug_path,
        {
            "dispatched_at": "2026-11-02T17:00:00+00:00",
            "worker_task_id": "pid:42",
            "worker_model": "claude-opus-4-7",
        },
    )
    scratch = cfg.worker_scratch_root / "bug-2"
    scratch.mkdir(parents=True)
    scratch.joinpath("result.json").write_text(
        json.dumps(
            {
                "type": "result",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 10_000, "output_tokens": 8_000},
                "result": json.dumps(
                    {"pr_url": "https://github.com/jcronq/modern-mud/pull/42"}
                ),
            }
        )
    )

    tick(cfg, log=lambda _: None)
    text = bug_path.read_text()
    fields, _, _ = fm.parse(text)
    assert fields.get("pr_url") == "https://github.com/jcronq/modern-mud/pull/42"
    assert "actual_usd" in fields
    # 10K input + 8K output on Opus 4.7 = $0.25
    assert abs(float(fields["actual_usd"]) - 0.25) < 1e-4
    assert "input=10000" in fields["actual_tokens"]
    assert "output=8000" in fields["actual_tokens"]


def test_adopt_pr_url_without_tokens_writes_null_actual_usd(tmp_path):
    """A worker that emits a PR URL but no ``usage`` block → actual_usd=null + note."""
    cfg = _make_cfg(tmp_path)
    bug_path = write_bug(cfg.status_dir, 3, status="in-progress", title="live")
    fm.update(
        bug_path,
        {
            "dispatched_at": "2026-11-02T17:00:00+00:00",
            "worker_task_id": "pid:42",
            "worker_model": "claude-opus-4-7",
        },
    )
    scratch = cfg.worker_scratch_root / "bug-3"
    scratch.mkdir(parents=True)
    # ``result.json`` carries a PR URL but no usage block.
    scratch.joinpath("result.json").write_text(
        json.dumps({"pr_url": "https://github.com/jcronq/modern-mud/pull/99"})
    )
    scratch.joinpath("stderr.log").write_text("no token lines here either\n")

    tick(cfg, log=lambda _: None)
    text = bug_path.read_text()
    fields, _, _ = fm.parse(text)
    assert fields.get("pr_url") == "https://github.com/jcronq/modern-mud/pull/99"
    # ``actual_usd`` is dropped from the frontmatter when None (sentinel
    # for "unparseable"); ``cost_attribution_note`` tells Jason why.
    assert "actual_usd" not in fields
    assert "cost_attribution_note" in fields
    assert "no parseable token counts" in fields["cost_attribution_note"]


def test_compute_actuals_patch_known_model(tmp_path):
    cfg = _make_cfg(tmp_path)
    scratch = tmp_path / "scratch" / "bug-7"
    scratch.mkdir(parents=True)
    scratch.joinpath("result.json").write_text(
        json.dumps(
            {
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 2000, "output_tokens": 1000},
            }
        )
    )
    patch = _compute_actuals_patch(
        report_id=7,
        scratch_dir=scratch,
        fields={},
        cfg=cfg,
        log=lambda _: None,
    )
    # 2000/1M * 5 + 1000/1M * 25 = 0.01 + 0.025 = 0.035
    assert abs(float(patch["actual_usd"]) - 0.035) < 1e-6
    assert "input=2000" in patch["actual_tokens"]
    assert "output=1000" in patch["actual_tokens"]
    assert patch["cost_attribution_note"] is None


def test_compute_actuals_patch_falls_back_to_dispatch_model(tmp_path):
    """If envelope has no ``model`` field, use ``worker_model`` from frontmatter."""
    cfg = _make_cfg(tmp_path)
    scratch = tmp_path / "scratch" / "bug-8"
    scratch.mkdir(parents=True)
    scratch.joinpath("result.json").write_text(
        json.dumps({"usage": {"input_tokens": 1000, "output_tokens": 500}})
    )
    patch = _compute_actuals_patch(
        report_id=8,
        scratch_dir=scratch,
        fields={"worker_model": "claude-sonnet-4-6"},
        cfg=cfg,
        log=lambda _: None,
    )
    # 1000/1M * 3 + 500/1M * 15 = 0.003 + 0.0075 = 0.0105
    assert abs(float(patch["actual_usd"]) - 0.0105) < 1e-6


def test_compute_actuals_patch_local_model_zero_cost(tmp_path):
    cfg = _make_cfg(tmp_path)
    scratch = tmp_path / "scratch" / "bug-9"
    scratch.mkdir(parents=True)
    scratch.joinpath("result.json").write_text(
        json.dumps(
            {
                "model": "hermes",
                "usage": {"input_tokens": 100_000, "output_tokens": 50_000},
            }
        )
    )
    patch = _compute_actuals_patch(
        report_id=9,
        scratch_dir=scratch,
        fields={},
        cfg=cfg,
        log=lambda _: None,
    )
    assert float(patch["actual_usd"]) == 0.0


def test_compute_actuals_patch_unknown_model_leaves_note(tmp_path):
    cfg = _make_cfg(tmp_path)
    scratch = tmp_path / "scratch" / "bug-10"
    scratch.mkdir(parents=True)
    scratch.joinpath("result.json").write_text(
        json.dumps(
            {
                "model": "gpt-5",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
        )
    )
    patch = _compute_actuals_patch(
        report_id=10,
        scratch_dir=scratch,
        fields={},
        cfg=cfg,
        log=lambda _: None,
    )
    assert patch["actual_usd"] is None
    assert "gpt-5" in patch["cost_attribution_note"]


def test_compute_actuals_patch_handles_empty_scratch(tmp_path):
    cfg = _make_cfg(tmp_path)
    scratch = tmp_path / "scratch" / "bug-11"
    scratch.mkdir(parents=True)
    patch = _compute_actuals_patch(
        report_id=11,
        scratch_dir=scratch,
        fields={},
        cfg=cfg,
        log=lambda _: None,
    )
    assert patch["actual_usd"] is None
    assert "no parseable token counts" in patch["cost_attribution_note"]


# ---------------------------------------------------------------- totals helper


def test_sum_pricing_totals_rolls_up_bug_files(tmp_path):
    cfg = _make_cfg(tmp_path)
    write_bug(cfg.status_dir, 1, title="a")
    fm.update(cfg.status_dir / "bug-1.md", {"estimate_usd": "0.42", "actual_usd": "0.87"})
    write_bug(cfg.status_dir, 2, title="b")
    fm.update(cfg.status_dir / "bug-2.md", {"estimate_usd": "1.00"})
    totals = _sum_pricing_totals(cfg.status_dir)
    assert abs(totals["estimate"] - 1.42) < 1e-9
    assert abs(totals["actual"] - 0.87) < 1e-9


def test_sum_pricing_totals_empty_status_dir(tmp_path):
    cfg = _make_cfg(tmp_path)
    totals = _sum_pricing_totals(cfg.status_dir)
    assert totals == {"estimate": 0.0, "actual": 0.0}
