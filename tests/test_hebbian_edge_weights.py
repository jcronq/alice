"""Hebbian edge weight eval harness -- synthetic-vault retrieval comparison.

Builds a controlled wiki with a known wikilink graph, defines a query set
with ground-truth answers, and measures precision@3, recall@3, NDCG@3,
and survival rates with and without edge-weight boosting.

Design reference:
    cortex-memory/research/2026-05-18-hebbian-eval-harness-design.md
    cortex-memory/research/2026-05-15-hebbian-retrieval-integration-design.md
"""
from __future__ import annotations

import json, math, pathlib, re, sqlite3, sys, tempfile
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

# ---------------------------------------------------------------------------
# Synthetic vault

# Graph: hub links to target-a, target-b, target-c (edge_weight_sum=1 each).
# Distractor-a and distractor-b have NO link from hub (edge_weight_sum=0).
# Regression distractors are unrelated nodes.

VAULT_NOTES: list[tuple[str, str, str]] = [
    (
        "retrieval-architecture",
        "---\ntitle: Retrieval architecture design\ntags: [research, alice-architecture]\nnote_type: design\ncreated: 2026-05-01\naccess_count: 80\n---\n",
        "This note covers the retrieval architecture for the knowledge management system. "
        "FTS5 indexing and the note_metrics table provide fast full-text search.\n\n"
        "See [[target-a]], [[target-b]], and [[target-c]] for detailed component studies.",
    ),
    (
        "target-a",
        "---\ntitle: Persistence mechanism study\ntags: [research]\nnote_type: research\ncreated: 2026-05-02\naccess_count: 3\n---\n",
        "A study of persistence mechanisms in knowledge retrieval systems. High indegree "
        "nodes resist decay. This is how structurally central notes survive the access cliff.",
    ),
    (
        "target-b",
        "---\ntitle: Consolidation pathways study\ntags: [research]\nnote_type: research\ncreated: 2026-05-03\naccess_count: 1\n---\n",
        "Consolidation pathways through the knowledge system. Structural links promote "
        "forgotten notes back to active attention. The system uses indirect associations.",
    ),
    (
        "target-c",
        "---\ntitle: Associative recovery study\ntags: [research]\nnote_type: research\ncreated: 2026-05-04\naccess_count: 0\n---\n",
        "Associative recovery in knowledge systems. Notes recover through indirect "
        "structural associations. Analogy to Hebbian strengthening in neural networks.",
    ),
    (
        "distractor-a",
        "---\ntitle: Training methods study\ntags: [research]\nnote_type: research\ncreated: 2026-05-05\naccess_count: 5\n---\n",
        "Training methods for the knowledge system include gradient optimization and "
        "attention mechanisms. These apply to retrieval and generation tasks.",
    ),
    (
        "distractor-b",
        "---\ntitle: Model evaluation study\ntags: [research]\nnote_type: research\ncreated: 2026-05-06\naccess_count: 4\n---\n",
        "Evaluation of the knowledge system model. Measures precision, recall, and NDCG.",
    ),
    # Regression distractors
    (
        "truenas-vault",
        "---\ntitle: TrueNAS VM config\ntags: [cozyhem, infrastructure]\nnote_type: state\ncreated: 2026-05-01\naccess_count: 15\n---\n",
        "TrueNAS VM setup, networking, storage pools, backup strategy.",
    ),
    (
        "cozyhem-arch",
        "---\ntitle: CozyHem architecture overview\ntags: [cozyhem, architecture]\nnote_type: reference\ncreated: 2026-04-15\naccess_count: 120\n---\n",
        "CozyHem smart home control layer: service mesh, entity model, automation engine.",
    ),
    (
        "signal-ref",
        "---\ntitle: Signal messaging reference\ntags: [reference, messaging]\nnote_type: reference\ncreated: 2026-04-20\naccess_count: 85\n---\n",
        "Signal messaging: account +15853109661, daemon port 8080, JSON-RPC API.",
    ),
    (
        "fitness-notes",
        "---\ntitle: Fitness program reference\ntags: [fitness]\nnote_type: reference\ncreated: 2026-04-18\naccess_count: 60\n---\n",
        "GAINZ SYSTEM v1.0, Upper/Lower split schedule, point-based tracking.",
    ),
    (
        "gym-equip",
        "---\ntitle: Home gym equipment\ntags: [fitness]\nnote_type: reference\ncreated: 2026-04-25\naccess_count: 25\n---\n",
        "PRx rack, barbell, adjustable DBs, bench, rower, exercise bike.",
    ),
]

