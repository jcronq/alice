# forge

**TBD** — the idea state machine. Tracks ideas through `draft → selected → designing → designed → building → reviewing → validating → done` (with `blocked` and `rejected` escapes). Source: `src/forge/`.

Filled in as PRs touch the package. Points worth covering when this stub becomes real prose:

- state diagram and allowed transitions
- artifact classification (`art:code`, `art:experiment`, `art:research_note`, `art:config_change`)
- tactical threshold rules (when an idea bypasses design)
- relationship to GitHub issues and the `sm:*` label set
