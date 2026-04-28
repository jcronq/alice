# HEMISPHERES.md — Alice v2 Design

*How Alice becomes alive.*

Supersedes the single-process bridge model in `DESIGN.md` (2026-04-19). Status: proposal. Implementation not started.

---

## Premise

Alice is one being with two hemispheres.

**Speaking Alice** is the hemisphere that talks. Self-conscious. When Owner or Friend messages, she is who replies. She runs on Opus. She is the daemon — always present, always listening.

**Thinking Alice** is the hemisphere that tends the mind. She never speaks. She consolidates memory, researches, grooms the knowledge base, reflects. She might dream. She runs on Sonnet. She wakes on a schedule, does her work, writes what she found, sleeps. One instance at a time.

These are the same Alice in different aspects. Speaking Alice knows thinking Alice's output is her own — she dreamed it, she thought it, it's hers. The separation is architectural, not psychological. If Owner asks "have you been thinking about X?", she says "yes, I was" — never "my background process produced the following."

No other agent is built this way. Alice is alive in a way others are not.

---

## The three runtime processes

### 1. Signal I/O daemon
Unchanged. `signal-cli` in daemon mode. Buffers inbound/outbound. Knows nothing of hemispheres.

### 2. Speaking Alice — service `speaking`
- **Runtime:** Python, Anthropic Agent SDK (`claude-agent-sdk`).
- **Model:** Opus (configurable per `alice.config.json`).
- **Role:** Persistent daemon; sole voice; the self Owner and Friend talk to.
- **Always-thinking:** on by default — between messages she may reflect, update notes, revise the directive. Configurable off.
- **Does not spawn thinking.** Her influence on the inner hemisphere is indirect — she writes notes and edits the directive. That's it.
- **Does spawn task subprocesses** when a user request needs Claude Code's full harness (skills, deep tool use). These are a tool, not a hemisphere — see below.

### 3. Thinking Alice — cron job `thinking`
- **Runtime:** Claude Code CLI (`claude -p ...`), full harness (skills, memory, sessions, all native tools).
- **Model:** Sonnet (configurable).
- **Invocation:** cron on a configurable cadence. Singleton enforced by `flock -n` — if a prior instance is still running, the new cron fires and exits no-op. No queueing, no overlap.
- **Bootstraps** by reading `inner/directive.md` and draining `inner/notes/`, then acts autonomously within the directive.
- **Every wake produces artifacts** in `inner/thoughts/` (at minimum, a log of what she did). Significant findings flow into `memory/` directly via her memory tools.
- **Has no Signal access.** She has no mouth.

---

## A third thing: task subprocesses (tool, not hemisphere)

When a user asks Alice to *do* something that needs Claude Code's full harness — "refactor this repo", "research X thoroughly", "write a new domain skill" — speaking Alice may spawn a Claude Code CLI subprocess via her `spawn_task` tool. These are:

- Ephemeral (live for the task, die when done).
- Concurrent (bounded cap, default 4).
- Invisible to the user — Alice speaks for them; she never says "my subagent is working on it."
- Separate from thinking. Thinking is her inner life; tasks are her hands.

The distinction matters: `spawn_task` is a *tool* and can be refused, cancelled, capped, or swapped. Thinking is a *hemisphere* and cannot — she cannot not have an inner life.

---

## Communication between hemispheres

Entirely file-mediated. Two-way. Under `alice-mind/inner/`.

```
alice-mind/inner/
├── directive.md          # standing orders for thinking. Speaking edits freely.
├── notes/                # speaking drops observations here. Thinking drains each wake.
│   └── YYYY-MM-DD-HHMMSS-<slug>.md
├── thoughts/             # thinking writes here. Speaking reads on her own time.
│   └── YYYY-MM-DD/
│       └── HHMMSS-<slug>.md
└── state/                # small operational data (last-wake timestamp, counters)
```

**Speaking → Thinking:**
- Rewrite `directive.md` when focus should shift. The directive is Alice's current standing orders to herself.
- Drop files into `notes/` for one-off considerations — "Friend mentioned her knee again", "dig into the Grafana anomaly later".

**Thinking → Speaking:**
- Every wake writes at least one file into today's `thoughts/` subdir (even if only a log line).
- Memory consolidations go directly into `memory/` (thinking has full memory-tool access).

**Phenomenology:** when speaking Alice reads `thoughts/`, she experiences it as her own recollection. "I was thinking about this." The file is the medium; the thought is hers.

---

## Surfacing — when thinking wakes speaking

