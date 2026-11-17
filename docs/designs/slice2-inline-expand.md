# Slice 2 — Waiting-on-you inline expand + Accept/Reject/Steer

Slice 1 (#881) put a side-sheet modal in front of every WoY item so Jason
could see the thread without leaving Overview. The modal works, but it
occludes the rest of the list and the Accept/Reject/Steer verbs Jason
wanted sit uneasily inside a dialog. Slice 2 replaces the modal with an
**inline expand** and surfaces the one-click verbs directly on the row.

## User flow

1. Overview renders Waiting-on-you as it does today (hand-zone markdown
   first, structured asks below). The hand-zone bullets each carry a
   dotted-underline bold title.
2. Jason clicks a title → the row **expands in place** with a timeline,
   a Steer input, a Reply composer, and Accept / Reject buttons. The
   rest of the list stays visible above and below the expanded block.
3. Clicking the title again (or an explicit "Hide" affordance on the
   expanded panel) collapses the row.
4. Items the server marks as **needs-approval** render under a
   "Needs your approval" subheader at the top of the WoY list, with
   Accept and Reject buttons visible on the *collapsed* row — Jason
   doesn't have to open the item to decide.
5. Accept on a PR → `gh pr merge --squash` server-side. Accept on a
   non-PR → thread post + dismissal. Reject → thread post + dismissal.
   Steer → appends a steering note to the thread (and, when the item
   has an in-flight bg-task, dispatches to that task; the surface
   wiring is on the alice-mind side and out of this slice's scope).
6. Dismissed items show the buttons grayed out with
   "Accepted Ns ago" / "Rejected Ns ago" until the next Tracker tick
   (10s), when the row drops off the WoY list.

## Implementation approach: DOM-portal (option a)

**Why not React rows (option b):** the hand-zone is Jason's authored
markdown in `inner/status/waiting-on-you.md`. Converting it to
structured rows would force every future Alice content pipeline to
mirror that shape; it also loses rich markdown (bold/italic/links
inside the title and continuation paragraphs) that Jason uses for
context. The structured asks list is already React, and the two
coexist today — making them look identical is a UX job, not a data-model job.

**The portal.** `MarkdownZone` renders the hand-zone HTML into a
container `<div class="woy-click">`. After render, the React
`WaitingTable` component tracks an `activeSlug` state. When Jason
clicks a title, we portal a `<ExpandedCard>` component into a
dedicated `<div class="woy-expand">` that we inject immediately after
the clicked `<li>` with a small DOM-ref trick (`createPortal` with a
dynamically mounted container). On collapse we unmount the portal and
remove the injected `<div>`. No modal, no scrim, no layout shift
beyond the expand itself.

**Needs-your-approval split.** `_render_waiting_on_you` now returns
`needs_approval_slugs: list[str]` alongside `markdown` / `rows`.
After `MarkdownZone` renders the hand-zone, a `useLayoutEffect` walks
the rendered `<li>`s; any whose bold-title slug is in
`needs_approval_slugs` is moved (by appending the same `<li>` node)
into a sibling `<ul class="woy-needs-approval">` that we prepend with
an `<h4>Needs your approval</h4>` label. The move is a cheap DOM
op and keeps React's virtual tree in sync because the moved nodes
belong to the inner HTML blob, not React itself. Those rows also
render a pair of `<button>` elements (via the same `useLayoutEffect`
appending into each row) so Accept / Reject are reachable without
expanding.

The predicate that produces `needs_approval` today is conservative:
an item is "needs approval" iff the server can detect a PR URL in its
title (`_detect_pr` returns non-None) **and** the item has no
pre-existing dismissal in `.woy-dismissed.jsonl`. This covers the
common case Jason flagged (open draft PRs waiting for his
"merge / nope") without burning bridges for non-PR workflows; the
predicate is easy to extend later with a `needs_approval: true`
marker inside item bodies or via a sidecar.

## Accept / Reject / Steer semantics

| item kind     | Accept                                   | Reject                       | Steer                              |
|---------------|------------------------------------------|------------------------------|------------------------------------|
| PR item       | `gh pr merge <N> --repo … --squash`      | thread "Rejected" + dismiss  | thread "Steer: <text>"             |
| non-PR item   | thread "Accepted" + dismiss              | thread "Rejected" + dismiss  | thread "Steer: <text>"             |

In every success case the server returns `{dismissed: true}` and
appends a record to `inner/status/.woy-dismissed.jsonl`
`{slug, action, ts}`. The next Overview render reads that file (set
of slugs) and filters the item out of BOTH the hand-zone markdown
and the structured ask list before shipping them to the browser.

On the client, the row keeps the just-pressed button in a disabled
`"Accepted 2s ago"` state until the server-pushed Tracker tick drops
it; this prevents double-submits and signals "yes it worked."

## Dismissal lifecycle

1. Click Accept / Reject → POST `/api/woy-action`.
2. Server does side-effect (merge or no-op), writes the thread line,
   appends `.woy-dismissed.jsonl`.
3. Server responds with `{dismissed: true, thread_message, …}`.
4. Client optimistically marks the row dismissed (button text flips
   to "Accepted Ns ago").
5. Next `/api/sections?names=waiting_on_you` poll (≤10s later) returns
   a WoY payload that no longer carries the item → React unmounts
   the row.
6. `.woy-dismissed.jsonl` is append-only and bounded on read
   (`WOY_DISMISSED_MAX`); operators can trim it by hand when a page
   is retired.

## Steer endpoint

`POST /api/woy-steer` with `{key, slug, text}`. Validates like the
other handlers (hmac on key, slug→`safe_slug`), rejects empty/overlong
text, appends `{from: "jason", action: "steer", at, text}` to the
item's thread. Does NOT dismiss. The eventual "dispatch steer to
in-flight bg-task" lives in alice-mind surface routing (`for-thinking`
with `steer: slug`), which this slice doesn't implement — the server
endpoint is the stable landing pad that dispatcher wiring will read
from later.

## Agent-UI-testing inspirations

Cited in `DESIGN.md` TODOs:

- **Inline expand with reply + actions** — Claude Code's own
  tool-result expanders; the slot below the row carries the full
  payload so the list stays compact.
- **Steer button pattern** — OpenAI o1-pro's "nudge" affordance:
  a single-line text input that the agent reads as live amendment.
  We adopt the single-line form; the dispatch surface is Slice-2b.
- **Inferred-status chips** (future Slice-2b, called out here so the
  needs-approval predicate isn't accidentally expanded) —
  inspired by Zed's AI-task status badges.

## Out of scope

- Multi-select / bulk Accept-Reject.
- Dispatcher wiring that reads `steer: slug` and pushes the text into
  a running bg-task's amendment channel (surface shape lives in
  alice-mind, not here).
- Per-item inferred status (coming in Slice-2b).
- Mobile swipe shortcuts for Accept / Reject.
