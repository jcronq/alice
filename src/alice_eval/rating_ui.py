"""Single-file blind-rating UI generator.

.. deprecated::
   Superseded by the SWE-Bench-style speaking-benchmark
   (:mod:`alice_eval.bench`, :mod:`alice_eval.assertions`,
   :mod:`alice_eval.score`) per
   ``cortex-memory/research/2026-05-18-speaking-benchmark-design.md``
   (issue #237). The blind-A/B rating UI is retained as the optional
   *Verified-subset* rater for the small set of prose-only turns
   where BLEU is informational only; it is no longer the primary
   acceptance path.

Reads ``eval_sample.jsonl`` and the per-candidate
``eval_outputs_<id>.jsonl`` files, embeds the data inline in an HTML
file, and writes ``eval_rating.html``. The page is fully offline —
no network, no server. Jason opens it in a browser, rates each
turn, exports a JSON ratings file.

Design notes (per
``cortex-memory/research/2026-05-15-eval-rating-ui-design.md`` and
``...-day2-rating-rubric.md``):

- Candidates shown as ``Response A`` / ``Response B``. The A/B-to-
  candidate mapping is randomised *per turn* with a seed derived
  from the turn_id so the same turn always shuffles to the same
  order across reloads.
- Five binary dimensions per candidate per turn (V/C/T/B/U). T is
  ``null`` for non-tool categories per the rubric's category
  emphasis (conversational, design, edge).
- Head-to-head radio: pick one candidate per turn.
- First five turns flagged as calibration; they're rateable but
  noted as "not counted" toward the final summary.
- Progress counter ("17/40").
- Reveal button — locked until all 40 turns have at least one
  rating action recorded; on click shows the candidate mapping.
- localStorage persistence keyed by ``alice_eval_ratings`` —
  refresh-safe.
- Export button downloads ``eval_ratings_<YYYY-MM-DD>.json`` whose
  schema matches the rubric note.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_OUTPUTS_DIR",
    "DEFAULT_SAMPLE_PATH",
    "build_html",
    "main_ui",
]


log = logging.getLogger(__name__)

DEFAULT_SAMPLE_PATH = Path("eval_sample.jsonl")
DEFAULT_OUTPUTS_DIR = Path("eval_outputs")
DEFAULT_HTML_PATH = Path("eval_rating.html")
DEFAULT_CANDIDATES_PATH = Path("configs/eval_candidates.json")
CALIBRATION_TURNS = 5

# Categories where Tool fidelity is meaningful per the rubric.
TOOL_RELEVANT_CATEGORIES = {"tool-heavy", "tactical", "image"}


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _load_outputs(outputs_dir: Path) -> dict[str, dict[str, dict]]:
    """Return ``{turn_id: {candidate_id: row}}``.

    Discovers ``eval_outputs_*.jsonl`` files in ``outputs_dir``;
    candidate_id is parsed from the file name.
    """
    by_turn: dict[str, dict[str, dict]] = {}
    for path in sorted(outputs_dir.glob("eval_outputs_*.jsonl")):
        stem = path.stem
        # eval_outputs_opus → opus
        candidate_id = stem[len("eval_outputs_"):]
        for row in _read_jsonl(path):
            turn_id = row.get("turn_id")
            if not turn_id:
                continue
            by_turn.setdefault(turn_id, {})[candidate_id] = row
    return by_turn


def _load_candidate_labels(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    return {row["id"]: row.get("label", row["id"]) for row in payload.get("candidates", [])}


def _truncate_history(turn: dict, max_turns: int = 3) -> list[dict]:
    """Pass through whatever ``history_context`` (if any) the sample
    carries; otherwise the UI just shows the inbound text.

    The sample file as produced by :mod:`alice_eval.sampling` doesn't
    currently embed history — the replay reconstructs it from the
    log. For UI purposes the bare inbound is enough; we still leave
    a hook so future iterations can pre-bake conversation context
    into ``eval_sample.jsonl``.
    """
    history = turn.get("history_context") or []
    return history[-max_turns:]


def _payload_for_html(
    sample: list[dict],
    outputs: dict[str, dict[str, dict]],
    candidate_labels: dict[str, str],
) -> dict[str, Any]:
    """Build the JSON blob embedded into the HTML."""
    turns_payload: list[dict[str, Any]] = []
    for idx, turn in enumerate(sample):
        turn_id = turn.get("turn_id")
        category = turn.get("sampled_category", "unknown")
        per_candidate = outputs.get(turn_id, {})
        turns_payload.append(
            {
                "index": idx,
                "turn_id": turn_id,
                "category": category,
                "is_calibration": idx < CALIBRATION_TURNS,
                "inbound": turn.get("inbound", ""),
                "history": _truncate_history(turn),
                "candidate_outputs": [
                    {
                        "candidate_id": cid,
                        "output": (row.get("output") or "").strip(),
                        "status": row.get("status", "ok"),
                        "error": row.get("error"),
                        "latency_ms": row.get("latency_ms"),
                    }
                    for cid, row in sorted(per_candidate.items())
                ],
            }
        )
    return {
        "generated": date.today().isoformat(),
        "candidate_labels": candidate_labels,
        "tool_relevant_categories": sorted(TOOL_RELEVANT_CATEGORIES),
        "calibration_count": CALIBRATION_TURNS,
        "turns": turns_payload,
    }


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Alice speaking-quality eval — blind rating</title>
<style>
  :root {
    --bg: #fafaf9;
    --fg: #1f2937;
    --muted: #6b7280;
    --border: #d1d5db;
    --card-bg: #ffffff;
    --accent: #1e40af;
    --warn: #b45309;
    --calibration: #fef3c7;
  }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--fg); margin: 0; padding: 0; }
  header { position: sticky; top: 0; z-index: 10; background: var(--card-bg);
           border-bottom: 1px solid var(--border); padding: 0.75rem 1.5rem;
           display: flex; justify-content: space-between; align-items: center; }
  header h1 { font-size: 1rem; font-weight: 600; margin: 0; }
  header .controls { display: flex; gap: 0.5rem; align-items: center; }
  button { background: var(--accent); color: #fff; border: none; padding: 0.4rem 0.8rem;
           border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
  button:disabled { background: #9ca3af; cursor: not-allowed; }
  main { padding: 1.5rem; max-width: 1200px; margin: 0 auto; }
  .card { background: var(--card-bg); border: 1px solid var(--border);
          border-radius: 6px; padding: 1rem 1.25rem; margin-bottom: 1.5rem; }
  .card.calibration { background: var(--calibration); }
  .card h2 { font-size: 0.85rem; text-transform: uppercase; color: var(--muted);
             margin: 0 0 0.25rem 0; letter-spacing: 0.05em; }
  .card .calibration-note { font-size: 0.8rem; color: var(--warn);
                            margin: 0 0 0.5rem 0; }
  .card .category-tag { display: inline-block; font-size: 0.75rem; color: var(--muted);
                        background: #f3f4f6; padding: 0.1rem 0.4rem; border-radius: 3px;
                        margin-bottom: 0.5rem; }
  .user-input { background: #f3f4f6; padding: 0.6rem 0.8rem; border-radius: 4px;
                font-family: ui-monospace, Menlo, monospace; font-size: 0.85rem;
                white-space: pre-wrap; }
  .responses { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;
               margin-top: 1rem; }
  .response { border: 1px solid var(--border); border-radius: 4px;
              padding: 0.75rem 0.9rem; background: #fafaf9; }
  .response h3 { margin: 0 0 0.5rem 0; font-size: 0.85rem; }
  .response .body { font-family: ui-monospace, Menlo, monospace; font-size: 0.85rem;
                    white-space: pre-wrap; max-height: 320px; overflow-y: auto;
                    background: var(--card-bg); padding: 0.6rem;
                    border: 1px solid var(--border); border-radius: 3px; }
  .response .error { color: var(--warn); font-weight: 600; }
  .dim-row { margin-top: 0.6rem; display: flex; flex-wrap: wrap; gap: 0.6rem;
             font-size: 0.85rem; }
  .dim-row label { display: flex; align-items: center; gap: 0.25rem; }
  .h2h { margin-top: 0.75rem; padding-top: 0.5rem; border-top: 1px dashed var(--border);
         font-size: 0.85rem; }
  .h2h label { margin-right: 1rem; }
  .latency { color: var(--muted); font-size: 0.75rem; margin-top: 0.25rem; }
  .reveal { margin-top: 2rem; padding: 1rem; border: 1px dashed var(--accent);
            border-radius: 6px; font-family: ui-monospace, Menlo, monospace;
            font-size: 0.85rem; }
  .reveal.hidden { display: none; }
</style>
</head>
<body>
<header>
  <h1>Alice speaking-quality eval — blind rating</h1>
  <div class="controls">
    <span id="progress">0 / 0</span>
    <button id="export">Export ratings JSON</button>
    <button id="reveal">Reveal A/B mapping</button>
  </div>
</header>
<main id="root"></main>
<div class="reveal hidden" id="revealBlock"></div>
<script id="eval-data" type="application/json">__PAYLOAD__</script>
<script>
(function () {
  const STORAGE_KEY = 'alice_eval_ratings';
  const data = JSON.parse(document.getElementById('eval-data').textContent);
  const candidateLabels = data.candidate_labels || {};
  const toolRelevant = new Set(data.tool_relevant_categories || []);
  const calibrationCount = data.calibration_count || 0;
  const turns = data.turns || [];

  // Deterministic shuffle: seed from turn_id so A/B mapping is
  // stable across reloads but differs between turns.
  function hash(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) {
      h = (h * 31 + s.charCodeAt(i)) | 0;
    }
    return Math.abs(h);
  }

  function abMapping(turn) {
    const candidates = turn.candidate_outputs.map((c) => c.candidate_id);
    if (candidates.length < 2) return candidates;
    // Two-candidate swap based on the LSB of the hash.
    const seed = hash(turn.turn_id || String(turn.index));
    if (seed % 2 === 1) {
      return [candidates[1], candidates[0]];
    }
    return candidates;
  }

  const state = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
  if (!state.session_date) {
    state.session_date = data.generated;
  }
  if (!state.results) state.results = {};
  if (!state.mapping) state.mapping = {};

  function persist() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    updateProgress();
  }

  function ensureTurn(turnId, mapping) {
    if (!state.results[turnId]) {
      state.results[turnId] = { scores: {}, h2h: null };
    }
    state.mapping[turnId] = mapping;
    for (const cid of mapping) {
      if (!state.results[turnId].scores[cid]) {
        state.results[turnId].scores[cid] = { V: null, C: null, T: null, B: null, U: null };
      }
    }
  }

  function updateProgress() {
    const total = turns.length;
    let done = 0;
    for (const turn of turns) {
      const tid = turn.turn_id;
      const entry = state.results[tid];
      if (!entry) continue;
      const allRated = Object.values(entry.scores).every((s) =>
        ['V', 'C', 'B', 'U'].every((d) => s[d] !== null)
      );
      const h2hPicked = entry.h2h !== null;
      if (allRated && h2hPicked) done += 1;
    }
    document.getElementById('progress').textContent = `${done} / ${total}`;
    const reveal = document.getElementById('reveal');
    reveal.disabled = done < total;
  }

  function renderCard(turn) {
    const card = document.createElement('section');
    card.className = 'card' + (turn.is_calibration ? ' calibration' : '');
    const mapping = abMapping(turn);
    ensureTurn(turn.turn_id, mapping);

    const titleParts = [];
    titleParts.push(`Turn ${turn.index + 1} / ${turns.length}`);
    if (turn.is_calibration) titleParts.push('— calibration (not counted)');
    const h2 = document.createElement('h2');
    h2.textContent = titleParts.join(' ');
    card.appendChild(h2);

    if (turn.is_calibration) {
      const note = document.createElement('p');
      note.className = 'calibration-note';
      note.textContent = 'Calibration round — rate to warm up; not counted toward the final score.';
      card.appendChild(note);
    }

    const cat = document.createElement('span');
    cat.className = 'category-tag';
    cat.textContent = `category: ${turn.category}`;
    card.appendChild(cat);

    if (turn.history && turn.history.length) {
      const histBlock = document.createElement('div');
      histBlock.className = 'user-input';
      histBlock.textContent = turn.history
        .map((h) => `${h.role}: ${h.content}`)
        .join('\\n---\\n');
      card.appendChild(histBlock);
    }

    const input = document.createElement('div');
    input.className = 'user-input';
    input.textContent = turn.inbound || '(empty inbound)';
    card.appendChild(input);

    const responses = document.createElement('div');
    responses.className = 'responses';
    const abLabels = ['A', 'B'];

    mapping.forEach((candidateId, idx) => {
      const slot = abLabels[idx] || `Response ${idx + 1}`;
      const cand = turn.candidate_outputs.find((c) => c.candidate_id === candidateId);
      const respDiv = document.createElement('div');
      respDiv.className = 'response';
      const h3 = document.createElement('h3');
      h3.textContent = `Response ${slot}`;
      respDiv.appendChild(h3);

      const body = document.createElement('div');
      body.className = 'body';
      if (!cand) {
        body.innerHTML = '<span class="error">(no output for this candidate)</span>';
      } else if (cand.status === 'error') {
        body.innerHTML = '<span class="error">ERROR: ' + (cand.error || 'unknown') + '</span>';
      } else {
        body.textContent = cand.output || '(empty output)';
      }
      respDiv.appendChild(body);

      if (cand && cand.latency_ms != null) {
        const lat = document.createElement('div');
        lat.className = 'latency';
        lat.textContent = `latency: ${cand.latency_ms} ms`;
        respDiv.appendChild(lat);
      }

      const dimRow = document.createElement('div');
      dimRow.className = 'dim-row';
      const dims = ['V', 'C', 'T', 'B', 'U'];
      const dimNames = {
        V: 'Voice',
        C: 'Correctness',
        T: 'Tool fidelity',
        B: 'Brevity',
        U: 'Usefulness',
      };
      const tApplies = toolRelevant.has(turn.category);
      dims.forEach((d) => {
        if (d === 'T' && !tApplies) {
          state.results[turn.turn_id].scores[candidateId][d] = null;
          return;
        }
        const label = document.createElement('label');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        const current = state.results[turn.turn_id].scores[candidateId][d];
        cb.checked = !!current;
        cb.addEventListener('change', () => {
          state.results[turn.turn_id].scores[candidateId][d] = cb.checked;
          persist();
        });
        // Mark non-null so progress sees this as "rated" once the box is touched.
        if (current === null) {
          state.results[turn.turn_id].scores[candidateId][d] = false;
        }
        label.appendChild(cb);
        label.appendChild(document.createTextNode(' ' + d + ' (' + dimNames[d] + ')'));
        dimRow.appendChild(label);
      });
      respDiv.appendChild(dimRow);
      responses.appendChild(respDiv);
    });

    card.appendChild(responses);

    const h2hDiv = document.createElement('div');
    h2hDiv.className = 'h2h';
    h2hDiv.appendChild(document.createTextNode(
      'If forced to ship one reply, which? '
    ));
    mapping.forEach((candidateId, idx) => {
      const slot = abLabels[idx];
      const lbl = document.createElement('label');
      const rad = document.createElement('input');
      rad.type = 'radio';
      rad.name = 'h2h_' + turn.turn_id;
      rad.value = candidateId;
      rad.checked = state.results[turn.turn_id].h2h === candidateId;
      rad.addEventListener('change', () => {
        state.results[turn.turn_id].h2h = candidateId;
        persist();
      });
      lbl.appendChild(rad);
      lbl.appendChild(document.createTextNode(' Response ' + slot));
      h2hDiv.appendChild(lbl);
    });
    card.appendChild(h2hDiv);

    return card;
  }

  const root = document.getElementById('root');
  turns.forEach((turn) => {
    root.appendChild(renderCard(turn));
  });

  document.getElementById('export').addEventListener('click', () => {
    const today = new Date().toISOString().slice(0, 10);
    const payload = {
      session_date: state.session_date || today,
      total_turns: turns.length,
      calibration_count: calibrationCount,
      results: state.results,
      mapping: state.mapping,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'eval_ratings_' + today + '.json';
    a.click();
  });

  document.getElementById('reveal').addEventListener('click', () => {
    const block = document.getElementById('revealBlock');
    block.classList.remove('hidden');
    const rows = turns.map((turn) => {
      const map = state.mapping[turn.turn_id] || abMapping(turn);
      const labels = ['A', 'B'];
      const lines = map.map((cid, idx) => {
        const name = candidateLabels[cid] || cid;
        return '  ' + labels[idx] + ' = ' + name;
      });
      return turn.turn_id + ':\\n' + lines.join('\\n');
    });
    block.textContent = rows.join('\\n\\n');
  });

  updateProgress();
})();
</script>
</body>
</html>
"""


