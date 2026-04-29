import { useEffect, useRef, useState, useCallback } from "react";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import type { WireEvent, Message, Status } from "../types.js";

export interface UseAliceClientOpts {
  worker: string;     // container name (alice-worker-blue)
  socket?: string;    // override for /tmp/alice.sock inside the container
}

export interface UseAliceClient {
  status: Status;
  messages: Message[];
  send: (text: string) => void;
  clear: () => void;
  // Push a system-level note into the scrollback (slash command output,
  // local errors, etc). Doesn't cross the wire.
  note: (text: string) => void;
}

// Spawns `docker exec -i WORKER alice-client --json` and translates the
// newline-delimited JSON event stream into Message objects + Status.
// Reuses the existing CLI protocol — no daemon-side changes.
export function useAliceClient(opts: UseAliceClientOpts): UseAliceClient {
  const [status, setStatus] = useState<Status>({ kind: "connecting" });
  const [messages, setMessages] = useState<Message[]>([]);
  const procRef = useRef<ChildProcessWithoutNullStreams | null>(null);
  const idCounter = useRef(0);
  const currentAliceId = useRef<string | null>(null);

  // Stable id generator — avoids collisions across renders.
  const nextId = () => `m${++idCounter.current}`;

  const spawnProc = useCallback(() => {
    // -u alice: daemon's address book maps the CLI principal to uid 501
    // (host user). docker exec defaults to root (uid 0) which the daemon
    // rejects with "unauthorized" + closes the socket — and then our
    // first send fails with EPIPE. Match what bin/alice does.
    const args = ["exec", "-i", "-u", "alice", opts.worker, "/usr/local/bin/alice-client", "--json"];
    if (opts.socket) {
      args.push("--socket", opts.socket);
    }
    const proc = spawn("docker", args, { stdio: ["pipe", "pipe", "pipe"] });
    procRef.current = proc;

    let stdoutBuf = "";
    proc.stdout.setEncoding("utf8");
    proc.stdout.on("data", (chunk: string) => {
      stdoutBuf += chunk;
      let nl: number;
      while ((nl = stdoutBuf.indexOf("\n")) !== -1) {
        const line = stdoutBuf.slice(0, nl).trim();
        stdoutBuf = stdoutBuf.slice(nl + 1);
        if (!line) continue;
        try {
          handleEvent(JSON.parse(line) as WireEvent);
        } catch {
          // Non-JSON line on stdout shouldn't happen in --json mode but
          // don't crash the TUI if it does.
        }
      }
    });

    let stderrBuf = "";
    proc.stderr.setEncoding("utf8");
    proc.stderr.on("data", (chunk: string) => {
      stderrBuf += chunk;
      // Surface stderr lines as system messages so transport errors are
      // visible (e.g. "socket not found", "cannot connect").
      let nl: number;
      while ((nl = stderrBuf.indexOf("\n")) !== -1) {
        const line = stderrBuf.slice(0, nl).trimEnd();
        stderrBuf = stderrBuf.slice(nl + 1);
        if (!line) continue;
        appendSystem(line);
      }
    });

    proc.on("spawn", () => {
      // Once spawned successfully we consider ourselves idle until the
      // first send. (alice-client connects on its first read — there's
      // no explicit handshake to wait for.)
      setStatus({ kind: "idle" });
    });

    proc.on("error", (err) => {
      setStatus({ kind: "error", message: err.message });
    });

    proc.on("exit", (code) => {
      setStatus({ kind: "exited", code });
      procRef.current = null;
    });
  }, [opts.worker, opts.socket]);

  // Helpers -----------------------------------------------------------------

  function appendSystem(text: string) {
    setMessages((prev) => [...prev, { id: nextId(), role: "system", text }]);
  }

  function handleEvent(ev: WireEvent) {
    switch (ev.type) {
      case "ack":
        // The daemon acknowledged the inbound. Move from thinking → still
        // thinking (we'll show a spinner until first chunk).
        setStatus({ kind: "thinking" });
        break;

      case "chunk": {
        const id = currentAliceId.current;
        if (id) {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === id ? { ...m, text: m.text + ev.text } : m
            )
          );
        } else {
          // First chunk of a new alice turn — create the message.
          const newId = nextId();
          currentAliceId.current = newId;
          setMessages((prev) => [
            ...prev,
            { id: newId, role: "alice", text: ev.text, streaming: true },
          ]);
        }
        setStatus({ kind: "streaming" });
        break;
      }

      case "tool_use": {
        // Settle the in-flight alice message *before* appending the tool, so
        // tools land chronologically between text segments instead of after
        // them. The next chunk (if any) creates a fresh alice message.
        //
        // This is also load-bearing for Conversation.tsx's <Static> region:
        // Ink diffs <Static> by index (not by key), so the `settled` array
        // must stay append-only. If we left m1 streaming while pushing the
        // tool, m1 would later migrate from `live` into `settled` at index N,
        // but Ink would have already consumed index N with the tool — so m1
        // would never be rendered to static and would visibly disappear when
        // it stopped streaming.
        const aliceId = currentAliceId.current;
        currentAliceId.current = null;
        setMessages((prev) => {
          const settled = aliceId
            ? prev.map((m) => (m.id === aliceId ? { ...m, streaming: false } : m))
            : prev;
          return [
            ...settled,
            {
              id: nextId(),
              role: "tool",
              text: "",
              toolName: ev.name,
              toolInput: (ev.input ?? undefined) as Record<string, unknown> | undefined,
            },
          ];
        });
        break;
      }

      case "result": {
        // Decorate the in-flight alice message with cost/duration AND
        // mark it complete. `result` is the only turn-end signal we get
        // for surface/emergency pushes (the daemon only emits `done`
        // for direct CLI turns), so without this the spinner sticks
        // forever after an ambient push.
        const id = currentAliceId.current;
        if (id) {
          const cost = typeof ev.total_cost_usd === "number" ? ev.total_cost_usd : undefined;
          const dur = typeof ev.duration_ms === "number" ? ev.duration_ms : undefined;
          setMessages((prev) =>
            prev.map((m) =>
              m.id === id
                ? { ...m, costUsd: cost, durationMs: dur, streaming: false }
                : m
            )
          );
        }
        currentAliceId.current = null;
        setStatus({ kind: "idle" });
        break;
      }

      case "error": {
        // Same settle-before-append pattern as tool_use — an in-flight alice
        // message must leave `streaming` so it migrates into <Static> in
        // chronological order, otherwise it gets stranded in `live` and
        // disappears.
        const aliceId = currentAliceId.current;
        currentAliceId.current = null;
        setMessages((prev) => {
          const settled = aliceId
            ? prev.map((m) => (m.id === aliceId ? { ...m, streaming: false } : m))
            : prev;
          return [...settled, { id: nextId(), role: "error", text: ev.message }];
        });
        setStatus({ kind: "idle" });
        break;
      }

      case "done":
        // Mark the in-flight alice message complete.
        if (currentAliceId.current) {
          const id = currentAliceId.current;
          setMessages((prev) =>
            prev.map((m) => (m.id === id ? { ...m, streaming: false } : m))
          );
        }
        currentAliceId.current = null;
        setStatus({ kind: "idle" });
        break;
    }
  }

  // Lifecycle ---------------------------------------------------------------

  useEffect(() => {
    spawnProc();
    return () => {
      const p = procRef.current;
      if (p && !p.killed) {
        p.kill("SIGTERM");
      }
    };
  }, [spawnProc]);

  // Public API --------------------------------------------------------------

  const send = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    const proc = procRef.current;
    if (!proc || proc.killed) {
      appendSystem("(not connected — alice-client subprocess is gone)");
      return;
    }
    // Echo into the scrollback first.
    setMessages((prev) => [...prev, { id: nextId(), role: "user", text: trimmed }]);
    currentAliceId.current = null;
    setStatus({ kind: "thinking" });
    proc.stdin.write(trimmed + "\n");
  }, []);

  const clear = useCallback(() => {
    setMessages([]);
  }, []);

  const note = useCallback((text: string) => {
    appendSystem(text);
  }, []);

  return { status, messages, send, clear, note };
}
