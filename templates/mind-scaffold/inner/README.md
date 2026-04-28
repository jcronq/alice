# inner/

Two-way channel between Alice's hemispheres. Speaking reads and writes here; thinking reads and writes here; they never call each other.

```
inner/
├── directive.md          # speaking edits → thinking reads at each wake
├── notes/                # speaking drops fleeting observations → thinking drains
│   └── .consumed/        # processed fleeting notes, by date
├── thoughts/             # thinking writes → speaking reads on her own time
│   └── <YYYY-MM-DD>/
├── surface/              # thinking surfaces sharp insights → speaking reviews
│   └── .handled/         # archived surfaces with verdicts
├── emergency/            # external monitors → speaking (bypasses quiet hours)
│   └── .handled/
└── state/                # operational state (turn log, last-wake timestamp)
```

See `HEMISPHERES.md` at the repo root for the full design.
