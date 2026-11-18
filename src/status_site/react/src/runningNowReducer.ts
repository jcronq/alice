// State reducer for <RunningNowPanel />.
//
// Kept separate from the component so it can be unit-tested without
// mounting React. The reducer is a plain function of
// `(state, action) -> state`; actions are AG-UI events we care about,
// plus `connection.*` meta-actions dispatched by the panel.

import type { AgUiEvent, RunningNowState } from './agui'

export type ConnectionStatus = 'connecting' | 'open' | 'retrying' | 'closed'

export type RunningNowPanelState = {
  /** Last-known section payload. Empty rows until the first snapshot. */
  running: RunningNowState
  /** Connection status (for the status pill in the header). */
  status: ConnectionStatus
  /** ISO timestamp of the most recent server message (heartbeat or event). */
  lastEventAt: string | null
  /** Retry attempt counter; reset to 0 on each successful snapshot. */
  retryAttempt: number
}

export const initialState: RunningNowPanelState = {
  running: { markdown: '', rows: [] },
  status: 'connecting',
  lastEventAt: null,
  retryAttempt: 0,
}

export type RunningNowAction =
  | { kind: 'event'; event: AgUiEvent }
  | { kind: 'connection.open' }
  | { kind: 'connection.retry'; attempt: number }
  | { kind: 'connection.closed' }

export function runningNowReducer(
  state: RunningNowPanelState,
  action: RunningNowAction,
): RunningNowPanelState {
  switch (action.kind) {
    case 'connection.open':
      return { ...state, status: 'open' }
    case 'connection.retry':
      return { ...state, status: 'retrying', retryAttempt: action.attempt }
    case 'connection.closed':
      return { ...state, status: 'closed' }
    case 'event': {
      const ev = action.event
      // STATE_SNAPSHOT replaces running_now wholesale. Per the
      // connectivity-guide pin: lists get replaced, never patched.
      if (ev.type === 'STATE_SNAPSHOT') {
        const running = ev.snapshot?.running_now ?? state.running
        return {
          ...state,
          running,
          lastEventAt: ev.ts,
          retryAttempt: 0,
        }
      }
      // RUN_STARTED / RUN_FINISHED are reserved for future per-task
      // progress updates. Slice-1 ignores them but still bumps
      // `lastEventAt` so the UI knows the stream is live.
      if (ev.type === 'RUN_STARTED' || ev.type === 'RUN_FINISHED') {
        return { ...state, lastEventAt: ev.ts }
      }
      return state
    }
    default:
      return state
  }
}
