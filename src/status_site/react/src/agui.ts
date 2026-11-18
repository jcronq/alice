// AG-UI event envelope types for slice-1 of #639.
//
// We deliberately skip `@ag-ui/http-agent` on the client — per Jason's
// connectivity-guide pin on #639, snapshot-first streams where
// activity deltas arrive without prior activity snapshots trip
// HttpAgent. Instead we hand-decode a thin subset of the AG-UI
// event vocabulary. The server side (`src/status_site/_ag_ui.py`)
// emits the matching shape.
//
// Slice-1 only needs two event types:
//
//   - `STATE_SNAPSHOT` — full `running_now` section payload. Fires on
//     (re)connect and on any change the server-side change-detector
//     notices.
//   - `RUN_STARTED` / `RUN_FINISHED` — reserved for future per-task
//     progress events. Not emitted in slice-1 but declared here so the
//     reducer can ignore them without a decode error.
//
// If this file grows past slice-1, pull @ag-ui/encoder's TypeScript
// types in and align with them; for now the hand-rolled shape keeps
// the bundle small and the build toolchain-free of extra deps.

export type RunningNowRow = {
  branch: string
  worktree: string
  alive: boolean
  pr_url: string
  elapsed: string
  task_status: string
  issue: string
}

export type RunningNowState = {
  markdown: string
  rows: RunningNowRow[]
}

export type StateSnapshotEvent = {
  type: 'STATE_SNAPSHOT'
  /** AG-UI uses `snapshot` (STATE_SNAPSHOT) or `delta` (STATE_DELTA). */
  snapshot: { running_now: RunningNowState }
  ts: string
}

export type RunStartedEvent = {
  type: 'RUN_STARTED'
  threadId: string
  runId: string
  ts: string
}

export type RunFinishedEvent = {
  type: 'RUN_FINISHED'
  threadId: string
  runId: string
  ts: string
}

export type AgUiEvent = StateSnapshotEvent | RunStartedEvent | RunFinishedEvent

export function isStateSnapshot(ev: AgUiEvent): ev is StateSnapshotEvent {
  return ev.type === 'STATE_SNAPSHOT'
}
