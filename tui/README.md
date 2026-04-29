# alice-tui

Pretty TUI for talking to Alice. Companion to `bin/alice-client` (the
agent-friendly minimalist).

## Architecture

The TUI is a host-side Node app. It spawns
`docker exec -i alice-worker-blue alice-client --json` as a child process
and renders the newline-delimited JSON event stream as a chat-style
interface. No changes to the worker image, no daemon-side socket
plumbing — the existing CLI protocol is fully reused.

```
┌──────────────┐    docker exec -i    ┌────────────────────┐
│  alice-tui   │  ───────────────────▶│  alice-client      │
│  (host node) │  ◀───────────────────│  (in worker)       │
│              │      NDJSON          │  ↕ /tmp/alice.sock │
└──────────────┘                      └────────────────────┘
```

## Run

```bash
~/dev/alice/bin/alice-tui                # default: alice-worker-blue
~/dev/alice/bin/alice-tui --worker alice-worker-green
~/dev/alice/bin/alice-tui --help
```

The wrapper script handles `npm install` + `tsc` lazily on first run.
Subsequent launches are instant.

## Slash commands

| Command         | Effect                            |
| --------------- | --------------------------------- |
| `/help`         | Print help into the scrollback    |
| `/clear`        | Clear scrollback                  |
| `/quit`, `/exit`| Exit (Ctrl-C also works)          |

## Keys

| Key             | Effect                            |
| --------------- | --------------------------------- |
| `Enter`         | Send                              |
| `↑` / `↓`       | History (when input is empty)     |
| `Ctrl-L`        | Clear scrollback                  |
| `Ctrl-C`        | Quit                              |

## Layout

- **Header** — banner with quick-key reminders.
- **Conversation** — scrollback. Settled messages render once via Ink's
  `<Static>` (terminal scrollback preserved). The in-flight Alice
  message re-renders in place as chunks arrive, then migrates into
  Static when `done` lands.
- **Input bar** — bottom-anchored. Disabled while Alice is mid-turn.
- **Status line** — connecting / ready / thinking / streaming / error.

## Tool-use indicators + cost/duration footer

These rely on the daemon emitting `tool_use` and `result` events to the
CLI socket — wired via `CLITraceHandler` in
`src/alice_speaking/handlers.py`. Without that handler installed, the
TUI still works fine, those rows just don't appear.

## Phase 1 explicitly does not include

- Push notifications (no daemon-side route to active CLI sessions yet).
- Markdown rendering (chunks are shown as plain text — Alice's code
  blocks survive on their own indentation).
- Themes / syntax highlighting beyond basic role coloring.
- Session persistence across launches.
- Multi-conversation tabs.

## Files

```
tui/
├── package.json
├── tsconfig.json
├── README.md            ← this
└── src/
    ├── index.tsx        # entry: parse argv, render
    ├── App.tsx          # composition: header, conversation, input, status
    ├── types.ts         # WireEvent, Message, Status
    ├── hooks/
    │   └── useAliceClient.ts   # subprocess + NDJSON → React state
    └── components/
        ├── Conversation.tsx    # Static-backed scrollback + live message
        ├── Message.tsx         # role-driven row renderer
        ├── InputBar.tsx        # bottom prompt + history
        └── StatusLine.tsx      # bottom strip
```
