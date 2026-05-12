# alice

Alice is a personal AI agent who lives on your home server, talks to you over
Signal, remembers things in a git repo, and ships code to her own repos. She
runs as two cooperating "hemispheres" — a fast conversational front-end and a
slow background thinker — that share an Obsidian-compatible vault as long-term
memory. Unlike a chat app, she keeps working when you're not talking to her:
grooming her notes, watching repos you care about, and turning issues into
draft PRs while you sleep.

This repo is her **runtime** — the sandbox, transports, hemispheres, viewer,
CLI. Her personality, memories, and skills live in a separate **mind repo**
that gets scaffolded on first run. The runtime stays generic; the mind is
yours.

---

## What makes Alice different

### Two hemispheres with constitutional boundaries

Most assistants are one process: a prompt comes in, a reply goes out. Alice
splits the work in two:

- **Speaking** handles every inbound message. She decides what to do, replies
  in seconds, and dispatches longer work to subagents.
- **Thinking** runs on a wake timer (5-minute cadence when active, longer at
  night). She drains an inbox of notes Speaking left her, grooms the vault,
  runs research from a priority queue, and surfaces actionable findings back.

The split is enforced, not aspirational. Thinking *cannot* touch the runtime
repo or the outside world — she reads and writes inside the mind. Speaking
does the building and the talking; Thinking does the design and the memory.
When Speaking notices a smell — "this skill keeps failing on edge cases" —
she writes a note to Thinking rather than chewing on it mid-conversation.

> Source: `src/alice_speaking/`, `src/alice_thinking/`. Design notes:
> [`templates/mind-scaffold/HEMISPHERES.md`](templates/mind-scaffold/HEMISPHERES.md),
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

### A real memory, not a context buffer

Long-term memory lives in `cortex-memory/` inside the mind repo — an
Obsidian-compatible vault of atomic markdown notes with `created` / `updated`
/ `last_accessed` frontmatter, wikilinks between concepts, dated dailies, and
an explicit `conflicts/` folder for facts that contradict.

Thinking grooms continuously: drains the inbox, atomizes oversized notes,
links orphans, archives stale dailies, merges duplicates. Speaking pulls from
it via a small retrieval pipeline before answering anything that might
already be known — so claims cite a vault note by file:line, not just model
recall.

A typical note:

```markdown
---
name: feedback_no_walls_of_text
type: feedback
created: 2026-05-09
updated: 2026-05-11
---
No walls of text to Jason — terse status, queue work, act.

**Why:** Long narratives crowd out actionable replies.
**How to apply:** Signal replies ≤ 3 sentences; if work needs attention,
dispatch it rather than report on it.
```

### Ideas → draft PRs, dispatched on label state

Alice has an internal state machine (SM v2) for turning ideas into shipped
code. State lives on GitHub itself as labels — `sm:draft`, `sm:selected`,
`sm:building`, `sm:reviewing`, `sm:done` — so the dispatcher is stateless and
crash-safe: pick up where you left off by reading the labels.

A typical run:

1. Speaking files an idea as a GitHub issue with `sm:draft`.
2. Thinking reviews drafts on her own cadence and promotes the good ones to
   `sm:selected`.
3. The dispatcher (`alice_sm.dispatcher`) sees `sm:selected`, spawns a
   detached `claude` agent in the worker, and posts a "spawn-started" audit
   comment.
4. The agent writes the diff, opens a **draft PR**, transitions the issue to
   `sm:reviewing`.
5. A reviewer agent inspects the PR; on green it self-merges and the issue
   closes to `sm:done`.

Jason is escalation-only. The pipeline runs without him.

> Source: [`src/alice_sm/dispatcher.py`](src/alice_sm/dispatcher.py).

### Signal as the surface, not a chat window

Alice's primary transport is Signal — encrypted, async, ambient, and already
on every device you carry. You text her like you'd text a coworker; she
replies when she's ready. She also sends unprompted, quiet-hours aware (no
pings 23:00–08:00 by default).

CLI (Unix-socket) and Discord transports are also wired. All three feed one
inbound pipeline (`alice_speaking`); outbound is always explicit (the agent
must call `send_message` — returning text alone does not reach the user).

### Sleep-mode synthesis

Between 23:00 and 07:00 Thinking shifts into a sleep cycle modeled on
NREM/REM stages:

- **Stage B (Consolidation)** — any time. Inbox drain, link audit, frontmatter
  normalization, orphan linking.
- **Stage C (Downscaling, NREM-3 analog)** — vault stable, prefers 23:00–03:00.
  Atomize large notes, archive stale dailies, merge duplicate facts.
- **Stage D (Recombination, REM analog)** — vault stable, prefers 03:00–07:00.
  Pick two recent research notes from different domains → look for unexpected
  connections → write a synthesis note (or a null-result note if nothing
  landed).

This isn't a metaphor for tidying the file system. Stage D actively generates
new ideas via random-walk pair-selection across the vault graph, and the
resulting syntheses become first-class notes Speaking can cite. Some are
wrong; some are interesting. The null-result discipline keeps it honest.