def build_html(
    sample_path: Path,
    outputs_dir: Path,
    candidates_path: Path | None = None,
) -> str:
    """Render the HTML page as a string. Pure: callers persist."""
    sample = _read_jsonl(sample_path)
    outputs = _load_outputs(outputs_dir)
    candidate_labels: dict[str, str] = {}
    if candidates_path and candidates_path.exists():
        candidate_labels = _load_candidate_labels(candidates_path)
    payload = _payload_for_html(sample, outputs, candidate_labels)
    payload_json = json.dumps(payload)
    # Embed safely — </script> in the data would break the page.
    payload_json = payload_json.replace("</", "<\\/")
    return _HTML_TEMPLATE.replace("__PAYLOAD__", payload_json)


def main_ui(
    *,
    sample_path: str | Path,
    outputs_dir: str | Path,
    out_path: str | Path,
    candidates_path: str | Path | None = None,
) -> Path:
    sample_path = Path(sample_path).expanduser()
    outputs_dir = Path(outputs_dir).expanduser()
    out_path = Path(out_path).expanduser()
    candidates_path = Path(candidates_path).expanduser() if candidates_path else None

    html = build_html(sample_path, outputs_dir, candidates_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    print(f"Wrote rating UI to {out_path}", file=sys.stderr)
    return out_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alice_eval.rating_ui",
        description="Generate the blind-rating HTML for the eval.",
    )
    parser.add_argument(
        "--sample", type=str, default=str(DEFAULT_SAMPLE_PATH),
        help="Path to eval_sample.jsonl",
    )
    parser.add_argument(
        "--outputs-dir", type=str, default=str(DEFAULT_OUTPUTS_DIR),
        help="Directory containing eval_outputs_*.jsonl",
    )
    parser.add_argument(
        "--out", type=str, default=str(DEFAULT_HTML_PATH),
        help="Output path for the rating HTML",
    )
    parser.add_argument(
        "--candidates", type=str, default=str(DEFAULT_CANDIDATES_PATH),
        help="Path to the candidate config (for labels on Reveal)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    main_ui(
        sample_path=args.sample,
        outputs_dir=args.outputs_dir,
        out_path=args.out,
        candidates_path=args.candidates,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
