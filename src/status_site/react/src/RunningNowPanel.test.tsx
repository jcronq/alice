import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RunningNowPanel } from './RunningNowPanel'
import { initialState } from './runningNowReducer'

// We don't exercise the live SSE loop here — the fetch adapter is
// covered in sseClient.test.ts and the reducer in
// runningNowReducer.test.ts. The job of this test is to confirm the
// component renders the state shape the reducer produces.

describe('RunningNowPanel', () => {
  it('shows an empty-state message when no rows have arrived', () => {
    render(<RunningNowPanel url="about:blank" initial={initialState} />)
    expect(screen.getByTestId('empty-state')).toBeTruthy()
  })

  it('renders one <li> per running_now row with branch + elapsed', () => {
    const initial = {
      ...initialState,
      running: {
        markdown: 'two rows',
        rows: [
          {
            branch: 'feat/alpha',
            worktree: '/w/alpha',
            alive: true,
            pr_url: 'https://github.com/jcronq/alice/pull/99',
            elapsed: '3m',
            task_status: 'building',
            issue: '639',
          },
          {
            branch: 'feat/beta',
            worktree: '/w/beta',
            alive: false,
            pr_url: '',
            elapsed: '1h',
            task_status: '',
            issue: '',
          },
        ],
      },
    }
    render(<RunningNowPanel url="about:blank" initial={initial} />)
    const rows = screen.getAllByTestId('row')
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('feat/alpha')
    expect(rows[0].textContent).toContain('3m')
    expect(rows[1].textContent).toContain('feat/beta')
    expect(rows[1].textContent).toContain('1h')
  })

  it('surfaces the connection status pill in the header', () => {
    render(<RunningNowPanel url="about:blank" initial={initialState} />)
    const pill = screen.getByTestId('status-pill')
    expect(pill).toBeTruthy()
    // The mount-effect dispatches `connection.open` synchronously
    // during the initial render pass; snapshotting `status` here
    // picks up either the initial `connecting` label or the
    // `open`/`retrying`/`closed` label the effect produced.
    expect(['connecting', 'open', 'retrying', 'closed']).toContain(
      pill.getAttribute('data-status'),
    )
  })
})
