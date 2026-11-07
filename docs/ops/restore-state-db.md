# Restoring `alice-state.db` from NAS backup

> Audience: future-Jason or future-me, under pressure, at 2 AM.
> Primary objective: get Alice back on her feet without destroying
> the evidence of what broke.

`alice-state.db` holds SM v2 state — items, state_machine,
transitions, amendments, external refs, plus the derived task /
worker / event tables. Once PR-6 lands and GitHub is no longer the
accidental backup (issue #823), this NAS chain is the only copy
of durable Alice state. Treat it accordingly.

## Where things live

- **Live DB** — `/home/alice/alice-mind/inner/state/alice-state.db`
  inside the `alice` container, backed by the host bind-mount
  `~/alice-mind/inner/state/alice-state.db` on aimax1.
- **NAS target** — `cronqj@10.20.30.14:/mnt/pool0/backups/alice-state-db/`
  laid out as `YYYY/MM/DD/alice-state-db-YYYYMMDD-HHMM.db` with a
  sibling `.sha256` manifest.
- **Backup cadence** — hourly cron (`:07 past the hour`); see
  `config/cron.d/alice-state-db-backup`.
- **Monthly verify** — `scripts/verify_state_db_backup.py` OR
  `bin/alice-state-db-verify`; see
  `config/cron.d/alice-state-db-verify`.
- **Backup notes** — each hourly run drops a JSON line to
  `~/alice-mind/inner/notes/alice-state-db-backup-<date>.jsonl`.
  The memory worker drains those into `events.jsonl` on its own
  cadence.

## The quick recipe (happy path)

1. **Stop the writer.** On aimax1:
   ```bash
   docker compose -f /home/cronqj/dev/alice/runtime/sandbox/docker-compose.yml \
       stop alice
   ```
   This is critical. The restore script will refuse (`status:
   live_wal_present`) if `alice-state.db-wal` is still around, and
   you really do not want to swap the DB underneath a running
   writer — SQLite will try to recover the dangling journal against
   the new file and corrupt it.

2. **Run the restore.** Still on aimax1 (not inside the stopped
   container), but with the venv set up:
   ```bash
   docker exec alice /home/alice/alice/bin/alice-state-db-restore --verbose
   ```
   …except the container is stopped, so use the host path:
   ```bash
   /home/cronqj/dev/alice/runtime/bin/alice-state-db-restore --verbose
   ```
   Or, simpler, keep the container running and pass `--force` — the
   script will overwrite the DB under the still-running writer. This
   is NOT recommended but is sometimes the only option when you can't
   afford the downtime.

3. **Verify the swap.** The script prints a JSON result:
   ```json
   {
     "status": "ok",
     "snapshot": "2026/02/03/alice-state-db-20260203-1415.db",
     "sha256": "…",
     "integrity": "ok",
     "safety_copy": "/home/alice/alice-mind/inner/state/pre-restore/alice-state.db.pre-restore-20260204-0315"
   }
   ```
   Spot-check a few tables:
   ```bash
   sqlite3 /home/alice/alice-mind/inner/state/alice-state.db \
       'SELECT COUNT(*) FROM items; SELECT COUNT(*) FROM state_machine;'
   ```

4. **Restart the writer.**
   ```bash
   docker compose -f /home/cronqj/dev/alice/runtime/sandbox/docker-compose.yml \
       up -d alice
   docker logs --tail 50 alice
   ```
   Confirm `event":"started"` appears in `/state/worker/speaking.log`.

## What the restore script actually does

(This is the contract — knowing it helps when something fails.)

1. **Enumerate** the NAS directory, parse filenames into timestamps,
   walk newest-first.
2. **Download** each candidate's `.db` + `.sha256` to a tmp dir on
   the same filesystem as the target DB (so the final swap is a
   same-FS atomic rename).
3. **Verify sha256** against the manifest. On mismatch, log and
   fall back to the next-newest.
4. **PRAGMA integrity_check** against the fetched DB. Must return
   literal `"ok"` or we refuse to swap.
5. **Move the current DB aside** to `<target_dir>/pre-restore/alice-state.db.pre-restore-<ts>`
   along with any `-wal` / `-shm` sidecars.
6. **Atomic swap** the verified copy into place via `os.replace`.
7. Report a JSON summary on stdout.

Any failure before step 6 is non-destructive: the live DB is still
there. Step 5 is reversible — if the restore turns out to be the
wrong snapshot, move the staged copy back.

## Picking a specific snapshot (when "latest" is wrong)

The default behaviour is "newest valid wins." If you need to roll
back to a specific point (e.g. "undo the last three hours of state
churn"), there's no flag yet — easiest path:

```bash
# On aimax1:
ssh cronqj@10.20.30.14 ls /mnt/pool0/backups/alice-state-db/2026/02/03/
# Pick the timestamp you want, pull it manually:
scp cronqj@10.20.30.14:/mnt/pool0/backups/alice-state-db/2026/02/03/alice-state-db-20260203-0800.db \
    /tmp/alice-state.db.candidate
scp cronqj@10.20.30.14:/mnt/pool0/backups/alice-state-db/2026/02/03/alice-state-db-20260203-0800.db.sha256 \
    /tmp/alice-state.db.candidate.sha256
cd /tmp && sha256sum -c alice-state.db.candidate.sha256
sqlite3 /tmp/alice-state.db.candidate 'PRAGMA integrity_check;'
# Then stop the container, move the current DB aside, mv the
# candidate into place.
```

A `--snapshot <relpath>` flag is a reasonable follow-up PR if this
turns into a common path.

## When it fails

- **`status: live_wal_present`.** Stop the alice container and
  retry, or pass `--force` (and understand the risk).
- **`status: no_valid_snapshot`.** The NAS directory is empty, or
  every snapshot's sha256 failed verification. Check
  `ssh cronqj@10.20.30.14 ls /mnt/pool0/backups/alice-state-db/`.
  If the directory is empty, the backup chain has been broken for
  at least `max(retention)` hours — investigate the hourly cron and
  its log at `/var/log/alice-state-db-backup.log`.
- **`status: integrity_failed`.** The newest sha-valid snapshot is
  corrupt at the SQLite level. Walk back manually (section above)
  until you find one that opens cleanly. Also: file an issue — this
  means a corruption escaped VACUUM INTO, which should be
  impossible.

## Monthly verify

The `bin/alice-state-db-verify` script runs on the 1st of each
month at 04:17 (see `config/cron.d/alice-state-db-verify`). It:

1. Pulls the latest snapshot.
2. Verifies sha256.
3. Runs `PRAGMA integrity_check`.
4. Counts rows on `items`, `state_machine`, `transitions` in both
   the backup and the live DB, and drops a `conflict-candidate`
   note to `inner/notes/` if any delta exceeds 5%.

A failing verify hits the signal/log callback with a message like
`state-db verify: integrity_check returned 'bad'`. Wire that up to
`send_message(jason, …)` in whatever supervisor runs the cron.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
