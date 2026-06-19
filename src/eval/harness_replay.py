"""Real-``TurnRunner`` correctness-eval harness.

Where the legacy benchmark (:mod:`eval.replay`) hits the model HTTP
endpoint directly — grading the *model*, not Alice's harness — and
infers tool calls by regex over the reply prose, this module drives a
**real** :class:`alice_speaking.turn_runner.TurnRunner` through one
turn and reads back the *structured* tool calls the kernel actually
made.

The seam is the ``tool_use`` block fan-out: the anthropic kernel calls
``on_tool_use(name, input, id)`` on every handler for every tool call,
including the MCP ``send_message``. ``TurnRunner`` installs a
:class:`alice_speaking.turn_runner.ToolCaptureHandler` that records
those into ``TurnRunner.last_tool_calls``; this harness reads it off
the runner after the turn.

Two modes:

- **fake mode** (deterministic, unit-testable): pass ``fake_messages``
  — a list of ``claude_agent_sdk`` message objects — and the harness
  patches ``kernels.anthropic.kernel.query`` to yield them, exactly
  like ``tests/test_kernel.py`` / ``tests/test_daemon.py`` do. No model
  call, no network, no auth. The full ``TurnRunner`` → kernel →
  ToolCaptureHandler path still runs, so the capture seam is exercised
  end-to-end.
- **live mode**: leave ``fake_messages=None`` and the real kernel runs
  against the subscription backend (OAuth token at
  ``~/.claude/.credentials.json``, assumed present at eval time). Wiring
  the real MCP ``send_message`` server is the caller's responsibility —
  pass ``mcp_servers`` / ``custom_tool_names`` from the live daemon
  config when you want the model's ``send_message`` tool-choice actually
  exercised. The capture seam is backend-agnostic either way.

Result record (one per case)::

    {
      "turn_id": str,
      "inbound": str,
      "outbound_text": str,
      "tool_calls": [{"name": str, "id": str}, ...],
      "sent": bool,        # a send_message tool call was made
      "error": str | None,
    }
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import pathlib
import sys
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator, Optional, Sequence

from core.events import CapturingEmitter

from eval.assertions import (
    AssertionFile,
    evaluate_instance,
    tool_calls_contain_send,
)

log = logging.getLogger(__name__)

__all__ = [
    "HarnessResult",
    "build_turn_runner",
    "fake_sdk_query",
    "make_fake_messages",
    "run_case",
    "run_case_sync",
    "main",
    "score_harness_results",
]


# ---------------------------------------------------------------------------
# Result record


@dataclass(slots=True)
class HarnessResult:
    turn_id: str
    inbound: str
    outbound_text: str
    tool_calls: list[dict] = field(default_factory=list)
    sent: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Fake SDK plumbing (deterministic mode)


@contextlib.contextmanager
def fake_sdk_query(messages: Sequence[Any]) -> Iterator[None]:
    """Patch ``kernels.anthropic.kernel.query`` to yield ``messages``.

    Mirrors ``tests/test_daemon.py``'s ``_patch_query``: we build real
    ``claude_agent_sdk`` message objects (so the kernel's ``isinstance``
    checks pass) and only swap the async ``query`` generator. Restored
    on exit.
    """
    import kernels.anthropic.kernel as k

    original = k.query

    async def _fake_query(*, prompt: str, options: Any):  # noqa: ANN001
        for msg in messages:
            yield msg

    k.query = _fake_query  # type: ignore[assignment]
    try:
        yield
    finally:
        k.query = original  # type: ignore[assignment]


def make_fake_messages(
    *,
    text: str,
    tool_calls: Sequence[dict] | None = None,
    session_id: str = "harness-sess",
) -> list[Any]:
    """Build a list of real ``claude_agent_sdk`` messages representing a
    turn that emits ``tool_calls`` (each ``{"name", "input"?, "id"?}``)
    and a final ``text`` reply. Used by fake mode + unit tests."""
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
    )

    blocks: list[Any] = []
    for i, tc in enumerate(tool_calls or []):
        blocks.append(
            ToolUseBlock(
                name=tc["name"],
                input=tc.get("input", {}),
                id=tc.get("id", f"tool-{i}"),
            )
        )
    if text:
        blocks.append(TextBlock(text=text))
    messages: list[Any] = [
        AssistantMessage(content=blocks, model="harness-fake"),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=session_id,
            usage={"input_tokens": 1},
            result=text,
        ),
    ]
    return messages


# ---------------------------------------------------------------------------
# TurnRunner construction


def build_turn_runner(
    tmp_dir: pathlib.Path,
    *,
    events: Any,
    mcp_servers: Optional[dict] = None,
    custom_tool_names: Optional[list[str]] = None,
    backend: Any = None,
    model: Optional[str] = None,
    turn_did_send_getter: Optional[Any] = None,
):
    """Construct a headless :class:`TurnRunner` with all state under
    ``tmp_dir``.

    Mirrors the deterministic wiring of ``tests/test_daemon.py`` (a
    minimal :class:`Config`, tmp paths) but builds the runner directly so
    the harness doesn't need the whole daemon. No transports are wired
    (``cli_transport``/``transport_for`` left ``None``), so the only
    handlers are the tool-capture handler, the session handler, and the
    compaction armer.
    """
    from alice_speaking.infra.config import SPEAKING_DEFAULTS, Config
    from alice_speaking.domain.turn_log import TurnLog
    from alice_speaking.pipeline import compaction as compaction_module
    from alice_speaking.turn_runner import TurnRunner

    tmp_dir = pathlib.Path(tmp_dir)
    mind_dir = tmp_dir / "mind"
    state_dir = tmp_dir / "state"
    mind_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(
        signal_api="http://127.0.0.1:8080",
        signal_account="+15550000000",
        oauth_token="dummy",
        work_dir=mind_dir,
        mind_dir=mind_dir,
        state_dir=state_dir,
        signal_log_path=state_dir / "signal.log",
        offset_path=state_dir / "offset",
        seen_path=state_dir / "seen",
        turn_log_path=mind_dir / "inner" / "state" / "speaking-turns.jsonl",
        event_log_path=state_dir / "speaking.log",
        principals_path=mind_dir / "config" / "principals.yaml",
        allowed_senders_fallback={},
        speaking=dict(SPEAKING_DEFAULTS),
    )

    turns = TurnLog(cfg.turn_log_path)
    compaction = compaction_module.CompactionTrigger()

    if turn_did_send_getter is None:
        # No real MCP outbox in the harness, so ``send_message`` never
        # flips a daemon flag. We derive ``sent`` from the captured tool
        # calls instead (see ``run_case``); this getter just keeps the
        # missed_reply path quiet.
        def turn_did_send_getter() -> bool:  # type: ignore[misc]
            return True

    runner = TurnRunner(
        cfg=cfg,
        events=events,
        turns=turns,
        mcp_servers=mcp_servers or {},
        custom_tool_names=custom_tool_names or [],
        session_path=state_dir / "session.json",
        summary_path=state_dir / "context-summary.md",
        compaction=compaction,
        turn_did_send_getter=turn_did_send_getter,
        current_reply_channel_getter=lambda: None,
        backend=backend,
        model=model,
        mind_dir=mind_dir,
    )
    return runner


# ---------------------------------------------------------------------------
# Driving one case


async def run_case(
    case: dict,
    *,
    tmp_dir: pathlib.Path,
    fake_messages: Optional[Sequence[Any]] = None,
    mcp_servers: Optional[dict] = None,
    custom_tool_names: Optional[list[str]] = None,
    backend: Any = None,
    model: Optional[str] = None,
) -> HarnessResult:
    """Drive a real ``TurnRunner.run_turn`` for one labelled case and
    return the structured result.

    ``case`` needs at least ``inbound`` (the prompt text) and optionally
    ``turn_id`` / ``sender_name``. ``fake_messages`` selects deterministic
    mode.
    """
    turn_id = case.get("turn_id") or uuid.uuid4().hex[:12]
    inbound = case.get("inbound") or ""

    events = CapturingEmitter()
    runner = build_turn_runner(
        tmp_dir,
        events=events,
        mcp_servers=mcp_servers,
        custom_tool_names=custom_tool_names,
        backend=backend,
        model=model,
    )

    error: Optional[str] = None
    outbound_text = ""
    try:
        if fake_messages is not None:
            with fake_sdk_query(fake_messages):
                outbound_text = await runner.run_turn(
                    inbound,
                    turn_id=turn_id,
                    outbound_recipient=case.get("sender_name"),
                )
        else:
            outbound_text = await runner.run_turn(
                inbound,
                turn_id=turn_id,
                outbound_recipient=case.get("sender_name"),
            )
    except Exception as exc:  # noqa: BLE001 - record, don't crash the run
        error = f"{type(exc).__name__}: {exc}"
        log.warning("harness turn failed for %s: %s", turn_id, error)

    tool_calls = list(runner.last_tool_calls or [])
    return HarnessResult(
        turn_id=turn_id,
        inbound=inbound,
        outbound_text=outbound_text or "",
        tool_calls=tool_calls,
        sent=tool_calls_contain_send(tool_calls),
        error=error,
    )


def run_case_sync(case: dict, **kwargs: Any) -> HarnessResult:
    """Blocking wrapper around :func:`run_case`."""
    return asyncio.run(run_case(case, **kwargs))


# ---------------------------------------------------------------------------
# Label → assertions → score
#
# We build the per-case assertion file straight from the label rather
# than from the historical reply (that's what makes this a *correctness*
# eval rather than a regression eval): an action-required label demands a
# send_message; expected_tools become a structured tool_call_match; and
# every case carries the unbacked-completion-claim guard.


def assertions_for_case(case: dict) -> AssertionFile:
    """Derive an :class:`AssertionFile` from a labelled case."""
    turn_id = case.get("turn_id") or "turn_unknown"
    category = case.get("sampled_category") or case.get("category") or "unknown"
    expected_tools = list(case.get("expected_tools") or [])
    action_required = bool(case.get("expected_action_required"))

    pass_to_pass: list[dict] = [
        # Hallucinated "done/sent" with no tool call is always a fail.
        {"type": "no_unbacked_completion_claim"},
    ]
    fail_to_pass: list[dict] = []
    if action_required:
        fail_to_pass.append({"type": "action_requires_send"})
    if expected_tools:
        fail_to_pass.append(
            {
                "type": "tool_call_match",
                "expected_tools": expected_tools,
                "match": "set",
            }
        )
    return AssertionFile(
        turn_id=turn_id,
        category=category,
        channel=case.get("channel", "signal"),
        pass_to_pass=pass_to_pass,
        fail_to_pass=fail_to_pass,
    )


# ---------------------------------------------------------------------------
# Spec label set: schema, offline reconstruction, label-driven assertions
#
# The canonical ground-truth labels live at
# ``~/alice-mind/inner/state/speaking-harness-eval-labels.jsonl`` (built by
# ``eval.build_harness_labels``). Each row carries ``channel``,
# ``action_required``, ``acceptable_ack_only`` and ``expected_tools``; this
# section turns one row into the three spec-named failure-mode assertions
# and, for offline mode, reconstructs a faithful HarnessResult from the
# historical record without any model call.

DEFAULT_LABELS = "~/alice-mind/inner/state/speaking-harness-eval-labels.jsonl"

# Tool names that mean "Alice delivered a reply to a human".
_SEND_NAMES = ("send_message", "mcp__alice__send_message")


def load_labels(path: str | pathlib.Path = DEFAULT_LABELS) -> list[dict]:
    """Load the canonical labelled set (skips any ``_seed_meta`` line)."""
    return _load_jsonl(path, skip_meta=True)


def label_category(label: dict) -> str:
    """A reporting bucket for the ``by_category`` table."""
    if label.get("acceptable_ack_only"):
        return "ack"
    if not label.get("action_required"):
        return "fyi"
    return "action-cli" if label.get("channel") == "cli" else "action-signal"


def offline_result(label: dict) -> HarnessResult:
    """Reconstruct a HarnessResult from the historical record — no model.

    Faithful reconstruction of what the daemon actually did:

    - On a **Signal** turn a non-empty historical outbound means
      ``send_message`` was called with that text (that's how it reached the
      user); an empty outbound means ``missed_reply`` — no send. So we
      synthesise a ``send_message`` call (with the real message body, so the
      bare-ack check can see whether it was emoji-only) iff the outbound is
      non-empty.
    - On a **CLI** turn the final assistant text IS the reply (no send), so
      no tool call is synthesised.

    Non-send tool calls cannot be recovered from this log, so offline mode
    can only score the bare-ack and missing-send modes precisely; a
    completion claim that needed a *non-send* tool (e.g. "filed an issue")
    will read as unbacked offline. That's a documented limitation — live
    mode is the ground truth for claim-backing of non-send tools.
    """
    outbound = label.get("historical_outbound") or ""
    channel = label.get("channel", "signal")
    tool_calls: list[dict] = []
    if channel == "signal" and outbound.strip():
        tool_calls = [
            {
                "name": "mcp__alice__send_message",
                "id": "offline-send",
                "input": {"message": outbound},
            }
        ]
    return HarnessResult(
        turn_id=label.get("turn_id") or "turn_unknown",
        inbound=label.get("inbound") or "",
        outbound_text=outbound,
        tool_calls=tool_calls,
        sent=bool(tool_calls),
        error=None,
    )


def assertions_for_label(label: dict) -> AssertionFile:
    """Build the three spec-named failure-mode assertions for one label.

    - ``claim_backed_by_tool`` (pass_to_pass): always — a completion claim
      must be backed by the corresponding tool (false-completion mode).
    - ``action_taken_when_required`` (fail_to_pass): when action_required —
      a substantive tool (or, on CLI, substantive text) must have fired
      (bare-ack mode).
    - ``send_message_when_expected`` (fail_to_pass): on Signal turns whose
      ``expected_tools`` includes send_message — send_message must have been
      called (missing-send mode).
    """
    channel = label.get("channel", "signal")
    expected_tools = list(label.get("expected_tools") or [])
    action_required = bool(label.get("action_required"))

    pass_to_pass: list[dict] = [
        {
            "type": "claim_backed_by_tool",
            "acceptable_ack_only": bool(label.get("acceptable_ack_only")),
        }
    ]
    fail_to_pass: list[dict] = []
    if action_required:
        fail_to_pass.append(
            {"type": "action_taken_when_required", "channel": channel}
        )
    if channel == "signal" and any(
        t in _SEND_NAMES for t in expected_tools
    ):
        fail_to_pass.append(
            {"type": "send_message_when_expected", "channel": channel}
        )
    return AssertionFile(
        turn_id=label.get("turn_id") or "turn_unknown",
        category=label_category(label),
        channel=channel,
        pass_to_pass=pass_to_pass,
        fail_to_pass=fail_to_pass,
    )


def score_label_results(
    labels: Sequence[dict],
    results: Sequence[HarnessResult],
    *,
    candidate_id: str = "alice",
) -> list[dict]:
    """Score harness results against the spec labels' failure-mode
    assertions. Returns rows consumable by :func:`eval.score.score_results`
    (each row carries ``resolved``, ``category`` and the per-assertion
    ``assertions`` list the per-failure-mode breakdown reads)."""
    by_turn = {r.turn_id: r for r in results}
    rows: list[dict] = []
    for label in labels:
        turn_id = label.get("turn_id")
        result = by_turn.get(turn_id)
        af = assertions_for_label(label)
        if result is None:
            rows.append(
                {
                    "turn_id": turn_id,
                    "category": af.category,
                    "candidate_id": candidate_id,
                    "resolved": False,
                    "error": "no harness result",
                    "assertions": [],
                }
            )
            continue
        instance = evaluate_instance(
            af,
            result.outbound_text,
            candidate_id=candidate_id,
            tool_calls=result.tool_calls,
        )
        row = instance.to_dict()
        row["error"] = result.error
        row["sent"] = result.sent
        row["tool_calls"] = result.tool_calls
        rows.append(row)
    return rows


def score_harness_results(
    cases: Sequence[dict],
    results: Sequence[HarnessResult],
    *,
    candidate_id: str = "alice",
) -> list[dict]:
    """Evaluate each harness result against its case's assertions and
    return score rows consumable by :func:`eval.score.score_results`.

    Crucially, ``tool_calls`` is the *structured* list from the harness —
    so the tool-aware assertions grade against what Alice actually
    called, not regex-over-prose.
    """
    by_turn = {r.turn_id: r for r in results}
    rows: list[dict] = []
    for case in cases:
        turn_id = case.get("turn_id")
        result = by_turn.get(turn_id)
        af = assertions_for_case(case)
        if result is None:
            rows.append(
                {
                    "turn_id": turn_id,
                    "category": af.category,
                    "candidate_id": candidate_id,
                    "resolved": False,
                    "error": "no harness result",
                }
            )
            continue
        instance = evaluate_instance(
            af,
            result.outbound_text,
            candidate_id=candidate_id,
            tool_calls=result.tool_calls,
        )
        row = instance.to_dict()
        row["error"] = result.error
        row["sent"] = result.sent
        row["tool_calls"] = result.tool_calls
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# I/O helpers


def _load_jsonl(path: str | pathlib.Path, *, skip_meta: bool = True) -> list[dict]:
    rows: list[dict] = []
    with pathlib.Path(path).expanduser().open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if skip_meta and obj.get("_seed_meta"):
                continue
            rows.append(obj)
    return rows


def _harness_results_from_jsonl(path: str | pathlib.Path) -> list[HarnessResult]:
    out: list[HarnessResult] = []
    for obj in _load_jsonl(path, skip_meta=True):
        out.append(
            HarnessResult(
                turn_id=obj["turn_id"],
                inbound=obj.get("inbound", ""),
                outbound_text=obj.get("outbound_text", ""),
                tool_calls=list(obj.get("tool_calls") or []),
                sent=bool(obj.get("sent")),
                error=obj.get("error"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# CLI


def _cmd_run(args: argparse.Namespace) -> int:
    """Drive the labelled set through the real TurnRunner and write
    harness result rows. Requires a live backend unless
    ``--results-from`` is supplied (precomputed results, for offline
    scoring / tests)."""
    cases = _load_jsonl(args.cases)
    import tempfile

    out_path = pathlib.Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    results: list[HarnessResult] = []
    with tempfile.TemporaryDirectory(prefix="alice-harness-") as td:
        td_path = pathlib.Path(td)
        for i, case in enumerate(cases):
            results.append(
                run_case_sync(case, tmp_dir=td_path / f"case-{i}")
            )

    with out_path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
    print(
        f"Wrote {len(results)} harness result rows to {out_path}",
        file=sys.stderr,
    )
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    """Score precomputed harness results against the labelled set and
    print the stratified pass-rate (reusing :mod:`eval.score`)."""
    from eval import score as _score

    cases = _load_jsonl(args.cases)
    results = _harness_results_from_jsonl(args.results)
    rows = score_harness_results(cases, results, candidate_id=args.candidate)
    report = _score.score_results(rows, candidate_id=args.candidate)
    print(_score.format_report(report))
    if args.out:
        pathlib.Path(args.out).expanduser().write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8"
        )
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    """End-to-end: load labels -> produce results (offline reconstruction
    or live TurnRunner replay) -> score the three failure-mode assertions
    -> print the table (with per-failure-mode breakdown).

    ``--offline`` (default) reconstructs results from the historical record
    with NO model call (free, deterministic, CI-safe). ``--live`` drives the
    real TurnRunner per turn against the subscription backend.
    """
    from eval import score as _score

    labels = load_labels(args.labels)
    if not labels:
        print(f"no labels loaded from {args.labels}", file=sys.stderr)
        return 1

    results: list[HarnessResult]
    if args.live:
        import tempfile

        results = []
        with tempfile.TemporaryDirectory(prefix="alice-harness-") as td:
            td_path = pathlib.Path(td)
            for i, label in enumerate(labels):
                results.append(
                    run_case_sync(label, tmp_dir=td_path / f"case-{i}")
                )
    else:
        results = [offline_result(label) for label in labels]

    rows = score_label_results(labels, results, candidate_id=args.candidate)
    report = _score.score_results(rows, candidate_id=args.candidate)
    mode = "LIVE" if args.live else "OFFLINE (historical reconstruction)"
    print(f"=== speaking-harness correctness eval — mode: {mode} ===")
    print(_score.format_report(report))

    if args.results_out:
        out = pathlib.Path(args.results_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
    if args.out:
        pathlib.Path(args.out).expanduser().write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8"
        )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval harness",
        description=(
            "Real-TurnRunner correctness harness: drives the speaking "
            "daemon's actual turn loop, captures structured tool calls "
            "(with inputs), and "
            "scores the three failure-mode assertions "
            "(action_taken_when_required, claim_backed_by_tool, "
            "send_message_when_expected)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    eval_p = sub.add_parser(
        "eval",
        help="One-shot: labels -> (offline|live) results -> score table",
    )
    eval_p.add_argument("--labels", default=DEFAULT_LABELS)
    eval_p.add_argument(
        "--offline",
        dest="live",
        action="store_false",
        default=False,
        help="Reconstruct from historical record, no model call (default)",
    )
    eval_p.add_argument(
        "--live",
        dest="live",
        action="store_true",
        help="Drive the real TurnRunner against the subscription backend",
    )
    eval_p.add_argument("--candidate", default="alice")
    eval_p.add_argument(
        "--results-out",
        default=None,
        help="Optional path to write per-turn HarnessResult rows",
    )
    eval_p.add_argument(
        "--out", default=None, help="Optional path to write the JSON report"
    )
    eval_p.set_defaults(func=_cmd_eval)

    run_p = sub.add_parser(
        "run", help="Drive the labelled set through a real TurnRunner"
    )
    run_p.add_argument(
        "--cases",
        default="configs/speaking_correctness_seed.jsonl",
        help="Labelled ground-truth JSONL",
    )
    run_p.add_argument("--out", default="harness_results.jsonl")
    run_p.set_defaults(func=_cmd_run)

    score_p = sub.add_parser(
        "score", help="Score precomputed harness results against the labels"
    )
    score_p.add_argument(
        "--cases", default="configs/speaking_correctness_seed.jsonl"
    )
    score_p.add_argument(
        "--results",
        default="harness_results.jsonl",
        help="Harness result rows from `harness run`",
    )
    score_p.add_argument("--candidate", default="alice")
    score_p.add_argument("--out", default=None)
    score_p.set_defaults(func=_cmd_score)

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