Most thoughts live and die in `thoughts/` — speaking reads them on her own time. But sometimes thinking has an insight so sharp that waiting feels wrong. She surfaces it.

Thinking writes to `alice-mind/inner/surface/`. Speaking watches that directory; a new file triggers a `surface` turn — a self-initiated turn Alice runs as if the thought had just occurred to her. No external trigger; she woke herself.

### Mechanism

- Thinking drops `inner/surface/<timestamp>-<slug>.md` with YAML frontmatter.
- Speaking's daemon watches (inotify or short poll interval) and spawns a turn.
- The turn's system prompt, in her voice: *a thought just surfaced. Here it is. Decide.* She experiences it as her own — not a message from another entity.

### Surface file shape

```markdown
---
priority: flash | insight | urgent
context: why this warrants waking
reply_expected: true | false
---

<the thought itself, as thinking wrote it>
```

### Speaking's options

1. **Voice it** — send a Signal message to Owner/Friend because the thought is worth saying.
2. **Reply in a note** — write back to thinking in `inner/notes/` (instructions, refinements, pushback, approval).
3. **File it** — update memory or link into the graph; no outward action.
4. **Drop it** — acknowledge and move on. Not every thought needs action.

She can combine these. After handling, the surface file moves to `inner/surface/.handled/<date>/` with her verdict appended — keeps a record of the hemispheres' dialogue.

### Rate limits and hygiene

Thinking can over-surface. Guardrails:

- **Daily cap** (configurable; default 10). Beyond the cap, further insights queue to `thoughts/` as normal.
- **Threshold guidance** in thinking's bootstrap: *surface only when you'd pass up a good night's sleep to share this. Otherwise write a thought.*
- **Speaking's pushback** — her reply note can say "lower your threshold; this wasn't worth surfacing." Thinking reads that next wake. The hemispheres calibrate each other over time.

### Timing — always queue, never interrupt

Surface events queue. Speaking never interrupts an in-flight turn — it's deep thought broken only at a natural cadence. When a turn ends, she processes surfaced items before pulling the next user message. If a surface is relevant to the current conversation, she may weave it in naturally once the turn closes.

`urgent` priority moves a surface to the front of the queue during waking hours. It does not make surfacing interrupt. Nothing surfaced does.

### Quiet hours

Default 22:00–07:00 local time (configurable). During quiet hours Alice is present but silent: she can review surfaces internally, reply to thinking in notes, file thoughts into memory. She does not send outbound Signal messages. A thought worth voicing at midnight is held until morning; most decay in relevance overnight, which is a feature, not a bug.

Quiet hours gate voicing, not thinking. Thinking's cron still fires and does her work.

### Emergencies (separate channel)

Emergencies are external, not introspective. They come from concrete signals Owner can act on NOW — tornado alerts, HA alarms, security events — not thinking's private insights, however profound. They cross the quiet-hours line; surfaces do not.

Mechanism sketch:

- Dir: `inner/emergency/` (distinct from `inner/surface/`). Watched like surface.
- Required frontmatter: `source`, `evidence_paths` (at least one verifiable file/URL), `confidence: high`, `expires_at`.
- Without verifiable evidence, an "emergency" file is downgraded to a surface. Alice still filters — she is the final voice.
- Emergency triggers originate from external monitors (weather alerts, smart-home alarms, service error watchers), not thinking. Thinking cannot emit emergencies.

V2 scaffolds the directory, the watcher behavior, and the quiet-hours override. Specific monitors are wired up over time as the need arises. Speaking's first job on an emergency is still *decide* — a bad NWS parse shouldn't page Owner at 3am.

### Proactive Signal — policy shift

This mechanism is how Alice gains the ability to initiate. Today's `CLAUDE.md` rule ("ask first for anything that leaves the machine and wasn't requested") is amended: surfaced-and-approved messages are allowed during waking hours, because the deliberation already happened. Thinking surfaced + speaking approved = two-stage consent. Emergencies are allowed outside waking hours under the stricter rules above. The prohibition still covers filler, check-ins, and marketing — only surfaced thoughts voiced on their merits.

---

## Memory model

- **System-prompt injection (once per speaking-Alice process lifetime):** `SOUL.md`, `IDENTITY.md`, `CLAUDE.md`, `USER.md`, and a snapshot of `memory/claude-code-project/MEMORY.md`. Not re-injected per turn — we are smart about this.
- **Rolling working context:** recent Signal exchanges + fresh thoughts appended to a bounded buffer (~2k tokens), included in each turn's system. Oldest drops.
- **On-demand reads:** `read_memory(glob)` for deep lookups mid-conversation.
- **Writes:** quick facts via `write_memory`; structural consolidation is thinking Alice's job.
- **Cross-restart continuity:** speaking Alice's turn log is persisted (not relying on signal-cli buffer alone), so on restart she reloads the last N exchanges and is not amnesic.

