# Slice: presence-aware Signal notifications for status-chat replies

Status: shipped 2026-11-16 (feat/presence-aware-notifs → master)

## Problem

Jason sends a message on the status-site Chat tab, Alice replies, and
Signal also pings. When Jason is sitting on the status-site tab with it
focused, the Signal ping is pure noise — he already sees Alice's reply
in-app. We want to skip the Signal ping in that specific case while
leaving every other outbound path untouched.

## Solution

Two pieces, loosely coupled over HTTP:

1. **Status site owns presence state.** The browser tab heartbeats
   `POST /api/presence` at ~20 s cadence while
   `document.visibilityState === "visible"` AND `document.hasFocus()`,
   and on every `visibilitychange` / `focus` / `blur` transition. The
   server stamps `last_presence_at = time.time()` (monotonic-ish wall
   clock; a sub-second skew doesn't matter at 45 s resolution). A fresh
   `GET /api/presence/status` returns
   `{focused: bool, focused_within_s: float, last_presence_at: iso|null}`
   where `focused` is `focused_within_s < PRESENCE_WINDOW_S` (45 s).
2. **Alice-speaking queries `/api/presence/status` just before Signal
   dispatch, only for status-chat turns.** When the current turn's
   surface is `type: status-chat` AND the outbound transport is
   `signal` AND presence reports focused, `_send_message` logs
   `signal_suppressed_presence` and returns without calling the
   `SignalTransport`. All other turn kinds and all other transports
   are unchanged.

### Suppression policy (explicit)

| Turn origin                     | Signal ping | Rationale                                   |
|---------------------------------|-------------|---------------------------------------------|
| status-chat surface, focused    | SUPPRESSED  | Jason sees the in-app reply in real time    |
| status-chat surface, not focused| sent        | Tab backgrounded / blurred / closed         |
| signal inbound reply            | sent        | The user is on Signal, that IS the channel  |
| signal-originated surface reply | sent        | Same                                        |
| cli / a2a / discord / gmail     | sent        | Other transports, untouched                 |
| emergency                       | sent        | Emergencies always bypass everything        |

The Signal → Signal path (someone texts Alice, Alice replies on Signal)
is governed by `_current_turn_kind == "signal"`, not the surface
mechanism, so it is NEVER affected by this slice.

### Endpoint shapes

```
POST /api/presence
     body: {"key": "<shared>", "focused": bool}
     returns 200 {"ok": true, "focused_within_s": 0}
     - shared-key auth, same pattern as every other POST
     - only `focused=true` updates `last_presence_at`;
       `focused=false` is logged but does not advance the stamp
       (so a stale `focused=true` can still age out naturally)

GET  /api/presence/status
     returns 200 {"focused": bool, "focused_within_s": float|null,
                  "last_presence_at": iso|null, "window_s": 45}
     - no auth — read-only, no PII, status-site is LAN-only anyway
     - `focused_within_s` is null when there's never been a heartbeat
```

### Failure modes (fail OPEN)

The presence check is a nice-to-have, not a guarantee. If any of the
following happens, Alice sends the Signal ping normally:

- `/api/presence/status` returns non-200
- network error / DNS / connection refused
- JSON parse error
- timeout (1.0 s hard cap)
- status-site unreachable

Every failure logs `signal_suppress_check_failed` with the reason so
we can spot a broken endpoint without losing pings in the meantime.

### Why not use SSE / websocket?

Simpler to bolt a polling heartbeat on than wire presence into the
broadcaster; the 45 s window is wide enough that a 20 s poll + 1 s
HTTP check at send-time is accurate without special-casing network
blips.

### Why 45 s?

20 s client poll + jitter + one skipped-tick tolerance ≈ 45 s. Tighter
(e.g., 30 s) false-negatives when the laptop briefly sleeps; wider
(e.g., 60 s) risks suppressing a ping Jason wants after he walked
away.

## Implementation

- `src/status_site/server.py` — new module-level `PresenceState`
  (thread-safe, seconds precision), two handler funcs, routes wired
  into `_get` and `do_POST`.
- `src/status_site/static/index.html` — small `presence.js`-style
  block that heartbeats and reacts to visibility transitions.
- `src/alice_speaking/infra/presence.py` — new
  `check_presence_focused(url, timeout=1.0)` helper (stdlib
  urllib). Fails open; never raises.
- `src/alice_speaking/daemon.py` — new `_current_turn_is_status_chat`
  attribute; `_send_message` consults it + presence check.
- `src/alice_speaking/_dispatch.py` — `handle_surface` sets the flag
  on chat turns and resets it in `finally`.

## Reload

Pure-Python changes in both the status-site code and the alice
container. After merge:

- `ssh cronqj@aimax1 "cd ~/dev/alice/runtime && git pull"`
- `ssh cronqj@aimax1 "cd ~/dev/alice/runtime/sandbox && docker compose up -d --no-deps --force-recreate status-site"` to pick up status-site changes (bind-mount, but recreate flushes handlers).
- `ssh cronqj@aimax1 "echo '{\"type\":\"hot\"}' > /state/worker/reload-requested"` inside the alice container's volume (watcher re-execs speaking in ~10 s).

Verify: `curl -s http://10.20.30.177:8095/api/presence/status` returns valid JSON.

## Follow-ups

- Optional in-app indicator next to the status-chat message: "(sent only here)". Not shipped in this slice — left as a UX follow-up because it needs frontend signal that the Signal ping was suppressed, which the server doesn't currently plumb back to the browser.
- If the fail-open rate spikes, consider a brief in-memory "status-site last OK" cache on alice-speaking so a transient blip doesn't flap through the whole log.
