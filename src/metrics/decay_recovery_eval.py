"""Decay recovery eval harness — read-only M5 measurement across threshold configs.

Simulates Phase 3.5 cosine pairing against the current vault without writing
to the vault. Measures M5 behavioral recovery ratio for each configuration.

Usage:
    python3 -m metrics.decay_recovery_eval \\
        --db ~/alice-mind/inner/state/cortex-index.db \\
        [--thresholds 0.40 0.45 0.50 0.55 0.60] \\
        [--keyword-injection] \\
        [--min-access-count 2] \\
        [--output table] \\
        [--baseline]

This is a read-only tool — it never writes to the vault or modifies any state.
It informed PR #470 (cosine threshold 0.40 → 0.45).
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict


# ---------------------------------------------------------------------------
# Tokenization (matches Phase 3.5 title cosine implementation)
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, keep words with length >= 3."""
    text = text.lower()
    tokens = []
    current = []
    for ch in text:
        if ch.isalpha() or ch.isdigit():
            current.append(ch)
        else:
            if len(current) >= 3:
                tokens.append(''.join(current))
            current = []
    if len(current) >= 3:
        tokens.append(''.join(current))
    return tokens


def word_freq(tokens: list[str]) -> dict[str, float]:
    """Bag-of-words frequency vector."""
    freq = defaultdict(float)
    for t in tokens:
        freq[t] += 1.0
    return dict(freq)


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------