# ---------------------------------------------------------------------------
# Query set: 7 regression + 2 structural-lift

QUERY_SET: list[tuple[str, list[str], bool]] = [
    ("cozyhem architecture", ["cozyhem-arch"], False),
    ("signal messaging setup", ["signal-ref"], False),
    ("fitness program", ["fitness-notes"], False),
    ("TrueNAS VM setup", ["truenas-vault"], False),
    ("home gym equipment", ["gym-equip"], False),
    ("retrieval architecture design", ["retrieval-architecture"], False),
    ("system study", ["retrieval-architecture", "target-a"], False),
    ("retrieval architecture system study", ["retrieval-architecture", "target-a", "target-b"], True),
    ("retrieval system study", ["retrieval-architecture", "target-a", "target-b"], True),
]

# ---------------------------------------------------------------------------
# Metrics

@dataclass
class RankingResult:
    query: str; mode: str; retrieved: list[str]; ground_truth: list[str]; ndcg: float

def precision_at_k(r: list[str], gt: list[str], k=3):
    if not r: return 0.0
    return sum(1 for s in r[:min(k,len(r))] if s in gt) / min(k, len(r))

def recall_at_k(r: list[str], gt: list[str], k=3):
    if not gt: return 0.0
    return sum(1 for s in r[:min(k,len(r))] if s in gt) / len(gt)

def dcg_at_k(r: list[str], gt: list[str], k=3):
    return sum(1/math.log2(i+2) for i, s in enumerate(r[:min(k,len(r))]) if s in gt)

def ndcg_at_k(r: list[str], gt: list[str], k=3):
    d = dcg_at_k(r, gt, k)
    idcg = dcg_at_k(sorted(gt[:k], reverse=True), gt, k)
    return d / idcg if idcg > 0 else 0.0

def survival_rate(all_r: list[list[str]], cands: list[str]):
    if not cands: return 0.0
    seen = set()
    for r in all_r: seen.update(r)
    return sum(1 for s in cands if s in seen) / len(cands)

# ---------------------------------------------------------------------------
# Scoring (forked from cue_runner.py, simplified)

_STATE_TYPES = {"daily", "state-snapshot", "skill"}
_BUCKET2_TAGS = {"cozyhem", "alice-architecture", "ripped-by-40", "strix-halo", "alice-thinking", "alice-speaking"}
_STOPWORDS = frozenset({"a","an","and","are","as","at","be","but","by","do","does","for","from","had","has",
    "have","he","her","his","i","if","in","is","it","its","me","my","no","not","of","on","or","she",
    "so","than","that","the","their","them","they","this","to","was","we","were","what","when",
    "where","which","who","why","will","with","you","your","about","into","over","again","above",
    "although","below","both","being","been","between","can","during","each","few","further",
    "here","how","may","might","more","most","must","nor","off","once","our","other","out",
    "same","since","such","through","though","too","under","until","up","us","very","whether","whose"})

def _classify(n, tags):
    if n in _STATE_TYPES: return 1.0
    if n == "behavior": return 1.0
    if n == "finding" or any(t in _BUCKET2_TAGS for t in tags): return 1.0
    return 1.0

def _tokenize(q):
    q = q.lower()
    return [t for t in re.findall(r'[a-z0-9_-]+', q) if len(t) >= 2 and t not in _STOPWORDS]

def _build_fts(tokens):
    return " OR ".join(f'"{t}"' for t in tokens if t)

