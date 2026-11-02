"""Pre-flight token + cost estimates for a Claude worker call.

The dispatcher calls ``estimate_tokens`` on the rendered worker prompt
and ``estimate_cost`` with an output budget sized for a code fix
(8K tokens by default). The result is written to the bug's
frontmatter as ``estimate_usd``.

Token counting is deliberately tokenizer-free: the Anthropic model
family uses a BPE tokenizer we don't ship. The 4-chars-per-token
heuristic is the one Anthropic itself documents in its "estimating
cost" guidance for English prose + code, and the error is small
compared to the model-choice variance (Opus vs Sonnet is a 5× swing).

If a user ever wants real counts for estimates, swap in
``anthropic.Anthropic().messages.count_tokens`` — this module keeps
the API narrow so the swap is a one-liner.
"""

from __future__ import annotations

from typing import Mapping

from .rates import rates_for


# Anthropic's own documented rule of thumb for English prose + code.
# ``prompt_tokens ≈ len(text) / CHARS_PER_TOKEN``.
CHARS_PER_TOKEN = 4

# Default output budget for a code-fix worker. Sized for a single-PR
# fix with a reasoned diff. Callers pricing a long multi-file
# refactor should override.
DEFAULT_OUTPUT_TOKENS = 8_000


def estimate_tokens(prompt_text: str) -> int:
    """Rough token count via the 4-chars-per-token heuristic.

    Rounds up — a tiny prompt still costs at least one token in
    practice, and the dispatcher's estimate column reads cleaner
    when a one-character prompt doesn't display as 0 tokens.

    Returns 0 for the empty string so arithmetic downstream stays
    well-defined; a cost of $0 is the honest answer there.
    """
    if not prompt_text:
        return 0
    length = len(prompt_text)
    # Round up: ``(length + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN``.
    return (length + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def estimate_cost(
    model: str,
    prompt_tokens: int,
    expected_output_tokens: int = DEFAULT_OUTPUT_TOKENS,
    *,
    rates: Mapping[str, float] | None = None,
) -> float:
    """Dollars for a prompt + expected output on ``model``.

    No caching assumption — the dispatcher's workers run with a fresh
    context on every bug, so the first-call input is all non-cached.
    If the caller later wires in cache hits, they can bias the
    estimate down via a custom ``rates`` dict.

    Negative token counts are coerced to zero (defensive — the only
    way they appear is a buggy caller, and we'd rather emit $0 than
    a negative dollar amount).
    """
    rate_dict = rates if rates is not None else rates_for(model)
    pt = max(0, int(prompt_tokens))
    ot = max(0, int(expected_output_tokens))
    input_dollars = pt / 1_000_000 * rate_dict["input_per_mtok"]
    output_dollars = ot / 1_000_000 * rate_dict["output_per_mtok"]
    return input_dollars + output_dollars


def format_usd(dollars: float) -> str:
    """Render ``dollars`` as a user-facing string.

    Rules — pick whichever is tighter / cleaner:
      * zero                       → ``$0.00``
      * < $0.01                    → four significant figures, e.g. ``$0.001234``
      * ≥ $0.01 and < $10          → two decimals, e.g. ``$0.42``
      * ≥ $10                      → two decimals, e.g. ``$42.00``
      * negative                   → leading ``-$`` (shouldn't happen in practice)

    The "four significant figures" branch handles edge cases where a
    cheap Haiku ping costs fractions of a cent and ``$0.00`` would
    read as "free".
    """
    if dollars == 0:
        return "$0.00"
    if dollars < 0:
        return "-" + format_usd(-dollars)
    if dollars < 0.01:
        # Four significant figures. ``%g`` with ``.4g`` picks that
        # representation; strip trailing zeros for readability but
        # keep at least one decimal digit.
        rendered = f"{dollars:.4g}"
        return f"${rendered}"
    return f"${dollars:.2f}"
