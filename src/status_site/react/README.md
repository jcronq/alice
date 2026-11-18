# status-site React shell

Slice 1 of issue #639 — the AG-UI reactive rewrite of the status site.
The current scope is a single `<RunningNowPanel />` served under
`/ui/running-now`. Later slices will port Waiting-on-you, In flight,
Blocked, and Bugs.

## Build

```bash
cd src/status_site/react
npm install
npm run build        # or: npx vite build
```

Output goes to `../static/react-dist/` (relative to this directory —
i.e. `src/status_site/static/react-dist/`). The dist directory is
**checked in** because the production container
(`python:3.12-alpine` → `alice-status`) has no npm/vite — it just
bind-mounts the whole `src/status_site/` tree read-only and the
Python server (`server.py::_react_dist`) serves the committed bundle
under `/ui/`.

If you forget to commit the rebuilt dist bundle, the backend will
keep serving the old bytes. CI does not currently rebuild the bundle
— that's deliberate for slice 1 so we don't pull npm into the test
pipeline. When slice 2 ships a widget library, revisit.

## Dev loop

`npm run dev` boots the Vite dev server on `localhost:5173`. The
dev server proxies nothing — point the React shell at a running
status-site via the same `?k=<key>` query param the production
bundle uses (Vite serves `src/status_site/react/index.html`, which
then fetches `/events?section=running_now&k=…` relative to the
origin — set the origin to the status-site when developing against
live data).

## Tests

```bash
npm test             # vitest run — jsdom environment
```

Three test files:

- `sseClient.test.ts` — SSE frame parser (heartbeat, multi-line
  data, malformed JSON).
- `runningNowReducer.test.ts` — reducer state transitions.
- `RunningNowPanel.test.tsx` — React component renders rows +
  empty state + status pill.

## Layout

```
react/
├── index.html              Vite HTML entry
├── package.json
├── tsconfig.json
├── vite.config.ts          base: '/ui/', outDir: ../static/react-dist
└── src/
    ├── main.tsx            ReactDOM root mount
    ├── App.tsx             slice-1 shell (just mounts the panel)
    ├── RunningNowPanel.tsx subscribes to /events, renders
    ├── runningNowReducer.ts state reducer (replaces list wholesale)
    ├── sseClient.ts        fetch-based SSE reader
    ├── agui.ts             AG-UI event envelope types
    └── *.test.*            vitest suites
```

## Why these picks

- **React + Vite** per Alice's decision comment on #639 and Jason's
  earlier connectivity-guide comment. Overrides the earlier
  Preact+htm / Lit / vanilla options.
- **Fetch-based SSE** (not EventSource) — the connectivity-guide
  pin. EventSource can't POST + can't detect dead TCP connections;
  fetch + ReadableStream lets us share a client with command POSTs
  and surfaces socket errors as rejected promises.
- **Snapshot-first** — the first event on every (re)connect is a
  full `STATE_SNAPSHOT`; subsequent changes are full snapshots too.
  No event ids, no gap reconciliation.
- **No `@ag-ui/http-agent`** — per the connectivity guide, HttpAgent
  trips on snapshot-first streams where activity deltas arrive
  without prior activity snapshots. We hand-decode a thin subset of
  the AG-UI event vocabulary instead.
