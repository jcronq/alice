# alice_pricing — API-cost estimates + actuals for Claude workers

A tiny module that attaches dollar amounts to every worker Alice
dispatches. Jason wants a running P&L on the status site; this
package is the price-book + the parsers + the formatter the status
site and the bug-fix dispatcher share.

## Shape

```python
from alice_pricing import (
    estimate_tokens, estimate_cost, format_usd,
    read_worker_tokens, actual_cost_from_tokens,
    rates_for, PRICING_SOURCE_DATE,
)
```

Four helpers do the work:

| Helper | Called at | Writes |
|---|---|---|
| `estimate_tokens(text)` | dispatch time | — |
| `estimate_cost(model, prompt_tokens, out_budget)` | dispatch time | `estimate_usd` in bug frontmatter |
| `read_worker_tokens(scratch_dir)` | poll time | token row (for the next helper) |
| `actual_cost_from_tokens(model, in, out, …)` | poll time | `actual_usd` in bug frontmatter |

`rates_for(model_id)` returns the rate dict for a model id — resolves
aliases, defaults local-inference ids to a zero dict so Thinking's
Hermes doesn't accidentally get priced.

## Rate source

The rate table lives in `rates.py` as a plain Python literal. It was
pulled **2026-11-02** from Anthropic's own docs:

- https://docs.anthropic.com/en/docs/about-claude/pricing
- https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
- https://www.anthropic.com/news/claude-opus-4-7

When Anthropic changes a rate, bump the dict and the
`PRICING_SOURCE_DATE` constant in the same edit. No fetcher — Jason
wants in-place edits, not a scheduled job that could silently drift.

## Token estimation

Tokenizer-free: 4 chars ≈ 1 token (Anthropic's own documented rule
of thumb for English prose + code). The error is small relative to
the model-choice variance (Opus is 5× Sonnet). If you need real
counts, swap in `anthropic.Anthropic().messages.count_tokens`;
the public API is narrow enough that it's a one-liner.

## Actuals

`read_worker_tokens` looks at `scratch_dir/result.json` first (the
SDK envelope under `--output-format json`), then falls back to
`stderr.log` regex. If neither surface yielded a count, it returns
`None` and the dispatcher writes `actual_usd: null` with a human-
readable `cost_attribution_note` explaining why.

## Deferred — index.md pricing

Index.md's items are hand-maintained markdown; the renderer doesn't
walk them item by item (no per-item detail file like `bug-N.md`).
Jason might reword a line at any moment. For MVP we only price
bugs and surface a pipeline-aggregate at the bottom of the Bugs
tab. Attaching per-item dollar amounts to index.md items will need
either (a) a first-class detail-file layout for todos (mirror the
bug pattern) or (b) a lightweight sidecar manifest the renderer
reads next to each line. Punt until Jason asks for it.
