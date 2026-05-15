"""Callable type aliases for the dispatcher.

Tests inject fakes here without monkeypatching the module-level names.
Kept in a tiny standalone module so the SM v1.5 / v2 handlers and the
:func:`alice_sm.dispatcher.run` wiring share one source of truth for the
gh / git / verifier / spawn callable shapes.
"""

from __future__ import annotations

import pathlib
import subprocess  # noqa: F401  (used in string-typed annotation below)
from typing import Any, Callable


ListIssuesFn = Callable[[str], list[dict[str, Any]]]
PostCommentFn = Callable[[str, int, str], None]
EditLabelsFn = Callable[..., None]
CloseIssueFn = Callable[[str, int], None]
FindLinkedPRFn = Callable[[str, int], dict[str, Any] | None]
PRMergeStatusFn = Callable[[str, int], dict[str, Any]]
PRMergeableFn = Callable[[str, int], dict[str, Any]]
MasterCIStatusFn = Callable[[str, str], dict[str, Any]]
ListCommentsFn = Callable[[str, int], list[dict[str, Any]]]
FindUnspawnedFn = Callable[[str], list[dict[str, Any]]]
PRFilesFn = Callable[[str, int], list[str]]
# Verifier contract: takes a PR number + the list of files it changed,
# returns a verdict dict ``{outcome, reason, route}``. ``outcome`` is
# one of ``"pass"`` / ``"skip"`` / ``"fail"`` — corresponding to the
# three audit comment shapes. ``route`` is the URL hit on pass / fail
# (None on skip).
VerifyFn = Callable[[int, list[str]], dict[str, Any]]
# (cmd_args, cwd) → CompletedProcess. ``cmd_args`` is the trailing
# argv (no leading ``git``); ``cwd`` is the repo to operate in. Tests
# inject a fake to avoid touching the real working tree.
GitRunFn = Callable[[list[str], pathlib.Path], "subprocess.CompletedProcess[str]"]
PostMergeCleanupFn = Callable[[str | None, int], None]
