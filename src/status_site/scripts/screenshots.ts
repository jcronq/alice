// Playwright screenshot harness for the alice status-site.
//
// Captures a matrix of (viewport, theme, state) PNGs against a running
// status-site server, then (optionally) diffs them against committed
// baselines. CI runs this with ``--diff`` on every status-site PR; the
// harness itself knows nothing about Python — it just drives a browser
// against ``STATUS_SITE_URL``.
//
// Modes:
//   screenshots.ts                       capture into current/, no diff
//   screenshots.ts --init                capture into baseline/ if empty,
//                                        exit 0 even if no baselines yet
//   screenshots.ts --baseline            overwrite baseline/ (regen)
//   screenshots.ts --diff                compare current/ vs baseline/,
//                                        write diff PNGs, exit 1 on fail
//
// Env:
//   STATUS_SITE_URL   default http://127.0.0.1:8095
//   STATUS_SITE_KEY   required — written to localStorage.statusKey so
//                     auth-gated POSTs work; the overview GETs are
//                     unauthenticated but the SSE handshake carries the
//                     key as a query param.
//   DIFF_THRESHOLD    per-image pixel-delta ratio (default 0.01 = 1%)
//   PIXEL_THRESHOLD   per-pixel colour delta (default 0.1 — pixelmatch
//                     default)

import { chromium, type Browser, type BrowserContext, type Page } from "playwright";
import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";
import { mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(fileURLToPath(import.meta.url), "../../../..");
const SHOTS_DIR = join(ROOT, "tests", "status_site", "__screenshots__");
const CURRENT = join(SHOTS_DIR, "current");
const BASELINE = join(SHOTS_DIR, "baseline");
const DIFF = join(SHOTS_DIR, "diff");

const URL_BASE = process.env.STATUS_SITE_URL ?? "http://127.0.0.1:8095";
const KEY = process.env.STATUS_SITE_KEY;
const DIFF_THRESHOLD = Number(process.env.DIFF_THRESHOLD ?? "0.01");
const PIXEL_THRESHOLD = Number(process.env.PIXEL_THRESHOLD ?? "0.1");

type Mode = "capture" | "init" | "baseline" | "diff";

function parseArgs(argv: readonly string[]): Mode {
  if (argv.includes("--diff")) return "diff";
  if (argv.includes("--baseline")) return "baseline";
  if (argv.includes("--init")) return "init";
  return "capture";
}

const DESKTOP = { width: 1440, height: 900 };
const MOBILE = { width: 390, height: 844 };
type Theme = "light" | "dark";

// Deterministic mask: anything with a dynamic clock (elapsed counters,
// relative timestamps, "just now" text) gets a neutral background so
// the diff ignores the moving pixels. The server-rendered HTML uses
// ``data-elapsed``, ``.ago`` and ``[data-dynamic-time]`` conventions
// across the overview widgets; if any of these are absent on your
// branch, the selector just misses and the image falls back to live
// content.
const MASK_CSS = `
  [data-elapsed],
  [data-dynamic-time],
  .ago,
  time.relative,
  .elapsed,
  .live-badge,
  #presence-dot {
    color: transparent !important;
    background: var(--color-border-line, #d0d0d0) !important;
    border-radius: 2px !important;
  }
`;

interface Shot {
  readonly name: string;
  readonly hash: string; // url fragment, e.g. "" or "#bugs"
  readonly viewports: readonly ("desktop" | "mobile")[];
  readonly themes: readonly Theme[];
  readonly prepare?: (page: Page) => Promise<void>;
}

const SHOTS: readonly Shot[] = [
  {
    name: "overview",
    hash: "",
    viewports: ["desktop", "mobile"],
    themes: ["light", "dark"],
    prepare: async (page) => {
      // Overview is the landing hash. Wait for the SSE snapshot to
      // land (section payloads render ".section-ready" markers in
      // live mode; fall back to a bounded settle if the shell is
      // still booting under the fixture dataset).
      await page.waitForSelector('[role="tablist"]', { timeout: 10_000 });
      await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
      await page.waitForTimeout(1500);
    },
  },
  {
    name: "overview-help",
    hash: "",
    viewports: ["desktop"],
    themes: ["light", "dark"],
    prepare: async (page) => {
      await page.waitForSelector('[role="tablist"]', { timeout: 10_000 });
      await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
      await page.waitForTimeout(1000);
      // ? opens the shortcut help modal (PR #905).
      await page.keyboard.press("Shift+/");
      // Modal animation + render.
      await page.waitForTimeout(600);
    },
  },
  {
    name: "overview-nav-focus",
    hash: "",
    viewports: ["desktop"],
    themes: ["light"],
    prepare: async (page) => {
      await page.waitForSelector('[role="tablist"]', { timeout: 10_000 });
      await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
      await page.waitForTimeout(1000);
      // Focus the first tab button so the ARIA tablist focus ring is
      // visible (PR #904 + #906 regression target).
      const firstTab = page.locator('[role="tablist"] [role="tab"]').first();
      await firstTab.focus();
      await page.waitForTimeout(300);
    },
  },
];

function filename(shot: Shot, viewport: "desktop" | "mobile", theme: Theme): string {
  return `${shot.name}-${viewport}-${theme}.png`;
}

async function freezeClock(page: Page): Promise<void> {
  // Fixtures target 2026-11-18 14:00 UTC. Freezing both Date.now and
  // the Date() ctor stops client-side "X minutes ago" counters from
  // drifting between runs. Server-rendered timestamps come from the
  // fixture files themselves, so they're already stable.
  const FROZEN = Date.UTC(2026, 10, 18, 14, 0, 0); // month is 0-indexed
  await page.addInitScript((frozenMs: number) => {
    const Real = Date;
    class Fixed extends Real {
      constructor(...args: unknown[]) {
        if (args.length === 0) {
          super(frozenMs);
        } else {
          // Cast is safe: Date accepts a flexible ctor arg set.
          super(...(args as [number]));
        }
      }
      static now(): number {
        return frozenMs;
      }
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).Date = Fixed as any;
  }, FROZEN);
}

async function newCtx(
  browser: Browser,
  viewport: "desktop" | "mobile",
  theme: Theme,
): Promise<{ ctx: BrowserContext; page: Page }> {
  const ctx = await browser.newContext({
    viewport: viewport === "mobile" ? MOBILE : DESKTOP,
    colorScheme: theme,
    deviceScaleFactor: 2,
    reducedMotion: "reduce",
  });
  await ctx.addInitScript((key: string | undefined) => {
    if (key) {
      try {
        localStorage.setItem("statusKey", key);
      } catch {
        // swallow — localStorage is available in Playwright contexts
      }
    }
  }, KEY);
  const page = await ctx.newPage();
  await freezeClock(page);
  await page.addStyleTag({ content: MASK_CSS }).catch(() => {});
  // The mask has to re-apply on every navigation; hook it to the
  // ``load`` event rather than only running it once.
  page.on("load", async () => {
    await page.addStyleTag({ content: MASK_CSS }).catch(() => {});
  });
  return { ctx, page };
}

async function capture(targetDir: string): Promise<string[]> {
  mkdirSync(targetDir, { recursive: true });
  const browser = await chromium.launch();
  const written: string[] = [];
  try {
    for (const shot of SHOTS) {
      for (const viewport of shot.viewports) {
        for (const theme of shot.themes) {
          const { ctx, page } = await newCtx(browser, viewport, theme);
          try {
            const url = `${URL_BASE}/${shot.hash}`;
            await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30_000 });
            if (shot.prepare) await shot.prepare(page);
            const out = join(targetDir, filename(shot, viewport, theme));
            await page.screenshot({ path: out, fullPage: true });
            written.push(out);
            console.log(`  wrote ${out}`);
          } finally {
            await ctx.close();
          }
        }
      }
    }
  } finally {
    await browser.close();
  }
  return written;
}