class Scorer:
    def __init__(self, db_path, edge_boost=0.3):
        self.db = db_path
        self.eb = edge_boost

    def score(self, query, hebbian=False):
        tokens = _tokenize(query)
        if not tokens: return []
        fts = _build_fts(tokens)
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT n.slug, n.title, n.note_type, n.tags_json, n.body, notes_fts.rank "
            "FROM notes n JOIN notes_fts ON notes_fts.rowid=n.rowid "
            "WHERE notes_fts MATCH ? ORDER BY notes_fts.rank LIMIT 15", (fts,)).fetchall()
        conn.close()
        slugs = [r["slug"] for r in rows]
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        if slugs:
            ps = ",".join("?"*len(slugs))
            counts = {r[0]: int(r[1] or 0) for r in conn.execute(
                f"SELECT slug, access_count FROM note_metrics WHERE slug IN ({ps})", slugs).fetchall()}
        else:
            counts = {}
        conn.close()
        ew = {}
        if hebbian and rows:
            ctx = set(slugs)
            conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            ps = ",".join("?"*len(ctx))
            ew = {r[0]: int(r[1]) for r in conn.execute(
                f"SELECT to_slug, SUM(weight) FROM note_edges WHERE from_slug IN ({ps}) GROUP BY to_slug",
                list(ctx)).fetchall()}
            conn.close()
        scored = []
        for r in rows:
            slug, title, nt, tj, body, rank = r["slug"], r["title"], r["note_type"], r["tags_json"], r["body"], r["rank"]
            tags = json.loads(tj) if tj else []
            boost = _classify(nt, tags)
            ac = counts.get(slug, 0)
            recency = 1.0 + 0.15 * math.log1p(min(ac, 100))
            base = -float(rank)
            sc = base * boost * recency
            if hebbian: sc += self.eb * ew.get(slug, 0)
            scored.append((slug, sc))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

# ---------------------------------------------------------------------------
# Harness runner

def run_eval(vault_root, db_path, edge_boost=0.3):
    p = Scorer(db_path, 0.0)
    h = Scorer(db_path, edge_boost)
    p_results, h_results = [], []
    p_all, h_all = [], []
    for query, gt, is_h in QUERY_SET:
        ps = p.score(query)
        hs = h.score(query, True)
        pt = [s for s,_ in ps[:3]]
        ht = [s for s,_ in hs[:3]]
        p_results.append(RankingResult(query, "plain", pt, gt, ndcg_at_k(pt, gt)))
        h_results.append(RankingResult(query, "hebbian", ht, gt, ndcg_at_k(ht, gt)))
        p_all.append(pt); h_all.append(ht)
    def agg(res):
        n = len(res); tp = sum(precision_at_k(r.retrieved, r.ground_truth) for r in res)
        tr = sum(recall_at_k(r.retrieved, r.ground_truth) for r in res)
        tn = sum(r.ndcg for r in res)
        p, r_val = tp/n if n else 0, tr/n if n else 0
        return {"precision@3": p, "recall@3": r_val, "F1@3": 2*p*r_val/(p+r_val) if (p+r_val) else 0, "ndcg@3": tn/n if n else 0}
    pa, ha = agg(p_results), agg(h_results)
    # Regression: non-hebbian queries, any GT note dropped from top-3?
    regs = 0; rc = 0
    for pr, hr in zip(p_results, h_results):
        if any(q[0] == pr.query and not q[2] for q in QUERY_SET):
            rc += 1
            for g in pr.ground_truth:
                if g in pr.retrieved and g not in hr.retrieved: regs += 1
    # Hebbian query details
    details = []
    for pr, hr in zip(p_results, h_results):
        is_hq = any(q[2] for q in QUERY_SET if q[0] == pr.query)
        if is_hq:
            details.append({"query": pr.query, "gt": pr.ground_truth,
                "plain": pr.retrieved, "hebbian": hr.retrieved,
                "p_ndcg": pr.ndcg, "h_ndcg": hr.ndcg,
                "improved": hr.ndcg > pr.ndcg, "regressed": hr.ndcg < pr.ndcg})
    return {"plain": pa, "hebbian": ha, "p_survival": survival_rate(p_all, [s for s,_,_ in VAULT_NOTES]),
            "h_survival": survival_rate(h_all, [s for s,_,_ in VAULT_NOTES]),
            "survival_delta": survival_rate(h_all, [s for s,_,_ in VAULT_NOTES]) - survival_rate(p_all, [s for s,_,_ in VAULT_NOTES]),
            "regressions": regs, "regression_checks": rc, "edge_boost": edge_boost, "details": details}

# ---------------------------------------------------------------------------
# DB builder

