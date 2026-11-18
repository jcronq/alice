import { useEffect, useReducer, useRef } from 'react'
import { openEventStream } from './sseClient'
import {
  initialState,
  runningNowReducer,
  type ConnectionStatus,
} from './runningNowReducer'
import type { RunningNowRow } from './agui'

// Max reconnect delay — keeps a failing server from thrashing us into
// a tight loop while still recovering fast enough once the stream
// comes back.
const MAX_BACKOFF_MS = 10_000

export type RunningNowPanelProps = {
  /** URL for the AG-UI event stream (section-scoped). Includes any
   * `?k=<key>` query param the host site uses for shared-secret auth. */
  url: string
  /** Optional initial state override for tests. Production uses the
   * built-in empty-row initial state. */
  initial?: typeof initialState
}

export function RunningNowPanel({ url, initial }: RunningNowPanelProps) {
  const [state, dispatch] = useReducer(
    runningNowReducer,
    initial ?? initialState,
  )
  // React 18 StrictMode double-mounts effects; `aliveRef` keeps the
  // retry loop from double-running.
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true
    const ctrl = new AbortController()

    let attempt = 0
    const run = async () => {
      while (aliveRef.current) {
        try {
          dispatch(attempt === 0
            ? { kind: 'connection.open' }
            : { kind: 'connection.retry', attempt })
          await openEventStream({
            url,
            signal: ctrl.signal,
            onEvent: (ev) => dispatch({ kind: 'event', event: ev }),
          })
          // Clean stream close (server shutdown) — exit the retry loop.
          if (!aliveRef.current) return
          dispatch({ kind: 'connection.closed' })
          return
        } catch (err) {
          if (!aliveRef.current) return
          attempt += 1
          const delay = Math.min(500 * 2 ** (attempt - 1), MAX_BACKOFF_MS)
          await sleep(delay, ctrl.signal)
        }
      }
    }
    void run()

    return () => {
      aliveRef.current = false
      ctrl.abort()
    }
  }, [url])

  return (
    <section data-testid="running-now-panel">
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          marginBottom: 12,
        }}
      >
        <h2 style={{ margin: 0 }}>Running now</h2>
        <StatusPill status={state.status} />
        {state.lastEventAt && (
          <span style={{ fontSize: 12, opacity: 0.7 }}>
            last event · {state.lastEventAt}
          </span>
        )}
      </header>
      <RowList rows={state.running.rows} />
    </section>
  )
}

function StatusPill({ status }: { status: ConnectionStatus }) {
  const color =
    status === 'open'
      ? '#3fb950'
      : status === 'retrying'
        ? '#d29922'
        : status === 'closed'
          ? '#f85149'
          : '#8b949e'
  return (
    <span
      data-testid="status-pill"
      data-status={status}
      style={{
        padding: '2px 8px',
        borderRadius: 999,
        background: color,
        color: '#0f1419',
        fontSize: 12,
        fontWeight: 600,
      }}
    >
      {status}
    </span>
  )
}

function RowList({ rows }: { rows: RunningNowRow[] }) {
  if (rows.length === 0) {
    return (
      <p data-testid="empty-state" style={{ opacity: 0.7 }}>
        No active builds.
      </p>
    )
  }
  return (
    <ul
      data-testid="row-list"
      style={{ listStyle: 'none', padding: 0, margin: 0 }}
    >
      {rows.map((r) => (
        <li
          key={r.branch || r.worktree}
          data-testid="row"
          style={{
            padding: '8px 0',
            borderBottom: '1px solid #30363d',
            display: 'flex',
            gap: 12,
            alignItems: 'baseline',
          }}
        >
          <span style={{ fontFamily: 'monospace' }}>{r.branch}</span>
          <span style={{ opacity: 0.6, fontSize: 12 }}>{r.elapsed}</span>
          <span
            style={{
              opacity: 0.8,
              fontSize: 12,
              color: r.alive ? '#3fb950' : '#8b949e',
            }}
          >
            {r.alive ? '● alive' : '○ idle'}
          </span>
          {r.pr_url && (
            <a href={r.pr_url} target="_blank" rel="noreferrer">
              PR
            </a>
          )}
          {r.issue && (
            <span style={{ opacity: 0.6, fontSize: 12 }}>#{r.issue}</span>
          )}
          {r.task_status && (
            <span style={{ opacity: 0.6, fontSize: 12 }}>
              {r.task_status}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve()
    const t = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(t)
      signal.removeEventListener('abort', onAbort)
      resolve()
    }
    signal.addEventListener('abort', onAbort)
  })
}
