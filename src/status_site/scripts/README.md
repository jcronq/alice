# Status-site screenshot harness

Playwright-driven visual regression gate for the alice status site. CI
runs it on every PR that touches the status-site source or static
bundle; it captures a deterministic matrix of PNGs against a fixture
server and diffs them against committed baselines.

## Files

| Path                                                   | Role                                               |
|--------------------------------------------------------|----------------------------------------------------|
| `screenshots.ts`                                       | Playwright driver. Reads `STATUS_SITE_URL`.        |
| `run-with-fixtures.sh`                                 | Boots a fixture server, hands off to the driver.   |
| `package.json` / `tsconfig.json`                       | Node tooling, scoped to this dir.                  |
| `tests/status_site/fixtures/alice-mind/`               | Committed fixture `ALICE_MIND` tree.               |
| `tests/status_site/__screenshots__/baseline/`          | Committed reference PNGs.                          |
| `tests/status_site/__screenshots__/current/`           | This run's output (gitignored).                    |
| `tests/status_site/__screenshots__/diff/`              | pixelmatch overlays on failure (gitignored).       |

## One-time setup

```bash
cd src/status_site/scripts/
npm install
npx playwright install --with-deps chromium
```

Playwright + chromium are TEST-ONLY. They never ship in the
alice-status container (see PROJECT_PROFILE.md § invariants).

## Usage

```bash
# Capture a fresh set against the committed fixture (first-run safe,
# exits 0 even if no baseline is present yet).
./run-with-fixtures.sh --init

# Regenerate baselines (commit the resulting PNGs).
./run-with-fixtures.sh --baseline

# CI mode: capture current, diff against baseline, exit 1 on failure.
./run-with-fixtures.sh --diff
```

Point the driver at a live instance instead:

```bash
STATUS_SITE_URL=http://10.20.30.177:8095 \
STATUS_SITE_KEY="$(cat ~/alice-mind/inner/status/key)" \
npm run screenshot -- --diff
```

## Resolving a diff failure

1. Download the `visual-regression` workflow artifact (`current/` +
   `diff/` PNGs).
2. Eyeball the diff. Red pixels are mismatches.
3. If the change is intentional: re-run `--baseline` locally, commit
   the new baselines in the same PR.
4. If the change is a regression: fix the code and let CI re-run.

## Determinism notes

The harness pins every known source of flake:

- **Fixtures** — the server reads a frozen `ALICE_MIND` tree. No live
  SSE state bleeds in because `OVERVIEW_POLLERS=0` and `ITEM_HISTORY=0`.
- **Clock** — `Date.now()` is frozen to 2026-11-18 14:00 UTC in the
  browser; server-rendered timestamps come from the fixture files.
- **Viewport** — fixed 1440×900 desktop, 390×844 mobile, DPR 2.
- **Reduced motion** — animations disabled at the browser level.
- **Masks** — a stylesheet neutralises `[data-elapsed]`,
  `[data-dynamic-time]`, `.ago`, `.live-badge`, `#presence-dot`. The
  pixel-delta threshold is 1% per image; per-pixel threshold is
  pixelmatch's default 0.1.

## Not required (yet)

The CI job runs advisory — a failing diff does NOT block merge. Flip
`required: true` on the workflow once the baselines have survived a
few PR cycles without false positives.