def cosine_sim(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity between two word-frequency vectors."""
    common = set(a.keys()) & set(b.keys())
    if not common:
        return 0.0
    dot = sum(a[w] * b[w] for w in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Database access (read-only)
# ---------------------------------------------------------------------------

def load_db(db_path: str) -> dict:
    """Load notes + metrics from cortex-index.db.

    The harness simulates *new* pairings on top of the existing vault, so
    the real links table is intentionally ignored — recovery is judged
    against the simulated inbound counts, not the current link graph.

    Returns:
        notes: {slug: {title, tags_json, body}}
        metrics: {slug: {access_count}}
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Notes table
    cur.execute("SELECT slug, title, tags_json, body FROM notes")
    notes = {}
    for row in cur.fetchall():
        notes[row['slug']] = {
            'title': row['title'] or '',
            'tags_json': row['tags_json'] or '[]',
            'body': row['body'] or '',
        }

    # Note metrics table
    cur.execute("SELECT slug, access_count FROM note_metrics")
    metrics = {}
    for row in cur.fetchall():
        metrics[row['slug']] = {
            'access_count': row['access_count'] or 0,
        }

    conn.close()
    return {
        'notes': notes,
        'metrics': metrics,
    }


# ---------------------------------------------------------------------------
# Decay / access note identification
# ---------------------------------------------------------------------------

def identify_decay_notes(notes: dict) -> list[str]:
    """Return slugs of notes tagged with 'decay'."""
    decay = []
    for slug, info in notes.items():
        tags = json.loads(info['tags_json']) if info['tags_json'] else []
        if 'decay' in tags:
            decay.append(slug)
    return decay


def identify_accessed_notes(metrics: dict, min_access: int) -> list[str]:
    """Return slugs of notes with access_count >= min_access."""
    accessed = []
    for slug, m in metrics.items():
        if m['access_count'] >= min_access:
            accessed.append(slug)
    return accessed


# ---------------------------------------------------------------------------
# Keyword injection
# ---------------------------------------------------------------------------

def get_trigger_keywords(notes: dict, slug: str) -> list[str]:
    """Extract trigger_keywords from a note's body (frontmatter stored in body column).
    
    In the current vault, trigger_keywords are stored as frontmatter in the body
    column of the notes table. Format: "trigger_keywords: [kw1, kw2, ...]"
    
    Returns empty list if not found — caller should handle gracefully.
    """
    body = notes.get(slug, {}).get('body', '')
    # Match trigger_keywords: [...] anywhere in the body
    m = re.search(r'trigger_keywords:\s*\[([^\]]+)\]', body, re.IGNORECASE)
    if m:
        keywords_str = m.group(1)
        # Split on comma, strip quotes/spaces
        keywords = []
        for kw in keywords_str.split(','):
            kw = kw.strip().strip('"').strip("'").strip()
            if kw:
                keywords.append(kw.lower())
        return keywords
    return []


def inject_keywords(title: str, keywords: list[str]) -> str:
    """Append trigger keywords to a title for pairing purposes.
    
    This simulates the Layer 2a keyword injection hypothesis — adding
    trigger_keywords from the accessed note to the decayed note's title
    to improve pairing density.
    """
    if not keywords:
        return title
    appended = ' '.join(keywords)
    return title + ' ' + appended


# ---------------------------------------------------------------------------
# Pairing simulation
# ---------------------------------------------------------------------------

def simulate_pairing(
    decay_slugs: list[str],
    accessed_slugs: list[str],
    notes: dict,
    thresholds: list[float],
    keyword_injection: bool,
) -> dict[float, dict[str, int]]:
    """Simulate pairing for each threshold, optionally with keyword injection.
    
    Returns:
        {threshold: {decay_slug: inbound_link_count}}
    """
    # Pre-compute title token freqs for all notes
    title_freqs = {}
    for slug in decay_slugs + accessed_slugs:
        title = notes.get(slug, {}).get('title', '')
        title_freqs[slug] = word_freq(tokenize(title))

    # Pre-compute trigger keywords for accessed notes (for injection)
    if keyword_injection:
        accessed_keywords = {}
        for slug in accessed_slugs:
            accessed_keywords[slug] = get_trigger_keywords(notes, slug)

    results = {}

    for threshold in thresholds:
        inbound_counts = {slug: 0 for slug in decay_slugs}

        for decay_slug in decay_slugs:
            decay_freq = title_freqs[decay_slug]

            for accessed_slug in accessed_slugs:
                accessed_freq = title_freqs[accessed_slug]
                accessed_title = notes[accessed_slug]['title']

                # Apply keyword injection if enabled
                if keyword_injection:
                    keywords = accessed_keywords.get(accessed_slug, [])
                    if keywords:
                        accessed_title = inject_keywords(accessed_title, keywords)
                        accessed_freq = word_freq(tokenize(accessed_title))

                sim = cosine_sim(decay_freq, accessed_freq)
                if sim >= threshold:
                    inbound_counts[decay_slug] += 1

        results[threshold] = inbound_counts

    return results


# ---------------------------------------------------------------------------
# M5 computation
# ---------------------------------------------------------------------------

def compute_m5(
    decay_slugs: list[str],
    inbound_counts: dict[str, int],
    metrics: dict,
) -> dict:
    """Compute M5 = mean(access_count of recovered) / mean(access_count of unrecovered).
    
    Recovered = notes with >= 2 inbound structural links.
    Unrecovered = notes with < 2 inbound structural links.
    
    Returns:
        {
            'm5': float or math.inf,
            'recovered_mean': float,
            'unrecovered_mean': float,
            'recovered_count': int,
            'unrecovered_count': int,
            'total_pairs': int,
        }
    """
    recovered = []
    unrecovered = []

    for slug in decay_slugs:
        count = inbound_counts.get(slug, 0)
        ac = metrics.get(slug, {}).get('access_count', 0)
        if count >= 2:
            recovered.append(ac)
        else:
            unrecovered.append(ac)

    recovered_mean = sum(recovered) / len(recovered) if recovered else 0.0
    unrecovered_mean = sum(unrecovered) / len(unrecovered) if unrecovered else 0.0

    if unrecovered_mean == 0:
        m5 = math.inf
    else:
        m5 = recovered_mean / unrecovered_mean

    return {
        'm5': m5,
        'recovered_mean': recovered_mean,
        'unrecovered_mean': unrecovered_mean,
        'recovered_count': len(recovered),
        'unrecovered_count': len(unrecovered),
        'total_pairs': sum(1 for c in inbound_counts.values() if c >= 2),
    }


# ---------------------------------------------------------------------------
# Full eval pipeline
# ---------------------------------------------------------------------------

def run_eval(
    db_path: str,
    thresholds: list[float],
    keyword_injection: bool = False,
    min_access_count: int = 2,
) -> list[dict]:
    """Run the full decay recovery evaluation.
    
    Returns a list of result dicts, one per threshold config.
    """
    data = load_db(db_path)
    notes = data['notes']
    metrics = data['metrics']

    decay_slugs = identify_decay_notes(notes)
    accessed_slugs = identify_accessed_notes(metrics, min_access_count)

    print(f"Loaded: {len(notes)} notes, {len(metrics)} metrics", file=sys.stderr)
    print(f"Decay notes: {len(decay_slugs)}, Accessed notes: {len(accessed_slugs)}", file=sys.stderr)

    config_label = "keyword_injection" if keyword_injection else "control"
    print(f"Config: threshold sweep {thresholds}, keyword_injection={keyword_injection}", file=sys.stderr)

    inbound_counts = simulate_pairing(
        decay_slugs, accessed_slugs, notes, thresholds, keyword_injection
    )

    results = []
    for threshold in thresholds:
        m5_result = compute_m5(decay_slugs, inbound_counts[threshold], metrics)
        row = {
            'threshold': threshold,
            'config': config_label,
            'm5': m5_result['m5'],
            'recovered_count': m5_result['recovered_count'],
            'unrecovered_count': m5_result['unrecovered_count'],
            'total_pairs': m5_result['total_pairs'],
        }
        results.append(row)

    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_table(results: list[dict]) -> str:
    """Format results as a markdown table."""
    header = "| Threshold | Config | M5 | Recovered | Unrecovered | Total Pairs |"
    sep = "|-----------|--------|------|-----------|-------------|-------------|"
    rows = [header, sep]

    for r in results:
        m5_str = f"{r['m5']:.3f}" if r['m5'] != math.inf else "inf"
        rows.append(
            f"| {r['threshold']} | {r['config']} | {m5_str} | "
            f"{r['recovered_count']} | {r['unrecovered_count']} | {r['total_pairs']} |"
        )

    return '\n'.join(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Decay recovery eval harness — read-only M5 measurement'
    )
    parser.add_argument(
        '--db', required=True,
        help='Path to cortex-index.db'
    )
    parser.add_argument(
        '--thresholds', nargs='+', type=float, default=[0.40, 0.45, 0.50, 0.55, 0.60],
        help='Cosine thresholds to sweep'
    )
    parser.add_argument(
        '--keyword-injection', action='store_true',
        help='Enable keyword injection (Layer 2a)'
    )
    parser.add_argument(
        '--min-access-count', type=int, default=2,
        help='Minimum access_count to be considered "accessed"'
    )
    parser.add_argument(
        '--output', choices=['table', 'json'], default='table',
        help='Output format (default: table)'
    )
    parser.add_argument(
        '--baseline', action='store_true',
        help='Also compute baseline M5 (no simulated links)'
    )

    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    results = run_eval(
        args.db,
        args.thresholds,
        keyword_injection=args.keyword_injection,
        min_access_count=args.min_access_count,
    )

    if args.baseline:
        # Compute baseline: decay notes with zero simulated inbound links.
        # Single load_db call — both decay_slugs and metrics come from the same snapshot.
        baseline_data = load_db(args.db)
        decay_slugs = identify_decay_notes(baseline_data['notes'])
        zero_counts = {slug: 0 for slug in decay_slugs}
        baseline = compute_m5(decay_slugs, zero_counts, baseline_data['metrics'])
        print(f"\nBaseline M5 (no simulated links): {baseline['m5']:.3f}", file=sys.stderr)
        print(f"  Recovered: {baseline['recovered_count']}, Unrecovered: {baseline['unrecovered_count']}", file=sys.stderr)

    if args.output == 'table':
        print(format_table(results))
    else:
        # Convert inf to string for JSON
        serializable = []
        for r in results:
            sr = dict(r)
            if sr['m5'] == math.inf:
                sr['m5'] = 'inf'
            serializable.append(sr)
        print(json.dumps(serializable, indent=2))


if __name__ == '__main__':
    main()
