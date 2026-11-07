#!/usr/bin/env python3
"""One-time ``sm:*`` label strip (issue #829, PR-6 of #823).

Phase B of the #823 cutover plan deprecates SM-on-GitHub in favour of
the DB-primary state machine. Once the dispatcher / thinking / speaking
paths stop reading ``sm:*`` labels (PR-3/PR-4/PR-5), this script sweeps
every issue in ``jcronq/alice`` and ``jcronq/cozyhem-engine`` and strips
every ``sm:*`` label. Manually invoked — not wired into any dispatcher,
cron, or merge path.

Usage::

    # Dry-run (default): print what would change, mutate nothing.
    python scripts/strip_sm_labels.py

    # Mutate for real, writing the restore manifest first.
    python scripts/strip_sm_labels.py --apply

    # Different repos / custom restore file.
    python scripts/strip_sm_labels.py --apply \\
        --repo jcronq/alice,jcronq/cozyhem-engine \\
        --restore-file /tmp/strip.restore.json

Exit codes:
    0  completed (dry-run or apply with zero/more removals)
    1  a ``gh`` call failed partway through — see
       ``scripts/strip_sm_labels.log`` for the exact issue count that
       succeeded, and ``--restore-file`` for the full pre-mutation
       state.

Idempotent — a re-run after a successful ``--apply`` finds zero
``sm:*``-labeled issues and performs zero ``gh issue edit`` calls.

Rate-limit aware via :mod:`alice_forge.rate_limit` — before every batch
of 50 mutations, :func:`alice_forge.rate_limit.should_defer` is polled
and the script sleeps if the gh budget is under the floor or a global
backoff window is active.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Callable

from alice_forge import rate_limit as rl


log = logging.getLogger("strip_sm_labels")


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent

DEFAULT_REPOS = "jcronq/alice,jcronq/cozyhem-engine"
DEFAULT_RESTORE_FILE = SCRIPT_DIR / "strip_sm_labels.restore.json"
DEFAULT_LOG_FILE = SCRIPT_DIR / "strip_sm_labels.log"

# Mutation-batch size for the rate-limit gate: we poll ``should_defer``
# before the 1st, 51st, 101st, ... mutation. 50 is small enough that an
# in-flight burst can't blow through the floor, large enough that the
# poll overhead stays negligible.
BATCH_SIZE = 50


def _run_gh(args: list[str], *, timeout: int = 60) -> str:
    """Invoke ``gh`` with ``args`` and return stdout. Raises on error."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed (rc={result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout


