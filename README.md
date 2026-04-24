# alice-viewer

Observability UI for Alice's two hemispheres — reads the JSONL event logs
emitted by the speaking daemon and the thinking wakes, plus the
filesystem artifacts in `alice-mind/inner/` and `alice-mind/memory/`.

## Run

```sh
./run.sh               # binds 127.0.0.1:7777
open http://127.0.0.1:7777
```

## Views

| route           | what                                                       |
|-----------------|------------------------------------------------------------|
| `/`             | unified timeline, live-tail over SSE                       |
| `/wakes`        | thinking wake list + per-wake SDK trace                    |
| `/turns`        | speaking turn list + per-turn SDK trace                    |
| `/interactions` | surfaces / notes / emergencies with timing + verdicts      |
| `/graph`        | d3-force graph: directive → wake → surface → turn → note  |
| `/memory`       | d3-force graph of `memory/**.md` wikilinks                 |
| `/activity`     | stacked band chart over 1h/6h/24h/7d/30d + tool histogram  |

## Data sources (all file-based, no DB)

- `thinking.log` — SDK events from `alice-speaking/think.py`
- `speaking.log` — SDK events from the speaking daemon (`events.py`)
- `inner/state/speaking-turns.jsonl` — per-turn record
- `inner/{surface,emergency,notes,thoughts}/` — pending + handled artifacts
- `memory/**.md` — wikilink parsing
