"""``alice-backend`` CLI — show the resolved backend per hemisphere.

Plan 06 Phase 7. Useful for verifying ``mind/config/model.yml``
during deploys + debugging when a hemisphere lands on the wrong
backend.

Two subcommands:

- ``alice-backend show`` — prints the resolved backend + model per
  hemisphere from ``model.yml``.
- ``alice-backend test [hemisphere]`` — placeholder for the live LLM
  smoke test (deferred; see plan 06 phases 5/6 — paid).
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

from .model import HEMISPHERES, ModelConfig
from .model import load as load_model_config


def _default_mind_path() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("ALICE_MIND") or pathlib.Path.home() / "alice-mind"
    )


def _show(args: argparse.Namespace) -> int:
    mind = pathlib.Path(args.mind)
    cfg = load_model_config(mind)
    print(f"# {mind / 'config' / 'model.yml'}")
    if cfg == ModelConfig.subscription_default():
        print("(no model.yml present — every hemisphere on subscription default)")
    has_pi = False
    for name in HEMISPHERES:
        spec = cfg.hemisphere(name)
        bits = [f"backend={spec.backend}"]
        if spec.model:
            bits.append(f"model={spec.model}")
        if spec.region:
            bits.append(f"region={spec.region}")
        if spec.profile:
            bits.append(f"profile={spec.profile}")
        if spec.base_url:
            bits.append(f"base_url={spec.base_url}")
        print(f"{name:>10}: " + "  ".join(bits))
        if spec.backend == "pi":
            has_pi = True

    if has_pi:
        # Pi backend reads ~/.pi/agent/auth.json; surface its presence
        # + token expiry so the operator can spot a stale auth before
        # the next rate-limit window hits.
        _print_pi_auth_status()
    return 0


def _print_pi_auth_status() -> None:
    import json as _json
    import time as _time

    pi_auth = pathlib.Path.home() / ".pi" / "agent" / "auth.json"
    print()
    if not pi_auth.is_file():
        print(f"      pi: {pi_auth} not found — run codex-to-pi-auth on host")
        return
    try:
        data = _json.loads(pi_auth.read_text())
    except _json.JSONDecodeError:
        print(f"      pi: {pi_auth} unparseable")
        return
    cred = data.get("openai-codex")
    if not cred or cred.get("type") != "oauth":
        print(f"      pi: {pi_auth} has no openai-codex oauth entry")
        return
    expires_ms = cred.get("expires", 0)
    remaining_min = int((expires_ms - _time.time() * 1000) / 60000)
    if remaining_min > 0:
        print(f"      pi: openai-codex token valid (~{remaining_min} min remaining)")
    else:
        print(f"      pi: openai-codex token EXPIRED — re-run codex login + bridge")


def _test(args: argparse.Namespace) -> int:
    print(
        "alice-backend test: live LLM smoke not yet wired (plan 06 phases 5/6 "
        "— paid; AWS / LiteLLM creds required). Run via .venv/bin/pytest with "
        "ALICE_LIVE_TESTS=1 once those phases ship.",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alice-backend",
        description="Inspect the per-hemisphere LLM backend resolved from "
        "mind/config/model.yml.",
    )
    parser.add_argument(
        "--mind",
        default=str(_default_mind_path()),
        help="alice-mind path (default: $ALICE_MIND or ~/alice-mind)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="Print resolved backend + model per hemisphere")
    sub_test = sub.add_parser(
        "test",
        help="Smoke-test the chosen hemisphere's backend (paid; not yet wired)",
    )
    sub_test.add_argument(
        "hemisphere", nargs="?", choices=HEMISPHERES, default="speaking"
    )
    args = parser.parse_args(argv)
    if args.cmd == "show":
        return _show(args)
    if args.cmd == "test":
        return _test(args)
    parser.error("unreachable")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
