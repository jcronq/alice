"""Tests for the pricing surface on ``/bugs.md``.

The renderer picks up ``estimate_usd`` + ``actual_usd`` from each
bug's frontmatter and tucks them into the italic summary tail.
Pipeline P&L total is appended once at the bottom.
"""

from __future__ import annotations

from pathlib import Path

from status_site.server import (
    _format_pricing_segment,
    _format_usd_local,
    _sum_pricing,
    collect_bugs,
    render_bugs_md,
)


# ---------------------------------------------------------------- helpers


def write_bug(
    status_dir: Path,
    report_id: int,
    *,
    status: str = "open",
    title: str = "placeholder",
    summary: str | None = None,
    estimate_usd: str | None = None,
    actual_usd: str | None = None,
    pr_url: str | None = None,
) -> Path:
    lines = ["---", f"report_id: {report_id}", f'title: "{title}"', f"status: {status}"]
    if summary is not None:
        lines.append(f'summary: "{summary}"')
    if estimate_usd is not None:
        lines.append(f"estimate_usd: {estimate_usd}")
    if actual_usd is not None:
        lines.append(f"actual_usd: {actual_usd}")
    if pr_url is not None:
        lines.append(f"pr_url: {pr_url}")
    lines.append("---")
    lines.append("")
    lines.append(f"# bug-{report_id} — {title}\n")
    p = status_dir / f"bug-{report_id}.md"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------- formatter


def test_format_usd_local_zero():
    assert _format_usd_local(0) == "$0.00"


def test_format_usd_local_normal():
    assert _format_usd_local(0.42) == "$0.42"
    assert _format_usd_local(1.234) == "$1.23"
    assert _format_usd_local(42) == "$42.00"


def test_format_usd_local_sub_cent():
    rendered = _format_usd_local(0.001234)
    assert rendered.startswith("$0.00")


def test_format_pricing_segment_both_present():
    assert _format_pricing_segment("0.42", "0.87") == "$0.42 est / $0.87 actual"


def test_format_pricing_segment_only_estimate():
    assert _format_pricing_segment("0.42", "") == "$0.42 est"


def test_format_pricing_segment_only_actual():
    assert _format_pricing_segment("", "0.87") == "$0.87 actual"


def test_format_pricing_segment_neither_present():
    assert _format_pricing_segment("", "") == ""


def test_format_pricing_segment_null_sentinel_treated_as_absent():
    """``actual_usd: null`` (from an unparseable worker) renders cleanly."""
    assert _format_pricing_segment("0.42", "null") == "$0.42 est"
    assert _format_pricing_segment("null", "null") == ""


def test_format_pricing_segment_malformed_values_treated_as_absent():
    """Non-float junk doesn't crash the render."""
    assert _format_pricing_segment("not-a-number", "0.5") == "$0.50 actual"
    assert _format_pricing_segment("not-a-number", "") == ""


# ---------------------------------------------------------------- collect_bugs


def test_collect_bugs_picks_up_pricing_fields(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, estimate_usd="0.42", actual_usd="0.87")
    buckets, _ = collect_bugs(status)
    assert buckets["open"][0]["estimate_usd"] == "0.42"
    assert buckets["open"][0]["actual_usd"] == "0.87"


def test_collect_bugs_pricing_fields_default_to_empty(tmp_path):
    """A bug without pricing frontmatter still parses."""
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 2)
    buckets, _ = collect_bugs(status)
    assert buckets["open"][0]["estimate_usd"] == ""
    assert buckets["open"][0]["actual_usd"] == ""


# ---------------------------------------------------------------- render


