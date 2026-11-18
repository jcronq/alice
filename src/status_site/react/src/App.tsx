import { RunningNowPanel } from './RunningNowPanel'

// Slice-1 shell — just mounts <RunningNowPanel /> under /ui/running-now.
// Later slices will add <WaitingOnYouPanel />, <InFlightPanel />, etc.
// and a router; for now the whole page is one section.
//
// The URL carries any shared-secret `?k=<key>` the host site uses for
// auth. We just forward whatever query string is already on the
// `window.location` so the user's existing bookmark works.

export function App() {
  const url = buildEventsUrl()
  return (
    <main style={{ maxWidth: 960, margin: '0 auto' }}>
      <RunningNowPanel url={url} />
    </main>
  )
}

function buildEventsUrl(): string {
  if (typeof window === 'undefined') {
    return '/events?section=running_now'
  }
  const params = new URLSearchParams(window.location.search)
  params.set('section', 'running_now')
  return `/events?${params.toString()}`
}