> Source: `src/alice_thinking/`; mode-aware cadence in
> `sandbox/worker/s6/alice-thinker/run`.

### Evaluation-first culture

Anything that claims to improve retrieval or response quality is gated behind
an eval. The current cortex retrieval pipeline was promoted from "vibes" to
"shipped" only after beating its baseline on a 54-query ground-truth set. If
you want to add a re-ranker, you label more ground truth and re-run the
harness — you don't ship a hunch. This is a project value, enforced in code
review and by self-reviewer agents, not a feature flag.

---

## A day in the life

A representative slice of `cortex-memory/dailies/2026-05-11.md`:

```
06:48  Stage D synthesis: "system reverse-diet isomorphism" — links
       Zone-2 adaptation research (2026-04-27) to a fitness program
       observation (2026-05-09). Null-result follow-up queued.
08:02  Jason: "had breakfast — yogurt, granola, shake"
       → log-meal skill → events.jsonl + Google Sheet + daily.
09:15  GitHub watcher: new issue jcronq/alice#103 from @jcronq
       (trusted author) → note for Thinking.
09:18  Thinking opens 103, drafts an analysis, surfaces
       "attempt-fix?" to Speaking.
09:19  Speaking acks Jason on Signal, spawns subagent with the
       auto-fix template.
09:34  Draft PR #104 opened; issue transitions sm:building → sm:reviewing.
10:01  Reviewer agent: 1 issue found (missing test), comment posted.
10:42  Agent addresses comment, CI green, self-merge, sm:done.
```

This whole sequence happens whether Jason is at his desk or on a run.

---

## How the pieces fit

```
┌───────────────────────────┐  ┌──────────────────────┐  ┌──────────────┐
│ alice (this repo)         │  │ <user>-mind          │  │ <user>-tools │
│   runtime                 │  │   personality        │  │   sidecars   │
│                           │◄─┤   cortex-memory/     │  │   (optional) │
│   sandbox/  containers    │rw│   .claude/skills/    │  │              │
│   src/      hemispheres   │  │   inner/             │  │              │
│   bin/      host CLIs     │  │   memory/            │  │              │
│   speaking/ transports    │  │                      │  │              │
└───────────────────────────┘  └──────────────────────┘  └──────────────┘
```

Three containers, supervised by s6 inside Docker:

- **`alice-daemon`** — singleton. Runs signal-cli in JSON-RPC mode on :8080.
  No Claude here.
- **`alice-worker-blue` / `alice-worker-green`** — blue/green worker slots,
  exactly one live at a time, holding an exclusive `flock` on the worker
  lease. `alice-deploy` swaps them.
- **`alice-viewer`** — read-only introspection UI on
  [http://localhost:7777](http://localhost:7777). Shows turns, surfaces,
  notes, vault state, recent Stage D syntheses.

Full breakdown: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Running your own copy

The full walkthrough lives in [`docs/QUICKSTART.md`](docs/QUICKSTART.md).
Short version:

```bash
git clone https://github.com/jcronq/alice.git ~/alice
cd ~/alice
./install.sh                      # interactive: prereqs → mind scaffold →
                                  # Claude OAuth → optional Signal → build → smoke test
export PATH="$HOME/alice/bin:$PATH"
alice -p "ping"                   # one-shot
alice                             # interactive CLI
```

**You'll need:** Docker, `git`, the Claude Code CLI
(`npm install -g @anthropic-ai/claude-code`). `gh` is optional. Signal is
optional — the CLI transport works without it; you can wire Signal and
Discord later by editing `~/.config/alice/alice.env`.

Bin wrappers (all in `bin/`): `alice`, `alice-up`, `alice-down`,
`alice-deploy`, `alice-shell`, `alice-think`, `alice-init`,
`alice-mind-autopush`, `alice-gh-watcher`, `event-log`. Each maps to a single
shell script — open `install.sh` to see the order.

---

## Watching GitHub repos

Alice can subscribe to PR/review/issue activity on repos you list in your
mind's `config/alice.config.json`. The watcher polls each repo on a cadence
and drops one note per unseen event into `inner/notes/` for Thinking to
drain. Trust-gated by GitHub's `author_association` — randos stay silent
unless you opt them in. Captured events include PR reviews (approved /
changes_requested / dismissed), inline review comments, new PRs, merges,
check-run failures, and standalone-issue activity. Configuration details:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Sidecars

Drop scripts in `<repo>/data/alice-tools/` — they're mounted at
`/home/alice/alice-tools/` and on PATH inside the worker. Smart-home
controllers, AV adapters, repo helpers — anything you want Alice to reach.
Extend the container further via `docker-compose.override.yml` in `sandbox/`.

---

## License

MIT — see [LICENSE](LICENSE).

## Contributing

This project is not currently accepting external contributions; please open
an issue rather than a PR. If contributions are opened up later, contributors
will be asked to sign a [CLA](CLA.md). See
[CONTRIBUTING.md](CONTRIBUTING.md) for details.