def test_render_shows_both_estimate_and_actual(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(
        status, 1,
        status="fixed", title="done one",
        summary="worker finished",
        estimate_usd="0.42", actual_usd="0.87",
    )
    out, _ = render_bugs_md(status)
    assert "$0.42 est / $0.87 actual" in out
    assert "worker finished" in out


def test_render_shows_estimate_only_before_actual_lands(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(
        status, 2,
        status="in-progress", title="working",
        summary="still running",
        estimate_usd="1.23",
    )
    out, _ = render_bugs_md(status)
    assert "$1.23 est" in out
    assert "actual" not in out.split("## Fixed", 1)[0].split("## In progress", 1)[1]


def test_render_shows_actual_only_when_estimate_missing(tmp_path):
    """A hand-created bug (never dispatched) that got actuals attached later."""
    status = tmp_path / "status"
    status.mkdir()
    write_bug(
        status, 3,
        status="fixed", title="imported",
        summary="came from history",
        actual_usd="2.00",
    )
    out, _ = render_bugs_md(status)
    assert "$2.00 actual" in out
    # No "est" tag.
    fixed_section = out.split("## Fixed", 1)[1].split("## ", 1)[0]
    assert "est /" not in fixed_section
    assert "est*" not in fixed_section
    assert "est —" not in fixed_section


def test_render_omits_pricing_segment_when_both_missing(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 4, status="open", title="unpriced", summary="never dispatched")
    out, _ = render_bugs_md(status)
    # Line still renders.
    assert "- [ ] **bug-4 — unpriced**: *never dispatched*" in out
    # No dollar signs anywhere (no priced bugs → no P&L footer either).
    assert "$" not in out


def test_render_pricing_segment_appended_after_pr_url(tmp_path):
    """In-progress bugs with both PR URL and pricing show both, in order."""
    status = tmp_path / "status"
    status.mkdir()
    write_bug(
        status, 5,
        status="in-progress", title="live",
        summary="running",
        pr_url="https://github.com/jcronq/modern-mud/pull/42",
        estimate_usd="0.50",
    )
    out, _ = render_bugs_md(status)
    # in-progress + pr_url now renders under Waiting on review.
    section = out.split("## Waiting on review", 1)[1].split("## In progress", 1)[0]
    assert "[draft PR](https://github.com/jcronq/modern-mud/pull/42)" in section
    assert "$0.50 est" in section
    # PR URL comes before the pricing segment in the italic tail.
    pr_pos = section.index("[draft PR]")
    price_pos = section.index("$0.50 est")
    assert pr_pos < price_pos


def test_render_pricing_tail_formatted_with_em_dash(tmp_path):
    """Expected shape: ``: *<summary> — $0.42 est / $0.87 actual*``."""
    status = tmp_path / "status"
    status.mkdir()
    write_bug(
        status, 6, status="fixed", title="t", summary="summary-text",
        estimate_usd="0.42", actual_usd="0.87",
    )
    out, _ = render_bugs_md(status)
    assert "*summary-text — $0.42 est / $0.87 actual*" in out


# ---------------------------------------------------------------- Pipeline P&L


def test_pipeline_pnl_rolls_up_across_all_bugs(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, estimate_usd="1.00", actual_usd="1.50")
    write_bug(status, 2, status="fixed", estimate_usd="2.00", actual_usd="0.80")
    out, _ = render_bugs_md(status)
    # 3.00 total estimate, 2.30 total actual, 2.30-3.00 = -0.70 delta.
    assert "**Pipeline P&L (session):**" in out
    assert "total_estimate: $3.00" in out
    assert "total_actual: $2.30" in out
    assert "delta: -$0.70" in out


def test_pipeline_pnl_omitted_when_no_bug_is_priced(tmp_path):
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1)
    out, _ = render_bugs_md(status)
    assert "Pipeline P&L" not in out


def test_pipeline_pnl_counts_estimate_without_actual(tmp_path):
    """Mid-flight bugs with only ``estimate_usd`` still show in the roll-up."""
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, status="in-progress", estimate_usd="0.42")
    out, _ = render_bugs_md(status)
    assert "Pipeline P&L" in out
    assert "total_estimate: $0.42" in out
    assert "total_actual: $0.00" in out


def test_pipeline_pnl_ignores_null_sentinels(tmp_path):
    """``actual_usd: null`` doesn't crash the sum."""
    status = tmp_path / "status"
    status.mkdir()
    write_bug(status, 1, estimate_usd="0.42", actual_usd="null")
    out, _ = render_bugs_md(status)
    assert "total_estimate: $0.42" in out
    assert "total_actual: $0.00" in out


def test_sum_pricing_rolls_up_dict_input():
    buckets = {
        "open": [{"estimate_usd": "0.10", "actual_usd": ""}],
        "fixed": [
            {"estimate_usd": "0.20", "actual_usd": "0.15"},
            {"estimate_usd": "", "actual_usd": "0.05"},
        ],
    }
    totals = _sum_pricing(buckets)
    assert abs(totals["estimate"] - 0.30) < 1e-9
    assert abs(totals["actual"] - 0.20) < 1e-9
