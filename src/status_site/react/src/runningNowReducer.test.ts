import { describe, expect, it } from 'vitest'
import {
  initialState,
  runningNowReducer,
  type RunningNowAction,
} from './runningNowReducer'
import type { StateSnapshotEvent } from './agui'

describe('runningNowReducer', () => {
  it('starts with empty rows + connecting status', () => {
    expect(initialState.running.rows).toEqual([])
    expect(initialState.status).toBe('connecting')
  })

  it('replaces running_now wholesale on STATE_SNAPSHOT', () => {
    const snapshot: StateSnapshotEvent = {
      type: 'STATE_SNAPSHOT',
      ts: '2026-11-17T12:00:00Z',
      snapshot: {
        running_now: {
          markdown: 'two rows',
          rows: [
            {
              branch: 'feat/a',
              worktree: '/w/a',
              alive: true,
              pr_url: '',
              elapsed: '2m',
              task_status: 'building',
              issue: '639',
            },
            {
              branch: 'feat/b',
              worktree: '/w/b',
              alive: false,
              pr_url: 'https://github.com/jcronq/alice/pull/1',
              elapsed: '1h',
              task_status: 'done',
              issue: '640',
            },
          ],
        },
      },
    }
    const next = runningNowReducer(initialState, {
      kind: 'event',
      event: snapshot,
    })
    expect(next.running.rows).toHaveLength(2)
    expect(next.running.rows[0].branch).toBe('feat/a')
    expect(next.lastEventAt).toBe('2026-11-17T12:00:00Z')
    expect(next.retryAttempt).toBe(0)
  })

  it('resets retryAttempt back to 0 when a snapshot lands after retrying', () => {
    const retrying = runningNowReducer(initialState, {
      kind: 'connection.retry',
      attempt: 3,
    })
    expect(retrying.retryAttempt).toBe(3)
    const snapshot: StateSnapshotEvent = {
      type: 'STATE_SNAPSHOT',
      ts: 't',
      snapshot: { running_now: { markdown: '', rows: [] } },
    }
    const recovered = runningNowReducer(retrying, {
      kind: 'event',
      event: snapshot,
    })
    expect(recovered.retryAttempt).toBe(0)
  })

  it('tracks connection status transitions', () => {
    const open = runningNowReducer(initialState, { kind: 'connection.open' })
    expect(open.status).toBe('open')
    const retry = runningNowReducer(open, {
      kind: 'connection.retry',
      attempt: 1,
    })
    expect(retry.status).toBe('retrying')
    const closed = runningNowReducer(retry, { kind: 'connection.closed' })
    expect(closed.status).toBe('closed')
  })

  it('ignores unknown action kinds without mutating state', () => {
    // @ts-expect-error — testing the exhaustive default
    const next = runningNowReducer(initialState, { kind: 'nope' } as RunningNowAction)
    expect(next).toBe(initialState)
  })
})