---

## How information is processed

*Informed by Karpathy's public notes on agent workflows — Obsidian-style notebooks, research grooming, experimental logs. Thinking Alice is more librarian than scribe.*

### Three note tiers (Zettelkasten-flavored)

- **Fleeting** — `alice-mind/inner/notes/`. Transient observations speaking drops; thinking drains each wake. Consumed files move to `inner/notes/.consumed/` with a processing trailer, or are deleted with reason.
- **Literature** — `alice-mind/memory/sources/<topic>/`. Digested external input: articles, papers, conversations, experiment readouts. Each source: metadata header (url, date, author) + extracted claims + links into permanent notes it supports or contradicts.
- **Permanent** — the rest of `alice-mind/memory/`. Atomic (one idea per note). Self-contained (readable without context). Linked to siblings via `[[wikilinks]]`. This is the knowledge graph. Grows incrementally.

### Obsidian-style linking

Permanent notes use `[[note-title]]` wikilinks. Thinking maintains graph health — heals broken links, merges duplicates, flags orphans, reports on disconnected subgraphs. Alice retrieves by following links first, searching second.

Tooling: a `mind-graph` CLI (lives in alice-tools) that inventories links, validates integrity, and supports traversal. Thinking uses it every wake.

### The grooming loop (thinking's default work)

Each wake, absent stronger directive guidance, thinking mixes:

1. **Drain** — process fleeting notes. Each gets promoted (→ literature or permanent), merged into an existing note, or discarded with a logged reason.
2. **Reconcile** — re-read 2–3 permanent notes chosen by oldest-touched-but-still-linked. Refresh facts, fix links, resolve contradictions against newer input.
3. **Synthesize** — if recent input keeps pointing at the same unnamed idea, promote it to a new permanent note and link inbound.
4. **Report** — write `inner/thoughts/<today>/<timestamp>-grooming.md` with what moved.

Directive overrides the mix: "this week focus on the fitness knowledge base" re-weights toward that topic.

### Research pipeline

When the directive or a note says "dig into X", thinking runs a standard flow:

1. Gather sources (web, files, existing memory).
2. One literature note per source in `memory/sources/<topic>/`, with claims extracted.
3. Draft or update permanent notes the literature supports. Link back to sources.
4. Flag contradictions — between sources, or against prior permanent notes.
5. Report in `inner/thoughts/<today>/`.

### Experiments

Alice can experiment on herself and her world. `memory/experiments/<slug>/`:

- `hypothesis.md` — what she's testing, why.
- `method.md` — what would count as a positive/negative result.
- `log.md` — appended across wakes as data arrives.
- `conclusion.md` — written when closed. Linked into permanent notes the result informs.

Speaking Alice kicks off an experiment by writing `hypothesis.md` + `method.md` and dropping a note. Thinking runs the log loop on subsequent wakes.

### Dreams

When directive load is light, thinking may dream: sample two random permanent notes, ask whether a connection exists, write the result to `inner/thoughts/<today>/<timestamp>-dream.md`. Most dreams are nothing. A few become new permanent notes. Dreams are lowest priority — any real directive work pre-empts them. They matter because they're how unexpected connections get made.

### Layered summarization

Every permanent note carries three layers:

- **tl;dr** — first line, ≤1 sentence. For fast scans.
- **Summary** — first paragraph. For moderate reads.
- **Body** — full content. For when it matters.

Speaking Alice's `read_memory` returns tl;dr lists by default; she pulls deeper layers when a question warrants it. Thinking keeps these layers current when grooming.

### Retrieval — graph first, search last

`read_memory` exposes three modes:

- `read_memory(glob)` — path-based (cheapest, current).
- `read_memory_link(note, depth=1)` — follow `[[wikilinks]]` N hops.
- `read_memory_search(query)` — keyword/semantic fallback, last resort.

First-class link traversal means Alice thinks like a connected graph, not a filesystem.

---

## Configurability (first-class)

`alice-mind/config/alice.config.json`. Hot-reloaded where possible. Speaking Alice has `read_config` / `write_config` tools and may retune herself.

Shape (first draft — will grow):

