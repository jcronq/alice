// Tiny reactive store — no Redux. One subscriber set + a snapshot
// getter, consumed via ``useSyncExternalStore``. The connectivity
// guide calls for "small stores"; this is the smallest thing that
// gives React a stable snapshot per render.

import type { SseEvent } from "./sse";

export type SectionKey =
  | "running_now"
  | "ready_for_review"
  | "waiting_on_you"
  | "alice_queue"
  | "done_today"
  | "known_issues";

export const SECTION_ORDER: SectionKey[] = [
  "running_now",
  "ready_for_review",
  "waiting_on_you",
  "alice_queue",
  "done_today",
  "known_issues",
];

export const SECTION_LABELS: Record<SectionKey, string> = {
  running_now: "Running now",
  ready_for_review: "Ready for Review",
  waiting_on_you: "Waiting on you",
  alice_queue: "Alice's queue",
  done_today: "Done today",
  known_issues: "Known issues I'm on",
};

export type ConnState = "connecting" | "open" | "retrying" | "closed";

export interface StatusState {
  conn: ConnState;
  // Markdown strings, one per section key. Empty string means "no
  // content yet" (which is also the server's pre-first-poll shape).
  sections: Record<SectionKey, string>;
  // Last update per section, used in the dev tooltip — not rendered
  // in slice-1.
  updatedAt: Record<SectionKey, number>;
}

const EMPTY_SECTIONS: Record<SectionKey, string> = {
  running_now: "",
  ready_for_review: "",
  waiting_on_you: "",
  alice_queue: "",
  done_today: "",
  known_issues: "",
};

const EMPTY_UPDATED_AT: Record<SectionKey, number> = {
  running_now: 0,
  ready_for_review: 0,
  waiting_on_you: 0,
  alice_queue: 0,
  done_today: 0,
  known_issues: 0,
};

function isSectionKey(key: unknown): key is SectionKey {
  return (
    typeof key === "string" && (SECTION_ORDER as readonly string[]).includes(key)
  );
}

export class StatusStore {
  private state: StatusState = {
    conn: "connecting",
    sections: { ...EMPTY_SECTIONS },
    updatedAt: { ...EMPTY_UPDATED_AT },
  };
  private listeners: Set<() => void> = new Set();

  getSnapshot = (): StatusState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private notify(): void {
    for (const l of this.listeners) l();
  }

  setConn(conn: ConnState): void {
    if (this.state.conn === conn) return;
    this.state = { ...this.state, conn };
    this.notify();
  }

  // Replaces every section whole; this is the connectivity guide's
  // "recover by snapshot" contract.
  applySnapshot(sections: Record<string, unknown>): void {
    const next: Record<SectionKey, string> = { ...this.state.sections };
    const now = Date.now();
    const updated: Record<SectionKey, number> = { ...this.state.updatedAt };
    for (const k of SECTION_ORDER) {
      const v = sections[k];
      if (typeof v === "string") {
        if (next[k] !== v) {
          next[k] = v;
          updated[k] = now;
        }
      }
    }
    this.state = { ...this.state, sections: next, updatedAt: updated };
    this.notify();
  }

  applySectionUpdate(key: unknown, content: unknown): void {
    if (!isSectionKey(key)) return;
    if (typeof content !== "string") return;
    if (this.state.sections[key] === content) return;
    this.state = {
      ...this.state,
      sections: { ...this.state.sections, [key]: content },
      updatedAt: { ...this.state.updatedAt, [key]: Date.now() },
    };
    this.notify();
  }

  // Convenience entry point for the SSE client.
  handleEvent(evt: SseEvent): void {
    switch (evt.type) {
      case "snapshot": {
        const sections = (evt.sections ?? {}) as Record<string, unknown>;
        this.applySnapshot(sections);
        break;
      }
      case "section.update": {
        this.applySectionUpdate(evt.key, evt.content);
        break;
      }
      default:
        // Unknown types are ignored — a forward-compatible protocol
        // contract (the client never crashes on new event names).
        break;
    }
  }
}

export const store = new StatusStore();
