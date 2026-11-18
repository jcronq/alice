// Fetch-based SSE reader for the AG-UI event stream.
//
// Why fetch, not EventSource: per Jason's connectivity-guide pin on
// #639, EventSource can't POST and can't tell you when the TCP
// connection died (it just silently retries). A fetch+ReadableStream
// reader can share a client with command POSTs and surfaces socket
// errors as rejected promises, which lets us back off deterministically.
//
// Wire format: one event per `data: <JSON>\n\n` frame, with the
// `type` carried inside the JSON. `:` comment lines (heartbeats) are
// discarded. This matches the server-side `encode_event` in
// `_sse.py` / `_ag_ui.py`.

import type { AgUiEvent } from './agui'

export type SseOpenOptions = {
  /** Full URL including `?section=running_now` and any auth `?k=` query. */
  url: string
  /** Called once per decoded event. */
  onEvent: (event: AgUiEvent) => void
  /**
   * Optional abort signal. When fired, the reader cancels the
   * underlying fetch and the promise resolves normally. The reader
   * does NOT reject on abort — it treats it as an orderly shutdown.
   */
  signal?: AbortSignal
}

/**
 * Open an SSE connection to `url`, decode AG-UI events, and hand them
 * to `onEvent`. Returns when the server closes the connection or the
 * abort signal fires. Rejects on network / HTTP error — the caller
 * owns reconnect policy.
 */
export async function openEventStream({
  url,
  onEvent,
  signal,
}: SseOpenOptions): Promise<void> {
  const resp = await fetch(url, {
    method: 'GET',
    credentials: 'same-origin',
    headers: { Accept: 'text/event-stream' },
    signal,
  })
  if (!resp.ok) {
    throw new Error(`SSE open failed: ${resp.status} ${resp.statusText}`)
  }
  if (!resp.body) {
    throw new Error('SSE open failed: no response body')
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buf = ''

  try {
    // Loop until the server closes or abort fires.
    // The stream contract: frames end with a blank line (`\n\n`). A
    // frame may contain multiple `data:` lines and any number of `:`
    // comment lines (heartbeat). We split on the double-newline and
    // process one frame at a time.
    while (true) {
      const { value, done } = await reader.read()
      if (done) return
      buf += decoder.decode(value, { stream: true })

      // Normalize CRLF to LF so frames match on either line ending.
      buf = buf.replace(/\r\n/g, '\n').replace(/\r/g, '\n')

      let sep: number
      while ((sep = buf.indexOf('\n\n')) !== -1) {
        const frame = buf.slice(0, sep)
        buf = buf.slice(sep + 2)
        const evt = decodeFrame(frame)
        if (evt) onEvent(evt)
      }
    }
  } catch (err) {
    if ((err as DOMException | undefined)?.name === 'AbortError') {
      return
    }
    throw err
  }
}

/**
 * Decode one SSE frame into an AG-UI event. Returns `null` for
 * heartbeat-only frames or frames we couldn't parse as JSON.
 *
 * Exported for unit tests — the frame parser is the trickiest piece
 * of the client and the bit we want pinned by tests.
 */
export function decodeFrame(frame: string): AgUiEvent | null {
  const lines = frame.split('\n')
  const dataLines: string[] = []
  for (const raw of lines) {
    const line = raw.startsWith('﻿') ? raw.slice(1) : raw
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^ /, ''))
    }
    // Ignore `event:` / `id:` / `retry:` — we encode the type inside
    // the JSON payload per the AG-UI wire format and don't use them.
  }
  if (dataLines.length === 0) return null
  const text = dataLines.join('\n')
  try {
    return JSON.parse(text) as AgUiEvent
  } catch {
    // Dropping a malformed frame is the right move here: a bad frame
    // should never kill the whole stream. The caller's reconnect
    // policy will cover a torrent of bad frames.
    return null
  }
}
