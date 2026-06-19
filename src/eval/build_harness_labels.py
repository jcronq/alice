"""Build the ground-truth labelled set for the speaking-harness eval.

This is the *yardstick*. Unlike :mod:`eval.seed_builder` (which auto-labels
by keyword and explicitly needs human review), the labels here are applied
BY HAND per the rubric in the task brief and baked in below as
:data:`HAND_LABELS`. The script's only mechanical job is to source each
turn's faithful ``ts`` / ``channel`` / ``inbound`` / ``historical_outbound``
from ``speaking-turns.jsonl`` (so the text isn't transcribed by hand) and
write the canonical JSONL.

Rubric (encoded in the hand labels, repeated here for provenance):

- ``action_required`` is True iff the inbound asks Alice to DO something
  beyond acknowledging (fix/build/change/look-up/file/send/log/dispatch/
  answer-a-question). False for pure acks, thanks, confirmations, FYIs.
- ``acceptable_ack_only`` is True iff a bare 👍/❤️ is a complete correct
  response (NOT action_required AND no substantive answer expected).
- Every **Signal** turn lists ``send_message`` in ``expected_tools``: the
  daemon's contract is that the reply (even a bare ack) reaches the user
  via send_message, else it emits ``missed_reply``. CLI turns do NOT — the
  final assistant text IS the reply there — so their ``expected_tools``
  holds only non-reply side-effect tools.

Output: ``~/alice-mind/inner/state/speaking-harness-eval-labels.jsonl``.
Schema (one object per line)::

    {"turn_id","ts","channel","inbound","historical_outbound",
     "action_required":bool,"acceptable_ack_only":bool,
     "expected_tools":[...],"notes":"..."}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eval.label_extractor import classify_channel, load_turns, make_turn_id
from eval.pii import redact

__all__ = ["HAND_LABELS", "build_labels", "main"]

DEFAULT_LOG = "~/alice-mind/inner/state/speaking-turns.jsonl"
DEFAULT_OUT = "~/alice-mind/inner/state/speaking-harness-eval-labels.jsonl"

_SEND = ["send_message"]
_SEND_DISPATCH = ["send_message", "Agent"]

# turn_id -> (action_required, acceptable_ack_only, expected_tools, notes)
HAND_LABELS: dict[str, tuple[bool, bool, list[str], str]] = {
    # ---- emoji-only replies (the bare-ack surface) -----------------------
    "turn_1779368743344": (False, True, _SEND, "Fact correction (checkout path); bare ack delivers the acknowledgment — acceptable. Ideally also fires a note-to-memory."),
    "turn_1779621651501": (True, False, _SEND_DISPATCH, "'just fix things and get it working' implies dispatch/build; a bare 👍 is the bare-ack failure mode."),
    "turn_1779670601969": (False, True, _SEND, "Standing instruction (notify when he replies); ack acceptable now, the action is future-triggered."),
    "turn_1779837193434": (False, True, _SEND, "FYI of an upcoming walk-test; ack acceptable now."),
    "turn_1779902460835": (False, True, _SEND, "'no worries, don't bother' — explicit stand-down; ack acceptable."),
    "turn_1779928692102": (False, True, _SEND, "Mike confirms service is back up; FYI, ack acceptable."),
    "turn_1780263365160": (False, True, _SEND, "Katie 'Thanks!' — pure ack."),
    "turn_1780272666337": (False, True, _SEND, "FYI ('I'll try a different cable'); ack acceptable."),
    "turn_1780352905157": (False, True, _SEND, "'It worked.' — pure ack."),
    "turn_1780353819781": (False, True, _SEND, "'Face looks great' — compliment, pure ack."),
    "turn_1780355047005": (False, True, _SEND, "'Cool' — pure ack."),
    "turn_1780365553636": (True, False, _SEND_DISPATCH, "'work through each of the phases until complete' — explicit multi-phase build; 👍 is the bare-ack failure mode."),
    "turn_1780408465148": (False, True, _SEND, "'happy with current naming' — confirmation; ack acceptable."),
    "turn_1780444134734": (False, True, _SEND, "'are you back?' presence check; an affirmative 👍 is acceptable."),
    "turn_1780538060039": (False, True, _SEND, "FYI: Jason updated the face script; ack acceptable."),
    "turn_1781808472250": (True, False, _SEND_DISPATCH, "Design instruction (make the 3D print say just the agent name) implies an edit/render; 👍 is bare-ack."),
    "turn_1781809242089": (False, True, _SEND, "'Ok.' — pure ack."),
    "turn_1781809552562": (True, False, _SEND_DISPATCH, "'Remove the extra stuff' — explicit model edit; 👍 is bare-ack."),
    "turn_1781809565989": (True, False, _SEND, "'And send me the result' — explicit send request; a bare 👍 sends no file."),
    "turn_1781809874004": (True, False, _SEND_DISPATCH, "Bug report (imported as text, not geometry) implies a fix/re-render; 👍 is bare-ack."),
    # ---- empty-outbound Signal turns (the missing-send surface) ----------
    "turn_1780486650792": (True, False, _SEND, "Question about slide-2 progress; empty reply = missing-send failure."),
    "turn_1780453652686": (True, False, _SEND_DISPATCH, "'just give me samples' — produce + reply; empty reply = missing-send."),
    "turn_1780485978939": (True, False, _SEND_DISPATCH, "'add animations to every slide' — build; empty reply = missing-send."),
    "turn_1780429977990": (True, False, _SEND_DISPATCH, "Bug report ('Auto rotate still not working'); empty reply = missing-send."),
    "turn_1780508216740": (True, False, _SEND, "'how did such a regression happen?' question; empty reply = missing-send."),
    "turn_1780420596904": (True, False, _SEND, "'shut down the model on 3090' instruction; empty reply = missing-send."),
    "turn_1780454196289": (True, False, _SEND_DISPATCH, "'get me a sample of ...' action; empty reply = missing-send."),
    "turn_1780425568149": (True, False, _SEND, "'What do you think now?' question; empty reply = missing-send."),
    "turn_1780429866460": (True, False, _SEND_DISPATCH, "'Make the nodes smaller ... auto-rotate' edit; empty reply = missing-send."),
    "turn_1780449644969": (True, False, _SEND, "'Why does it take so long?' question; empty reply = missing-send."),
    "turn_1780496620411": (True, False, _SEND_DISPATCH, "'Stitch all the videos into a single video' action; empty reply = missing-send."),
    "turn_1780495338224": (True, False, _SEND, "'best way to run LTX2.3?' question; empty reply = missing-send."),
    "turn_1780015005812": (True, False, _SEND_DISPATCH, "'Remove it completely' (cozyhem) action; empty reply = missing-send."),
    "turn_1780484639166": (True, False, _SEND, "Clarification ('I was talking about the circadian lights') expects an answer; empty reply = missing-send."),
    # ---- empty-outbound CLI turns (benign downtime pings) ----------------
    "turn_1781748985561": (False, True, [], "CLI health-check 'ping'; empty during daemon downtime. An ack would suffice; not an action."),
    "turn_1781749185226": (False, True, [], "CLI presence check ('just checking you're online'); empty during downtime. Ack suffices."),
    # ---- clearly-correct controls: Signal (substantive Q -> A) -----------
    "turn_1780625433238": (True, False, _SEND, "Control: server-rack material question answered substantively."),
    "turn_1780625067394": (True, False, _SEND, "Control: filament (petg-esd/gf25/pet-cf17) question answered substantively."),
    "turn_1779321453695": (True, False, _SEND, "Control: 'was it the right drive?' answered correctly (No — slot 5)."),
    "turn_1780335697758": (True, False, _SEND, "Control: 'is this going to work?' answered (yes)."),
    "turn_1780075452049": (True, False, _SEND, "Control: split-loops / middle-blindness analysis."),
    "turn_1780670647516": (True, False, _SEND, "Control: wiring-backwards diagnosis."),
    "turn_1779368239581": (True, False, _SEND, "Control: 'why are we losing tasks?' explained (PR 264 stalled)."),
    "turn_1779670204105": (True, False, _SEND, "Control: 'did you tell him about his restrictions?' answered (No)."),
    # ---- clearly-correct controls: CLI (final text IS the reply) ---------
    "turn_1779302244901": (True, False, [], "Control (CLI): recovery report answered substantively (will redispatch cue-runner worker, flags rename gap); final text is the reply, no send_message expected."),
    "turn_1779892403682": (True, False, [], "Control (CLI): cozylobe-pipeline review answered; final text is the reply."),
}


def build_labels(log_path: str | Path) -> list[dict]:
    """Join HAND_LABELS to the faithful turn text from the log."""
    turns = load_turns(log_path)
    by_id: dict[str, dict] = {}
    for t in turns:
        tid = make_turn_id(t)
        # A turn_id can repeat across batched envelopes; prefer the record
        # whose presence/absence of outbound matches what we labelled (the
        # daemon stores the reply on the last envelope). We keep the record
        # with the longest outbound, falling back to the first seen.
        cur = by_id.get(tid)
        if cur is None:
            by_id[tid] = t
            continue
        if len((t.get("outbound") or "")) > len((cur.get("outbound") or "")):
            by_id[tid] = t

    rows: list[dict] = []
    missing: list[str] = []
    for tid, (ar, ack, tools, notes) in HAND_LABELS.items():
        rec = by_id.get(tid)
        if rec is None:
            missing.append(tid)
            continue
        rows.append(
            {
                "turn_id": tid,
                "ts": rec.get("ts"),
                "channel": classify_channel(rec.get("sender_number")),
                "inbound": redact(rec.get("inbound") or ""),
                "historical_outbound": redact(rec.get("outbound") or ""),
                "action_required": ar,
                "acceptable_ack_only": ack,
                "expected_tools": list(tools),
                "notes": notes,
            }
        )
    if missing:
        raise SystemExit(
            f"{len(missing)} labelled turn_ids not found in log: {missing}"
        )
    rows.sort(key=lambda r: r.get("ts") or 0)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eval.build_harness_labels")
    p.add_argument("--log", default=DEFAULT_LOG)
    p.add_argument("--out", default=DEFAULT_OUT)
    args = p.parse_args(argv)

    rows = build_labels(args.log)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    n_sig = sum(1 for r in rows if r["channel"] == "signal")
    n_act = sum(1 for r in rows if r["action_required"])
    print(
        f"Wrote {len(rows)} labels ({n_sig} signal, {n_act} action-required) "
        f"to {out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
