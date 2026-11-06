import { useEffect, useSyncExternalStore } from "react";
import { Section } from "./components/Section";
import { connectSse } from "./lib/sse";
import { SECTION_LABELS, SECTION_ORDER, store } from "./lib/store";

export function App(): JSX.Element {
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot);

  useEffect(() => {
    const conn = connectSse(
      "/api/events",
      {
        onStatus: (s) => store.setConn(s),
        onMessage: (evt) => store.handleEvent(evt),
      },
      {
        // The server honours ``AGENT_UI_HEARTBEAT_MS`` and defaults to
        // 15 s. The client cross-checks with a 3× idle timer; default
        // 45 s. Matches the connectivity guide.
        heartbeatMs: 15_000,
      },
    );
    return () => conn.disconnect();
  }, []);

  return (
    <main>
      <header className="app-header">
        <h1>Alice · status</h1>
        <span className="conn" data-state={state.conn}>
          {state.conn === "open"
            ? "live"
            : state.conn === "connecting"
              ? "connecting…"
              : state.conn === "retrying"
                ? "reconnecting…"
                : "offline"}
        </span>
      </header>

      {SECTION_ORDER.map((key) => (
        <Section
          key={key}
          label={SECTION_LABELS[key]}
          markdown={state.sections[key]}
        />
      ))}

      <footer className="fallback-link">
        <a href="/">legacy markdown view</a>
      </footer>
    </main>
  );
}