def create_db(vr, dp):
    conn = sqlite3.connect(str(dp))
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE notes (slug TEXT PRIMARY KEY, title TEXT, note_type TEXT, tags_json TEXT, body TEXT, path TEXT);
        CREATE VIRTUAL TABLE notes_fts USING fts5(slug, title, body, content='notes', tokenize='unicode61');
        CREATE TABLE note_metrics (slug TEXT PRIMARY KEY, access_count INTEGER DEFAULT 0);
        CREATE TABLE note_edges (from_slug TEXT, to_slug TEXT, weight INTEGER DEFAULT 1, PRIMARY KEY(from_slug, to_slug));
    """)
    for slug, fm, body in VAULT_NOTES:
        title = slug.replace("-"," ").title()
        for line in fm.split("\n"):
            if line.startswith("title: "): title = line[7:].strip().strip('"'); break
        tags = ["research"]
        for line in fm.split("\n"):
            if line.startswith("tags: ["):
                tags = [t.strip() for t in line[line.index("[")+1:line.index("]")].split(",") if t.strip()]
                break
        p = f"research/{slug}.md"
        (vr / p).parent.mkdir(parents=True, exist_ok=True)
        (vr / p).write_text(fm + "\n" + body)
        ac = 0
        for line in fm.split("\n"):
            if line.startswith("access_count:"):
                try: ac = int(line.split(":")[1].strip())
                except: pass
        c.execute("INSERT INTO notes VALUES(?,?,?,?,?,?)", (slug, title, "research", json.dumps(tags), body, p))
        c.execute("INSERT INTO notes_fts VALUES(?,?,?)", (slug, title, body))
        if ac > 0: c.execute("INSERT INTO note_metrics VALUES(?,?)", (slug, ac))
        for m in re.findall(r"\[\[([a-z0-9_-]+)\]\]", body):
            c.execute("INSERT INTO note_edges VALUES(?,?,1)", (slug, m))
    conn.commit(); conn.close()

# ---------------------------------------------------------------------------
# Tests

def test_plain_fts_returns_results():
    with tempfile.TemporaryDirectory() as td:
        vr, dp = pathlib.Path(td)/"cortex-memory", pathlib.Path(td)/"state"/"cortex-index.db"
        dp.parent.mkdir(parents=True, exist_ok=True); create_db(vr, dp)
        scored = Scorer(dp, 0.0).score("cozyhem architecture")
        assert len(scored) > 0
        assert "cozyhem-arch" in [s for s,_ in scored]

def test_hebbian_promotes_structurally_connected():
    with tempfile.TemporaryDirectory() as td:
        vr, dp = pathlib.Path(td)/"cortex-memory", pathlib.Path(td)/"state"/"cortex-index.db"
        dp.parent.mkdir(parents=True, exist_ok=True); create_db(vr, dp)
        q = "retrieval architecture system study"
        plain = Scorer(dp, 0.0).score(q)
        hebbian = Scorer(dp, 0.5).score(q, True)
        p3 = {s for s,_ in plain[:3]}
        h3 = {s for s,_ in hebbian[:3]}
        # Hub must be in both top-3
        assert "retrieval-architecture" in p3
        assert "retrieval-architecture" in h3
        # At least one target must appear in hebbian top-3 and not in plain
        targets_in_h = h3 & {"target-a", "target-b", "target-c"}
        targets_not_in_p = targets_in_h - p3
        assert targets_not_in_p, f"No target promoted: plain={p3} hebbian={h3}"

def test_edge_weight_additive():
    with tempfile.TemporaryDirectory() as td:
        vr, dp = pathlib.Path(td)/"cortex-memory", pathlib.Path(td)/"state"/"cortex-index.db"
        dp.parent.mkdir(parents=True, exist_ok=True); create_db(vr, dp)
        s05 = Scorer(dp, 0.5); s10 = Scorer(dp, 1.0)
        q = "retrieval architecture"
        p = {s: sc for s, sc in s05.score(q)}
        h05 = {s: sc for s, sc in s05.score(q, True)}
        h10 = {s: sc for s, sc in s10.score(q, True)}
        for slug in p:
            diff = h10.get(slug,0) - h05.get(slug,0)
            edge_comp = h05.get(slug,0) - p.get(slug,0)
            assert abs(diff - edge_comp) < 0.01, f"{slug}: diff={diff} expected={edge_comp}"

def test_hebbian_disabled_by_default():
    with tempfile.TemporaryDirectory() as td:
        vr, dp = pathlib.Path(td)/"cortex-memory", pathlib.Path(td)/"state"/"cortex-index.db"
        dp.parent.mkdir(parents=True, exist_ok=True); create_db(vr, dp)
        s = Scorer(dp, 0.0)
        assert s.score("cliff") == s.score("cliff", True)

def test_harness_metrics():
    with tempfile.TemporaryDirectory() as td:
        vr, dp = pathlib.Path(td)/"cortex-memory", pathlib.Path(td)/"state"/"cortex-index.db"
        dp.parent.mkdir(parents=True, exist_ok=True); create_db(vr, dp)
        r = run_eval(vr, dp, 0.5)
        assert r["hebbian"]["ndcg@3"] >= r["plain"]["ndcg@3"], \
            f"NDCG: hebbian={r['hebbian']['ndcg@3']:.3f} < plain={r['plain']['ndcg@3']:.3f}"

def test_regression_guard():
    with tempfile.TemporaryDirectory() as td:
        vr, dp = pathlib.Path(td)/"cortex-memory", pathlib.Path(td)/"state"/"cortex-index.db"
        dp.parent.mkdir(parents=True, exist_ok=True); create_db(vr, dp)
        r = run_eval(vr, dp, 0.5)
        assert r["regressions"] == 0, f"Regresions: {r['regressions']}/{r['regression_checks']}"

def test_hebbian_query_improvement():
    with tempfile.TemporaryDirectory() as td:
        vr, dp = pathlib.Path(td)/"cortex-memory", pathlib.Path(td)/"state"/"cortex-index.db"
        dp.parent.mkdir(parents=True, exist_ok=True); create_db(vr, dp)
        r = run_eval(vr, dp, 0.5)
        improved = sum(1 for d in r["details"] if d["improved"])
        assert improved > 0, f"No Hebbian query improved: details={[(d['query'],d['plain'],d['hebbian']) for d in r['details']]}"

def test_edge_boost_sensitivity():
    with tempfile.TemporaryDirectory() as td:
        vr, dp = pathlib.Path(td)/"cortex-memory", pathlib.Path(td)/"state"/"cortex-index.db"
        dp.parent.mkdir(parents=True, exist_ok=True); create_db(vr, dp)
        r_low = run_eval(vr, dp, 0.1)
        r_high = run_eval(vr, dp, 1.0)
        assert r_high["h_survival"] >= r_low["h_survival"], \
            f"Higher edge boost should not decrease survival: {r_high['h_survival']:.3f} < {r_low['h_survival']:.3f}"

def test_metrics_helpers():
    gt = ["a","b","c"]
    assert precision_at_k(["a","b","c"], gt) == 1.0
    assert recall_at_k(["a","b","c"], gt) == 1.0
    assert ndcg_at_k(["a","b","c"], gt) == 1.0
    assert precision_at_k(["a","d","e"], gt) == 1/3
    assert ndcg_at_k([], gt) == 0.0

def test_vault_note_count():
    with tempfile.TemporaryDirectory() as td:
        vr, dp = pathlib.Path(td)/"cortex-memory", pathlib.Path(td)/"state"/"cortex-index.db"
        dp.parent.mkdir(parents=True, exist_ok=True); create_db(vr, dp)
        c = sqlite3.connect(str(dp))
        assert c.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == len(VAULT_NOTES)
        assert c.execute("SELECT COUNT(*) FROM note_edges").fetchone()[0] > 0
        c.close()

def test_standalone_run():
    """Run the harness and print a summary (also used as standalone entry point)."""
    print("Hebbian Edge Weight Eval Harness")
    print("=" * 60)
    for eb in [0.0, 0.3, 0.5, 1.0]:
        with tempfile.TemporaryDirectory() as td:
            vr, dp = pathlib.Path(td)/"cortex-memory", pathlib.Path(td)/"state"/"cortex-index.db"
            dp.parent.mkdir(parents=True, exist_ok=True); create_db(vr, dp)
            r = run_eval(vr, dp, eb)
            print(f"\nedge_boost={eb}")
            print(f"  Plain:  P@3={r['plain']['precision@3']:.3f} R@3={r['plain']['recall@3']:.3f} "
                  f"F1@3={r['plain']['F1@3']:.3f} NDCG={r['plain']['ndcg@3']:.3f}")
            print(f"  Hebb:   P@3={r['hebbian']['precision@3']:.3f} R@3={r['hebbian']['recall@3']:.3f} "
                  f"F1@3={r['hebbian']['F1@3']:.3f} NDCG={r['hebbian']['ndcg@3']:.3f}")
            print(f"  Survival: plain={r['p_survival']:.3f} hebb={r['h_survival']:.3f} d={r['survival_delta']:+.3f}")
            print(f"  Regressions: {r['regressions']}/{r['regression_checks']}")
            for d in r["details"]:
                st = "+" if d["improved"] else ("-" if d["regressed"] else "=")
                print(f"  {st} {d['query'][:35]:35s} p_ndcg={d['p_ndcg']:.3f} h_ndcg={d['h_ndcg']:.3f}")

if __name__ == "__main__":
    test_standalone_run()