function diffPair(baselinePath: string, currentPath: string, diffPath: string): number {
  const base = PNG.sync.read(readFileSync(baselinePath));
  const curr = PNG.sync.read(readFileSync(currentPath));
  if (base.width !== curr.width || base.height !== curr.height) {
    // Dimension mismatch is a full fail. Emit a 1x1 PNG as the diff so
    // the artifact upload doesn't choke.
    const stub = new PNG({ width: 1, height: 1 });
    writeFileSync(diffPath, PNG.sync.write(stub));
    console.error(
      `  dimension mismatch for ${currentPath}: ` +
        `baseline ${base.width}x${base.height} vs current ${curr.width}x${curr.height}`,
    );
    return 1.0;
  }
  const { width, height } = base;
  const diff = new PNG({ width, height });
  const mismatched = pixelmatch(base.data, curr.data, diff.data, width, height, {
    threshold: PIXEL_THRESHOLD,
  });
  writeFileSync(diffPath, PNG.sync.write(diff));
  return mismatched / (width * height);
}

async function runDiff(): Promise<number> {
  if (!existsSync(BASELINE) || readdirSync(BASELINE).length === 0) {
    console.error(
      `No baselines at ${BASELINE}. Run with --init or --baseline to create them first.`,
    );
    return 2;
  }
  console.log(`Capturing current screenshots to ${CURRENT}`);
  const currents = await capture(CURRENT);
  mkdirSync(DIFF, { recursive: true });
  let failures = 0;
  for (const curr of currents) {
    const name = curr.slice(CURRENT.length + 1);
    const base = join(BASELINE, name);
    const diff = join(DIFF, name);
    if (!existsSync(base)) {
      console.error(`  missing baseline for ${name} — add one or run --baseline`);
      failures++;
      continue;
    }
    const ratio = diffPair(base, curr, diff);
    const pct = (ratio * 100).toFixed(3);
    if (ratio > DIFF_THRESHOLD) {
      console.error(`  FAIL ${name}: ${pct}% changed (threshold ${DIFF_THRESHOLD * 100}%)`);
      failures++;
    } else {
      console.log(`  ok   ${name}: ${pct}% changed`);
    }
  }
  console.log(`\n${failures} image(s) exceed threshold`);
  return failures === 0 ? 0 : 1;
}

async function main(): Promise<number> {
  const mode = parseArgs(process.argv.slice(2));
  console.log(`mode=${mode} url=${URL_BASE} baseline=${BASELINE}`);

  if (!KEY) {
    console.warn(
      "warn: STATUS_SITE_KEY is unset; authenticated POST routes will 401. " +
        "Overview GETs work without it.",
    );
  }

  if (mode === "init") {
    if (existsSync(BASELINE) && readdirSync(BASELINE).length > 0) {
      console.log(`baseline/ already populated at ${BASELINE} — nothing to do`);
      return 0;
    }
    console.log(`Capturing initial baselines to ${BASELINE}`);
    await capture(BASELINE);
    return 0;
  }
  if (mode === "baseline") {
    console.log(`Regenerating baselines at ${BASELINE}`);
    await capture(BASELINE);
    return 0;
  }
  if (mode === "diff") {
    return await runDiff();
  }
  console.log(`Capturing to ${CURRENT}`);
  await capture(CURRENT);
  return 0;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error(err);
    process.exit(1);
  },
);