def list_sm_labeled_issues(
    repo: str,
    *,
    runner: Callable[[list[str]], str] = _run_gh,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return every issue in ``repo`` that carries any ``sm:*`` label.

    Uses the ``label:sm:`` prefix search so GitHub does the filtering
    and the script catches every current and legacy ``sm:*`` label —
    not just what ``SM_LABEL_WHITELIST`` enumerates (typos, old
    deprecated labels like ``sm:needs_study`` all surface here).
    """
    args = [
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--search",
        "label:sm:",
        "--json",
        "number,labels,url",
        "--limit",
        str(limit),
    ]
    stdout = runner(args)
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    return payload if isinstance(payload, list) else []


def labels_to_strip(issue: dict[str, Any]) -> list[str]:
    """Return the sorted ``sm:*`` label names on ``issue``.

    Defensive about label-entry shape: ``gh issue list --json labels``
    returns dicts (``{"name": ...}``) today, but we accept plain
    strings too in case the response shape ever shifts.
    """
    names: list[str] = []
    for entry in issue.get("labels") or []:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str) and name.startswith("sm:"):
            names.append(name)
    return sorted(names)


def build_restore_manifest(
    repos: list[str],
    *,
    runner: Callable[[list[str]], str] = _run_gh,
) -> list[dict[str, Any]]:
    """Scan every repo and return the full pre-mutation manifest.

    Skips issues with no ``sm:*`` labels (so a second ``--apply`` run
    finds an empty manifest and performs zero edits — the idempotency
    requirement).
    """
    manifest: list[dict[str, Any]] = []
    for repo in repos:
        for issue in list_sm_labeled_issues(repo, runner=runner):
            to_remove = labels_to_strip(issue)
            if not to_remove:
                continue
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            manifest.append(
                {
                    "repo": repo,
                    "issue": number,
                    "labels": to_remove,
                }
            )
    return manifest


def strip_issue_labels(
    repo: str,
    number: int,
    labels: list[str],
    *,
    runner: Callable[[list[str]], str] = _run_gh,
) -> None:
    """Issue one ``gh issue edit`` call stripping all ``labels``.

    ``--remove-label`` is a repeatable StringArray gh-CLI flag, so we
    pass one instance per label rather than comma-joining.
    """
    args = ["issue", "edit", str(number), "--repo", repo]
    for label in labels:
        args += ["--remove-label", label]
    runner(args)


def _append_log(
    path: pathlib.Path,
    repo: str,
    number: int,
    labels: list[str],
    *,
    now: dt.datetime | None = None,
) -> None:
    """Append one JSON-line success record to ``path``.

    Single-writer ("Jason runs this script interactively, once"), so
    plain append mode is safe — no atomic-rename needed.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    line = json.dumps(
        {
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "repo": repo,
            "issue": number,
            "labels_removed": labels,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(line + "\n")


def _write_restore_file(path: pathlib.Path, manifest: list[dict[str, Any]]) -> None:
    """Persist ``manifest`` to ``path`` atomically.

    Temp-file + ``os.rename`` so a botched write never leaves a half-
    written restore file — the whole point of the manifest is
    "recoverable after a botched strip."
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.replace(tmp, path)


def apply_strip(
    manifest: list[dict[str, Any]],
    *,
    runner: Callable[[list[str]], str] = _run_gh,
    sleep: Callable[[float], None] = time.sleep,
    should_defer: Callable[..., rl.DeferDecision] = rl.should_defer,
    log_path: pathlib.Path = DEFAULT_LOG_FILE,
) -> None:
    """Mutate GitHub per ``manifest``, polling the rate-limit gate.

    Before entry ``i`` where ``i % BATCH_SIZE == 0`` (i.e. before the
    1st, 51st, 101st, ... mutation), poll ``should_defer`` in a loop;
    sleep ``retry_after_s`` (or 60 s fallback) if deferred, re-poll,
    repeat until the gate passes. Then strip the labels and append a
    log line.
    """
    for i, entry in enumerate(manifest):
        if i % BATCH_SIZE == 0:
            decision = should_defer()
            while decision.defer:
                log.info(
                    "rate-limit gate deferred (reason=%s retry_after_s=%s)"
                    " — sleeping",
                    decision.reason,
                    decision.retry_after_s,
                )
                sleep(decision.retry_after_s or 60.0)
                decision = should_defer()
        strip_issue_labels(
            entry["repo"], entry["issue"], entry["labels"], runner=runner
        )
        _append_log(log_path, entry["repo"], entry["issue"], entry["labels"])
        log.info(
            "stripped %s #%d: %s", entry["repo"], entry["issue"], entry["labels"]
        )


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strip_sm_labels",
        description=(
            "One-shot sweeper that strips every sm:* label from every "
            "issue in the given repos. Dry-run by default; --apply "
            "mutates. PR-6 of #823."
        ),
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPOS,
        help=(
            "Comma-separated repo list "
            f"(default: {DEFAULT_REPOS})."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually strip the labels. Default is dry-run which "
            "only prints what would change."
        ),
    )
    parser.add_argument(
        "--restore-file",
        default=str(DEFAULT_RESTORE_FILE),
        help=(
            "Path to write the pre-mutation (repo, issue, labels) "
            f"manifest (default: {DEFAULT_RESTORE_FILE})."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    args = _build_argparser().parse_args(argv)
    repos = [r.strip() for r in args.repo.split(",") if r.strip()]
    restore_path = pathlib.Path(args.restore_file)

    # Resolve ``_run_gh`` at call time (not via default-arg binding) so a
    # test can ``monkeypatch.setattr(mod, "_run_gh", FakeRunner(...))``
    # and have ``main`` pick it up.
    runner = _run_gh
    try:
        manifest = build_restore_manifest(repos, runner=runner)
    except (RuntimeError, json.JSONDecodeError) as exc:
        log.error("strip failed during manifest build: %s", exc)
        return 1

    if not args.apply:
        for entry in manifest:
            print(f"{entry['repo']} #{entry['issue']}: {entry['labels']}")
        log.info(
            "DRY-RUN complete: %d issue(s) would be stripped across %s",
            len(manifest),
            ",".join(repos),
        )
        return 0

    # Write the restore manifest BEFORE any mutation so a crash mid-run
    # still leaves a complete record of the pre-strip state.
    _write_restore_file(restore_path, manifest)
    log.info(
        "wrote restore manifest (%d entries) to %s",
        len(manifest),
        restore_path,
    )

    try:
        apply_strip(manifest, runner=runner, log_path=DEFAULT_LOG_FILE)
    except RuntimeError as exc:
        log.error("strip failed partway through: %s", exc)
        return 1
    log.info(
        "APPLY complete: %d issue(s) stripped across %s",
        len(manifest),
        ",".join(repos),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