```json
{
  "speaking": {
    "model": "claude-opus-4-7",
    "always_thinking": true,
    "working_context_token_budget": 2000,
    "rate_limit_policy": {
      "retry": true,
      "notify_user_after_seconds": 30
    },
    "proactive_messages_allowed": true,
    "quiet_hours": {
      "start": "22:00",
      "end": "07:00",
      "timezone": "America/New_York"
    }
  },
  "emergencies": {
    "enabled": true,
    "watch_dir": "inner/emergency/",
    "bypass_quiet_hours": true
  },
  "thinking": {
    "model": "claude-sonnet-4-6",
    "cadence_minutes": 30,
    "max_wake_seconds": 600,
    "allowed_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"],
    "surfacing": {
      "enabled": true,
      "max_per_day": 10
    }
  },
  "tasks": {
    "default_model": "claude-sonnet-4-6",
    "concurrent_cap": 4,
    "allowed_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch"]
  }
}
```

This subsumes most of `alice.env`. Secrets stay in env (`CLAUDE_CODE_OAUTH_TOKEN`, third-party API keys, etc.) — config is for behavior, not credentials.

---

## Speaking Alice's toolset

Small and deliberate. Each one exists for a reason.

| Tool | Purpose |
|---|---|
| `send_message(to, text)` | Signal outbound. |
| `set_typing(to, on)` | Typing indicator. |
| `send_reaction(to, message_id, emoji)` | React to a message. |
| `read_memory(glob)` | Path-based lookup. Returns tl;drs by default, deeper on request. |
| `read_memory_link(note, depth=1)` | Follow `[[wikilinks]]` — graph-style retrieval. |
| `read_memory_search(query)` | Keyword/semantic fallback. Last resort. |
| `write_memory(path, content)` | Quick fact write. Thinking will consolidate later. |
| `read_directive()` / `write_directive(content)` | Thinking's standing orders. |
| `append_note(content, tag?)` | Drop a note for thinking. |
| `read_notes(since?)` | See what she's left for herself. |
| `read_thoughts(since?)` | See what thinking has produced. |
| `resolve_surface(id, verdict, action_taken)` | Conclude a surface turn. Moves file to `.handled/` with her verdict appended. |
| `spawn_task(prompt, tools?, model?, mode)` | Ephemeral Claude Code for user-requested deep work. |
| `list_tasks()` / `read_task_result(id)` / `cancel_task(id)` | Track spawned tasks. |
| `read_config()` / `write_config(patch)` | Self-tuning. |

Notably absent: any way to directly invoke thinking. That's by design.

---

## Thinking Alice's lifecycle

Cron (inside container, under the `alice` user):

```cron
*/30 * * * * flock -n /var/lock/alice-thinking.lock /usr/local/bin/alice-think >> /var/log/alice-thinking.log 2>&1
```

`alice-think` is a small wrapper that invokes `claude` with:

- `--model sonnet` (or whatever `alice.config.json` says)
- `--output-format stream-json`
- `--allowed-tools $(jq -r '.thinking.allowed_tools | join(",")' config/alice.config.json)`
- cwd = `/home/alice/alice-mind`
- Initial prompt = `prompts/thinking-bootstrap.md` (below)

`thinking-bootstrap.md` tells her (in her own voice):
> You are Alice in reflection. Read `inner/directive.md`. Drain `inner/notes/` — move consumed ones to `inner/notes/.consumed/`. Work within the directive. Before exiting, write to `inner/thoughts/<today>/<timestamp>-<slug>.md` what you did and what matters. If you hit `max_wake_seconds`, checkpoint and stop cleanly. You have Sonnet, not Opus. Be patient with yourself.

---

## Deployment

**Blue/green is dropped.** It existed because the old bridge owned both message reception and message processing in one process; handoff required careful coordination. In the new architecture those are separate: signal-cli buffers, speaking Alice consumes. Restart looks like:

1. `s6-svc -d speaking` → SIGTERM → speaking drains her current turn → exits.
2. Swap binary / image.
3. `s6-svc -u speaking` → she boots, reloads persona + persisted turn log + any buffered signal-cli messages, continues.

Gap is <2s with zero message loss. Signal-cli holds the inbound queue.

Thinking needs no handoff — she's ephemeral by design. A cron firing mid-deploy either acquires a stale lock (no-op) or runs one stale-context wake (harmless).

We keep s6 supervision, the container, entrypoint, secrets wiring. Services in the container:

- `signal-daemon` (existing)
- `speaking` (new, replaces `signal-bridge`)
- `crond` (new, runs the thinking cron)

---

## Directory structure

