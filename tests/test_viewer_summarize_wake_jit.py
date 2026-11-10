"""Tests for the JIT behavior of viewer.aggregators.summarize_wake.

Default policy (``auto_schedule=False``) must NOT fire a Haiku call on
cache miss — that's the whole point of the JIT rework. The explicit
summarize action (``auto_schedule=True``, used by the ``generate=1``
query param on ``/api/wakes/<id>/summary``) must still fire one.
"""

from __future__ import annotations

from viewer.aggregators import Wake, summarize_wake


class _FakeRunSummary:
    """Minimal shim of the run_summary module.

    Records every ``schedule`` call so a test can assert whether JIT
    actually suppressed the fire-and-forget Haiku dispatch.
    """

    def __init__(self, *, cached: str | None = None):
        self._cached = cached
        self.scheduled: list[str] = []

    def read(self, run_id: str) -> str | None:
        return self._cached

    def schedule(self, run_id: str, events) -> None:
        self.scheduled.append(run_id)


def _wake(wake_id: str = "wake-1", *, ended: bool = True) -> Wake:
    return Wake(
        wake_id=wake_id,
        start_ts=1.0,
        end_ts=2.0 if ended else None,
        status="ended" if ended else "running",
        model=None,
        duration_ms=1000,
        total_cost_usd=None,
        tools=["Read", "Grep"],
        events=[],
    )


def test_default_jit_mode_does_not_schedule_a_haiku_call_on_cache_miss():
    rs = _FakeRunSummary(cached=None)

    summary = summarize_wake(_wake(), rs)  # auto_schedule defaults to False

    assert rs.scheduled == []  # no Haiku burn on a plain timeline render
    assert summary.startswith("(no summary")
    # Tool fingerprint still surfaces so the row isn't blank.
    assert "Read" in summary


def test_cached_summary_is_returned_regardless_of_auto_schedule():
    rs = _FakeRunSummary(cached="Fixed the gh_poll merged-PR reconciliation bug.")

    s1 = summarize_wake(_wake(), rs, auto_schedule=False)
    s2 = summarize_wake(_wake(), rs, auto_schedule=True)

    assert s1 == s2 == "Fixed the gh_poll merged-PR reconciliation bug."
    assert rs.scheduled == []  # cache hit skips scheduling in both modes


def test_explicit_auto_schedule_true_fires_one_haiku_call_on_cache_miss():
    rs = _FakeRunSummary(cached=None)

    summary = summarize_wake(_wake("wake-42"), rs, auto_schedule=True)

    assert rs.scheduled == ["wake-42"]
    # Template keys off this prefix to kick off its 3s auto-poll.
    assert summary.startswith("summarising")


def test_running_wake_short_circuits_before_either_cache_or_schedule():
    rs = _FakeRunSummary(cached="shouldn't matter")

    summary = summarize_wake(_wake(ended=False), rs, auto_schedule=True)

    assert summary == "running…"
    assert rs.scheduled == []  # no summary while the wake is still moving


def test_cell_prefix_contract_for_template_branching():
    """The _wake_summary_cell.html template branches on these string
    prefixes. If either changes without updating the template, the
    polling / JIT button render will quietly break — pin them here."""
    rs_jit = _FakeRunSummary(cached=None)
    rs_auto = _FakeRunSummary(cached=None)

    jit_label = summarize_wake(_wake(), rs_jit, auto_schedule=False)
    auto_label = summarize_wake(_wake(), rs_auto, auto_schedule=True)

    assert jit_label.startswith("(no summary")
    assert auto_label.startswith("summarising")
