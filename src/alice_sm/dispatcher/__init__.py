"""State Machine v0/v1.5/v2 dispatcher — ``gh``-driven label-driven dispatcher.

Modeled on :mod:`alice_watchers.github`. Each invocation is a single pass:

  1. Poll ``jcronq/alice`` for open issues with any ``sm:*`` label
     (``gh issue list ... --json number,title,labels,author,...``).
  2. For ``sm:selected`` issues:
     - Apply the v0 trust filter — author whitelist, exactly one
       ``sm:*`` label, at least one ``art:*`` label — all from explicit
       allow-lists so a typo (``sm:building-pleaserun``) is silently
       dropped instead of producing a fuzzy match.
     - For each unseen passing issue, post a one-time
       ``[SM] dispatcher-hello ...`` comment as audit-trail evidence
       and record the issue number in
       ``/state/worker/sm-dispatcher-state.json`` so we don't
       re-comment on the next cadence.
     - If a linked open PR exists, transition to ``sm:reviewing``
       (Phase 1.5 T1). Hello + transition can co-occur in one pass.
     - Phase 2: if the issue has not already been spawned on (no
       ``[SM] spawn-started`` comment from a trusted author), and the
       global concurrency cap has room, spawn a detached ``claude``
       CLI subprocess to actually do the work. The spawn comment is
       posted *before* the Popen so the next pass sees the dedup
       marker even if the spawn crashes immediately.
  3. For ``sm:reviewing`` issues (Phase 1.5 T2/T3):
     - If the linked PR is merged AND master CI on the merge commit
       is green → relabel ``sm:done``, close the issue.
     - If the linked PR is merged AND master CI is red → relabel
       ``sm:building`` (do NOT close, do NOT spawn anything yet).
     - If still pending or PR still open, stay.

Phase 2 adds agent spawning but does NOT handle the persona × runtime
matrix (everything spawns Claude CLI), amendments in-flight, or
session continuity across review cycles. Those land in later phases.

The script is intended to be invoked on a cadence by s6 (later phase);
right now it runs by hand via ``python -m alice_sm.dispatcher``. The
``--dry-run`` flag prints the comments / transitions / spawns that
would be made without touching GitHub or launching subprocesses —
useful for tests and manual verification.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


# ---------------------------------------------------------------------------
# Constants — extracted to alice_sm.dispatcher.constants (issue #193).
# Re-exported so ``from alice_sm.dispatcher import X`` keeps working.
#
# If ``constants`` is already loaded when this package is re-executed
# (``importlib.reload(alice_sm.dispatcher)``), reload it first so the
# env-driven caps (MAX_CONCURRENT_*_SPAWNS) refresh against the current
# environment. The two existing reload-pattern tests
# (test_thinking_spawn_concurrency_cap_constant_and_env_override,
# test_speaking_spawn_concurrency_cap_constant_and_env_override) depend
# on this — pre-split, both constants lived in ``dispatcher.py`` and
# reloading the module picked up env changes directly.
# ---------------------------------------------------------------------------
import importlib as _importlib

_constants_modname = __name__ + ".constants"
if _constants_modname in sys.modules:
    _importlib.reload(sys.modules[_constants_modname])
del _importlib, _constants_modname

from alice_sm.dispatcher.constants import *  # noqa: F401, F403, E402
from alice_sm.dispatcher.constants import _now_iso  # noqa: F401, E402



# ---------------------------------------------------------------------------
# Errors — extracted to alice_sm.dispatcher.errors (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.errors import GHCommandError  # noqa: E402, F401


# ---------------------------------------------------------------------------
# State load/save — extracted to alice_sm.dispatcher.state (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.state import (  # noqa: E402, F401
    DispatcherState,
    load_state,
    save_state,
)



# ---------------------------------------------------------------------------
# gh CLI shims — extracted to alice_sm.dispatcher.gh (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.gh import (  # noqa: E402, F401
    _run_gh,
    _sort_oldest_first,
    gh_close_issue,
    gh_edit_labels,
    gh_find_linked_pr,
    gh_find_unspawned_selected_issues,
    gh_get_issue,
    gh_get_master_ci_status,
    gh_get_pr_files,
    gh_get_pr_mergeable,
    gh_get_pr_merge_status,
    gh_list_issue_comments,
    gh_list_open_done_sm_issues,
    gh_list_selected_issues,
    gh_list_sm_issues,
    gh_list_stale_closed_sm_issues,
    gh_post_comment,
)




# Callable type aliases — extracted to alice_sm.dispatcher.types (issue #193).
from alice_sm.dispatcher.types import (  # noqa: E402, F401
    CloseIssueFn,
    EditLabelsFn,
    FindLinkedPRFn,
    FindUnspawnedFn,
    GitRunFn,
    ListCommentsFn,
    ListIssuesFn,
    MasterCIStatusFn,
    PostCommentFn,
    PostMergeCleanupFn,
    PRFilesFn,
    PRMergeableFn,
    PRMergeStatusFn,
    VerifyFn,
)


# ---------------------------------------------------------------------------
# Git operations — extracted to alice_sm.dispatcher.git_ops (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.git_ops import (  # noqa: E402, F401
    _REBASE_CONFLICT_FILE_RE,
    _attempt_auto_rebase,
    _extract_rebase_conflict_file,
    _post_merge_cleanup,
    _run_git,
)



# ---------------------------------------------------------------------------
# Spawn machinery — extracted to alice_sm.dispatcher.spawn (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.spawn import (  # noqa: E402, F401
    _SPAWN_DIR_NAME_RE,
    _copy_session_jsonl_into_spawn,
    _find_worker_session_jsonl,
    _reap_spawn_dir,
    _spawn_dir_is_alive,
    _spawn_dir_issue_number,
    compose_rebase_prompt,
    compose_speaking_spawn_prompt,
    compose_spawn_prompt,
    compose_thinking_spawn_prompt,
    count_running_speaking_spawns,
    count_running_spawns,
    count_running_thinking_spawns,
    find_live_spawn_dir_for_issue,
    has_live_spawn_for_issue,
    has_live_speaking_spawn_for_issue,
    has_live_thinking_spawn_for_issue,
    proactive_reap_dead_spawns,
    render_spawn_started_comment,
    render_speaking_spawn_started_comment,
    render_thinking_spawn_started_comment,
    resolve_claude_bin,
    resolve_python_bin,
    spawn_agent,
    spawn_rebase_agent,
    spawn_speaking_agent,
    spawn_thinking_agent,
)



# ---------------------------------------------------------------------------
# Trust filter — extracted to alice_sm.dispatcher.trust (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.trust import (  # noqa: E402, F401
    TrustDecision,
    _author_login,
    _current_sm_label,
    _label_names,
    evaluate_trust,
)




# ---------------------------------------------------------------------------
# Comment rendering — extracted to alice_sm.dispatcher.rendering (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.rendering import (  # noqa: E402, F401
    REBASE_ESCALATED_PREFIX,
    REBASE_NEEDED_PREFIX,
    REBASE_PUSHED_PREFIX,
    render_auto_study_complete_comment,
    render_design_ready_audit_comment,
    render_design_revisions_capped_comment,
    render_exit_transition_required_comment,
    render_hello_comment,
    render_rebase_escalation_comment,
    render_rebase_needed_audit_comment,
    render_rebase_pushed_audit_comment,
    render_study_hint_audit_comment,
    render_study_hint_note_body,
    render_transition_comment,
    render_verify_comment,
)


# ---------------------------------------------------------------------------
# Verification (issue #128) — extracted to alice_sm.dispatcher.verify.
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.verify import (  # noqa: E402, F401
    _http_get_body,
    _touches_viewer,
    _verify_enabled,
    default_verifier,
    verify_viewer_route,
)




# ---------------------------------------------------------------------------
# Run report + dependency resolution — extracted to
# alice_sm.dispatcher.report (issue #193).
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.report import (  # noqa: E402, F401
    DependencyResolution,
    RunReport,
    resolve_dependencies,
)


# ---------------------------------------------------------------------------
# Shared handler helpers — extracted to alice_sm.dispatcher.helpers
# (issue #193). The pre-split duplicate definition of
# ``_find_parsed_comment_of_type`` (where the second def shadowed the
# first at runtime) is collapsed to a single definition; behavior is
# unchanged because no caller held a reference to the shadowed first
# definition.
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.helpers import (  # noqa: E402, F401
    _RESEARCH_WORKER_DONE_PREFIX,
    _comment_author_login,
    _current_art_label,
    _find_parsed_comment_of_type,
    _find_resolving_research_note,
    _has_exit_transition_comment,
    _has_prior_study_hint_audit,
    _matches_resolves_issue,
    _research_close_signal,
)


# ---------------------------------------------------------------------------
# State handlers — extracted to alice_sm.dispatcher.handlers.<state>
# (issue #193). One file per state-transition handler so workers don't
# have to read a 7K-line monolith to make a 30-line change.
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.handlers.building import _process_building  # noqa: E402, F401
from alice_sm.dispatcher.handlers.compacting import _process_compacting  # noqa: E402, F401
from alice_sm.dispatcher.handlers.design_review import _process_design_review  # noqa: E402, F401
from alice_sm.dispatcher.handlers.designed import (  # noqa: E402, F401
    _designed_spawn_speaking,
    _process_designed,
)
from alice_sm.dispatcher.handlers.designing import _process_designing  # noqa: E402, F401
from alice_sm.dispatcher.handlers.draft import _process_draft  # noqa: E402, F401
from alice_sm.dispatcher.handlers.needs_study import _process_needs_study  # noqa: E402, F401
from alice_sm.dispatcher.handlers.open_done import _process_open_done  # noqa: E402, F401
from alice_sm.dispatcher.handlers.reviewing import (  # noqa: E402, F401
    _handle_conflicting_pr,
    _process_reviewing,
)
from alice_sm.dispatcher.handlers.selected import _process_selected  # noqa: E402, F401
from alice_sm.dispatcher.handlers.stale_closed import _process_stale_closed  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Dispatcher main pass + CLI — extracted to alice_sm.dispatcher.main
# (issue #193). __init__.py keeps the ``run`` and ``main`` re-exports so
# ``alice-sm = "alice_sm.dispatcher:main"`` (pyproject.toml) and any
# caller of ``alice_sm.dispatcher.run(...)`` continue to work.
# ---------------------------------------------------------------------------
from alice_sm.dispatcher.main import main, run  # noqa: E402, F401






# ---------------------------------------------------------------------------
# Issue #157 — sm:needs_study handler
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Issue #164 — sm:designing / design_review / designed / compacting / building
# ---------------------------------------------------------------------------


















if __name__ == "__main__":
    sys.exit(main())