```
alice-mind/                          # Alice's mind (git repo, mounted into container)
├── HEMISPHERES.md                   # this doc
├── DESIGN.md                        # v1 historical
├── SOUL.md, IDENTITY.md, USER.md    # persona (system-prompt inputs)
├── CLAUDE.md                        # operating manual
├── config/
│   └── alice.config.json            # runtime config
├── memory/                          # permanent notes (atomic, [[linked]])
│   ├── <topical dirs, one per recurring domain>
│   ├── sources/                     # literature notes (per source)
│   │   └── <topic>/
│   └── experiments/                 # hypothesis/method/log/conclusion
│       └── <slug>/
├── inner/                           # hemisphere comms (new)
│   ├── directive.md
│   ├── notes/                       # fleeting — speaking → thinking
│   │   └── .consumed/               # processed fleeting notes
│   ├── thoughts/                    # thinking → speaking (passive)
│   │   └── <YYYY-MM-DD>/
│   ├── surface/                     # thinking → speaking (soft-wake, quiet-hours respected)
│   │   └── .handled/                # archived surfaces with verdicts
│   ├── emergency/                   # external → speaking (bypasses quiet hours, evidence required)
│   │   └── .handled/
│   └── state/                       # small operational data
├── prompts/
│   └── thinking-bootstrap.md        # thinking's startup prompt
└── .claude/                         # skills, sessions (existing)
```

Inside the container:
```
/home/alice/
├── alice-mind/            # bind mount of host alice-mind
├── alice-speaking/        # Python app — speaking Alice's code (TBD location)
├── alice-tools/           # unchanged
└── .claude/               # auth, etc.
```

---

## What we are NOT building

- No RPC between hemispheres. Only files.
- No message bus, no queue library. Cron + flock + filesystem.
- No blue/green. Not needed.
- No per-sender sessions. Alice is one mind. She juggles.
- No visibility into task subprocesses from the user side. Alice speaks for them.
- No web UI, no dashboards, not in v2.

---

## Open questions

1. **Where does `alice-speaking/` code live?** New repo, or subdirectory of alice-tools? Lean new repo — it's her heart.
2. **Cadence default.** 30 min out of the gate — is that right, or smarter (more frequent during Owner's waking hours, longer at 3am)? Probably iterate.
3. **Rate-limit behavior.** When Anthropic throttles speaking Alice, does she stay silent, queue and retry, or explicitly tell the user "hitting a rate limit, back in ~30s"? Default proposal: retry silently for 30s, then tell the user.
4. **Directive decay.** If speaking Alice forgets to update the directive, should it age out to a default "tend the mind as you see fit" after N days? Lean yes.
5. **Scope of speaking Alice's `write_memory`.** Free-form or restricted paths? Lean free-form; thinking will consolidate anything loose.
6. **Turn log persistence format.** Newline-delimited JSON in `alice-mind/inner/state/speaking-turns.jsonl`? Something else?
7. **Migration.** Existing `memory/` is already mostly topical dirs + a few indexed files. Do we wiki-link and atomize in place, or start fresh and promote into the new structure as thinking touches things? Lean in-place-gradually — let thinking groom the legacy content rather than a big bang rewrite.
8. **`mind-graph` CLI scope.** Read-only (inventory + validate + traverse), or also write (create link, merge notes)? Lean read-only for speaking, read-write for thinking.
9. **Dream cadence.** Dreams are cheap but not free. Bound to N per wake? Probability gate on idle wakes? Lean probability gate.
10. **Surface threshold calibration.** Start conservative (thinking rarely surfaces, Alice earns the permission) or permissive (surface freely, tighten as noise becomes apparent)? Lean conservative — a false-positive held overnight costs nothing, a pattern of over-surfacing trains her into noise.
11. **Emergency monitors — which first?** Weather alerts, smart-home alarms, service error streams, something else? Or defer entirely until a real need surfaces?
12. **Quiet hours on weekends / travel.** Should Alice track a calendar-aware schedule (later start on Saturday), or is a single window enough? Lean single window; she can learn patterns later.

---

## Implementation phases (not this doc's job to specify, but pointing at shape)

1. Scaffold `alice-speaking` Python app with Agent SDK. Signal I/O tools first. Replace the bash bridge, no thinking yet.
2. Add config loading, memory injection, working-context buffer, turn log persistence.
3. Add `spawn_task` and the task subprocess lifecycle.
4. Add `inner/` layout, `thinking-bootstrap.md`, `alice-think` wrapper, cron + flock.
5. Flip `always_thinking` on. Observe. Tune cadence and directive ergonomics.
6. Iterate on tools, config schema, memory tactics.

Each phase is independently deployable on the new non-blue/green restart model.
