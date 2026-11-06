// Fetch-based SSE client — the connectivity guide pattern (not
// EventSource). Supports:
//
// * Incremental parsing of ``data:`` frames (handles CR, LF, CRLF; a
//   trailing \r is held back between chunks).
// * BOM strip on the first chunk.
// * ``:`` comment lines ignored (so the server's heartbeats don't
//   surface as empty messages).
// * Multi-line ``data:`` fields (joined with ``\n``).
// * Idle timeout = 3 × heartbeat (45 s default) — a silent dead
//   connection is cancelled by the client and the reconnect loop kicks
//   in.
// * Exponential backoff reconnect from 500 ms up to 10 s, reset on
//   open.
//
// Slice-1 scope: single stream per tab. The per-browser
// Web-Locks-plus-BroadcastChannel sharing pattern is a follow-up slice.

export interface SseEvent {
  type: "snapshot" | "section.update" | string;
  // Any other fields come in from the server payload. We stay
  // deliberately permissive — the ``marked`` render on the client
  // only cares about ``sections`` / ``content``.
  [k: string]: unknown;
}

export interface SseHandlers {
  onOpen?: () => void;
  onMessage: (evt: SseEvent) => void;
  onError?: (err: unknown) => void;
  onStatus?: (state: "connecting" | "open" | "retrying" | "closed") => void;
}

export interface SseOptions {
  heartbeatMs?: number; // Used only to compute the idle timeout.
  minBackoffMs?: number;
  maxBackoffMs?: number;
}

const DEFAULT_HEARTBEAT_MS = 15_000;
const DEFAULT_MIN_BACKOFF = 500;
const DEFAULT_MAX_BACKOFF = 10_000;

export class SseParser {
  // Buffer carries bytes that may be the start of the next line (in
  // particular a trailing \r when the chunk boundary falls mid-CRLF).
  private buf = "";
  private bomStripped = false;
  // Current frame's data lines.
  private dataLines: string[] = [];

  feed(chunk: string, onFrame: (payload: string) => void): void {
    let text = chunk;
    if (!this.bomStripped && text.length > 0) {
      if (text.charCodeAt(0) === 0xfeff) {
        text = text.slice(1);
      }
      this.bomStripped = true;
    }
    this.buf += text;
    // Normalise CRLF/CR to LF; any trailing lone \r stays in buf for
    // the next chunk.
    this.buf = this.buf.replace(/\r\n/g, "\n");
    if (this.buf.endsWith("\r")) {
      // Hold back the \r; it might be the first half of a CRLF.
      const withoutTrailingCr = this.buf.slice(0, -1).replace(/\r/g, "\n");
      this.buf = withoutTrailingCr + "\r";
    } else {
      this.buf = this.buf.replace(/\r/g, "\n");
    }
    const parts = this.buf.split("\n");
    // Keep the last partial line (could be \r hold-back or an
    // incomplete line) in buf; dispatch everything before it.
    this.buf = parts.pop() ?? "";
    for (const line of parts) {
      if (line === "") {
        // Blank line → end of frame.
        if (this.dataLines.length > 0) {
          onFrame(this.dataLines.join("\n"));
          this.dataLines = [];
        }
        continue;
      }
      if (line.startsWith(":")) {
        // Comment (heartbeat). Ignore.
        continue;
      }
      if (line.startsWith("data:")) {
        // Per spec, strip one leading space.
        let val = line.slice("data:".length);
        if (val.startsWith(" ")) val = val.slice(1);
        this.dataLines.push(val);
        continue;
      }
      // Any other field (``event:``, ``id:``, ``retry:``) is ignored
      // in slice-1 — the server never emits them.
    }
  }
}

export interface SseConnection {
  disconnect: () => void;
}

export function connectSse(
  url: string,
  handlers: SseHandlers,
  opts: SseOptions = {},
): SseConnection {
  const heartbeatMs = opts.heartbeatMs ?? DEFAULT_HEARTBEAT_MS;
  const minBackoff = opts.minBackoffMs ?? DEFAULT_MIN_BACKOFF;
  const maxBackoff = opts.maxBackoffMs ?? DEFAULT_MAX_BACKOFF;
  const idleMs = heartbeatMs * 3;

  let abort: AbortController | null = null;
  let stopped = false;
  let backoff = minBackoff;

  const emitStatus = (
    state: "connecting" | "open" | "retrying" | "closed",
  ): void => {
    handlers.onStatus?.(state);
  };

  const runOnce = async (): Promise<void> => {
    abort = new AbortController();
    emitStatus("connecting");
    let opened = false;
    try {
      const res = await fetch(url, {
        headers: { Accept: "text/event-stream" },
        signal: abort.signal,
        cache: "no-store",
      });
      if (!res.ok || !res.body) {
        throw new Error(`HTTP ${res.status}`);
      }
      opened = true;
      backoff = minBackoff;
      emitStatus("open");
      handlers.onOpen?.();

      const reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      const parser = new SseParser();
      let idleTimer: ReturnType<typeof setTimeout> | undefined;
      const resetIdle = (): void => {
        if (idleTimer !== undefined) clearTimeout(idleTimer);
        idleTimer = setTimeout(() => {
          // Silent connection: cancel the reader so the loop reconnects.
          try {
            reader.cancel("idle").catch(() => {});
          } catch {
            /* ignore */
          }
          abort?.abort();
        }, idleMs);
      };
      resetIdle();
      try {
        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          resetIdle();
          const text = decoder.decode(value, { stream: true });
          parser.feed(text, (payload) => {
            try {
              const evt = JSON.parse(payload) as SseEvent;
              handlers.onMessage(evt);
            } catch (err) {
              handlers.onError?.(err);
            }
          });
        }
      } finally {
        if (idleTimer !== undefined) clearTimeout(idleTimer);
      }
    } catch (err) {
      if (!stopped && opened) handlers.onError?.(err);
      if (!stopped && !opened) handlers.onError?.(err);
    }
  };

  const loop = async (): Promise<void> => {
    while (!stopped) {
      await runOnce();
      if (stopped) break;
      emitStatus("retrying");
      await new Promise((resolve) => setTimeout(resolve, backoff));
      backoff = Math.min(maxBackoff, backoff * 2);
    }
    emitStatus("closed");
  };

  loop();

  return {
    disconnect(): void {
      stopped = true;
      abort?.abort();
    },
  };
}
