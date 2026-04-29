// Wire protocol from alice-client --json — one JSON object per line on stdout.
// Mirrors the events emitted by src/alice_speaking/transports/cli.py.

export type WireEvent =
  | { type: "chunk"; text: string }
  | { type: "tool_use"; name: string; input?: unknown }
  | { type: "ack" }
  | { type: "error"; message: string }
  | { type: "done" }
  | { type: "result"; total_cost_usd?: number; duration_ms?: number; [k: string]: unknown };

// What the TUI keeps in its scrollback. One Message per logical entry.
export type Role = "user" | "alice" | "system" | "tool" | "error";

export interface Message {
  id: string;            // monotonic, used as React key
  role: Role;
  text: string;          // accumulated display text
  toolName?: string;     // for role === "tool"
  toolInput?: Record<string, unknown>;  // single-field summary from daemon
  costUsd?: number;      // attached to alice messages on result
  durationMs?: number;
  streaming?: boolean;   // alice message still receiving chunks
}

// Connection lifecycle for the bottom status line.
export type Status =
  | { kind: "connecting" }
  | { kind: "idle" }
  | { kind: "thinking" }    // sent a turn, waiting for first chunk
  | { kind: "streaming" }   // chunks arriving
  | { kind: "error"; message: string }
  | { kind: "exited"; code: number | null };
