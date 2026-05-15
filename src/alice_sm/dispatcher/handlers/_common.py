"""Shared imports for the state-handler modules.

Each handler in this sub-package would otherwise need a 30+ line import
block enumerating constants, helpers, renderers, spawn primitives, gh
helpers, and so on. To keep handler files focused on the state-machine
logic rather than the import bookkeeping, the common surface is
consolidated here and each handler does::

    from alice_sm.dispatcher.handlers._common import *

The ``__all__`` list explicitly enumerates the names — including the
underscore-prefixed helpers (e.g. :func:`_label_names`,
:func:`_find_parsed_comment_of_type`) — so star-imports pick them up.

This is internal scaffolding. Add to ``__all__`` only what handlers
need; do not let it grow into a back-door public API.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Constants — labels, prefixes, paths, caps, SPAWN_MAP, _now_iso.
from alice_sm.dispatcher.constants import (
    ACTIVE_SM_LABEL,
    ART_LABEL_WHITELIST,
    BASE_BRANCH,
    BLOCKED_SM_LABEL,
    BUILDING_SM_LABEL,
    COMPACTING_SM_LABEL,
    COMPACT_SIGNAL_FILENAME,
    DEFAULT_REPO,
    DEFAULT_STATE_DIR,
    DEFAULT_STATE_FILE,
    DESIGN_READY_AUDIT_PREFIX,
    DESIGN_REVIEW_SM_LABEL,
    DESIGN_REVISION_CAP,
    DESIGNED_SM_LABEL,
    DESIGNING_SM_LABEL,
    DONE_SM_LABEL,
    DRAFT_SM_LABEL,
    EXIT_TRANSITION_PREFIX,
    EXIT_TRANSITION_REQUIRED_PREFIX,
    MAX_CONCURRENT_SPAWNS,
    MAX_CONCURRENT_SPEAKING_SPAWNS,
    MAX_CONCURRENT_THINKING_SPAWNS,
    NEEDS_STUDY_HINT_DIR,
    NEEDS_STUDY_SM_LABEL,
    NON_TERMINAL_SM_LABELS,
    REJECTED_SM_LABEL,
    RESEARCH_NOTES_DIR,
    REVIEWING_SM_LABEL,
    SEEN_ISSUE_CAP,
    SM_LABEL_WHITELIST,
    SM_SPEAKING_SPAWN_DIR,
    SM_THINKING_SPAWN_DIR,
    SPAWN_DIR,
    SPAWN_MAP,
    SPAWN_STARTED_PREFIX,
    SPEAKING_BUILD_COMPLETE_PREFIX,
    SPEAKING_SPAWN_STARTED_PREFIX,
    STATE_VERSION,
    STUDY_HINT_WRITTEN_PREFIX,
    TERMINAL_SM_LABELS,
    THINKING_SPAWN_STARTED_PREFIX,
    TRUSTED_AUTHORS,
    WORKER_REPO_PATH,
    _now_iso,
)
from alice_sm.dispatcher.errors import GHCommandError
from alice_sm.dispatcher.gh import (
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
    gh_list_sm_issues,
    gh_list_stale_closed_sm_issues,
    gh_post_comment,
)
from alice_sm.dispatcher.git_ops import (
    _attempt_auto_rebase,
    _post_merge_cleanup,
)
from alice_sm.dispatcher.helpers import (
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
from alice_sm.dispatcher.rendering import (
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
from alice_sm.dispatcher.report import (
    DependencyResolution,
    RunReport,
    resolve_dependencies,
)
from alice_sm.dispatcher.spawn import (
    _current_spawn_map,
    compose_rebase_prompt,
    compose_speaking_spawn_prompt,
    compose_spawn_prompt,
    compose_thinking_spawn_prompt,
    count_running_spawns,
    count_running_speaking_spawns,
    count_running_thinking_spawns,
    find_live_spawn_dir_for_issue,
    has_live_spawn_for_issue,
    has_live_speaking_spawn_for_issue,
    has_live_thinking_spawn_for_issue,
    proactive_reap_dead_spawns,
    render_spawn_started_comment,
    render_speaking_spawn_started_comment,
    render_thinking_spawn_started_comment,
    spawn_agent,
    spawn_rebase_agent,
    spawn_speaking_agent,
    spawn_thinking_agent,
)
from alice_sm.dispatcher.state import DispatcherState, load_state, save_state
from alice_sm.dispatcher.trust import (
    TrustDecision,
    _author_login,
    _current_sm_label,
    _label_names,
    evaluate_trust,
)
from alice_sm.dispatcher.types import (
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
from alice_sm.dispatcher.verify import (
    _verify_enabled,
    default_verifier,
)


__all__ = [
    # stdlib re-exports for handler convenience
    "argparse", "json", "os", "pathlib", "re", "sys", "time", "uuid",
    "dataclass", "field",
    "Any", "Callable", "Iterable",
    # constants
    "ACTIVE_SM_LABEL", "ART_LABEL_WHITELIST", "BASE_BRANCH", "BLOCKED_SM_LABEL",
    "BUILDING_SM_LABEL", "COMPACTING_SM_LABEL", "COMPACT_SIGNAL_FILENAME",
    "DEFAULT_REPO", "DEFAULT_STATE_DIR", "DEFAULT_STATE_FILE",
    "DESIGN_READY_AUDIT_PREFIX", "DESIGN_REVIEW_SM_LABEL", "DESIGN_REVISION_CAP",
    "DESIGNED_SM_LABEL", "DESIGNING_SM_LABEL", "DONE_SM_LABEL", "DRAFT_SM_LABEL",
    "EXIT_TRANSITION_PREFIX", "EXIT_TRANSITION_REQUIRED_PREFIX",
    "MAX_CONCURRENT_SPAWNS", "MAX_CONCURRENT_SPEAKING_SPAWNS",
    "MAX_CONCURRENT_THINKING_SPAWNS", "NEEDS_STUDY_HINT_DIR",
    "NEEDS_STUDY_SM_LABEL", "NON_TERMINAL_SM_LABELS", "REJECTED_SM_LABEL",
    "RESEARCH_NOTES_DIR", "REVIEWING_SM_LABEL", "SEEN_ISSUE_CAP",
    "SM_LABEL_WHITELIST", "SM_SPEAKING_SPAWN_DIR", "SM_THINKING_SPAWN_DIR",
    "SPAWN_DIR", "SPAWN_MAP", "SPAWN_STARTED_PREFIX",
    "SPEAKING_BUILD_COMPLETE_PREFIX", "SPEAKING_SPAWN_STARTED_PREFIX",
    "STATE_VERSION", "STUDY_HINT_WRITTEN_PREFIX", "TERMINAL_SM_LABELS",
    "THINKING_SPAWN_STARTED_PREFIX", "TRUSTED_AUTHORS", "WORKER_REPO_PATH",
    "_now_iso",
    # errors
    "GHCommandError",
    # gh helpers
    "gh_close_issue", "gh_edit_labels", "gh_find_linked_pr",
    "gh_find_unspawned_selected_issues", "gh_get_issue",
    "gh_get_master_ci_status", "gh_get_pr_files", "gh_get_pr_mergeable",
    "gh_get_pr_merge_status", "gh_list_issue_comments",
    "gh_list_open_done_sm_issues", "gh_list_sm_issues",
    "gh_list_stale_closed_sm_issues", "gh_post_comment",
    # git ops
    "_attempt_auto_rebase", "_post_merge_cleanup",
    # internal handler helpers
    "_RESEARCH_WORKER_DONE_PREFIX",
    "_comment_author_login", "_current_art_label",
    "_find_parsed_comment_of_type", "_find_resolving_research_note",
    "_has_exit_transition_comment", "_has_prior_study_hint_audit",
    "_matches_resolves_issue", "_research_close_signal",
    # rendering
    "REBASE_ESCALATED_PREFIX", "REBASE_NEEDED_PREFIX", "REBASE_PUSHED_PREFIX",
    "render_auto_study_complete_comment", "render_design_ready_audit_comment",
    "render_design_revisions_capped_comment",
    "render_exit_transition_required_comment", "render_hello_comment",
    "render_rebase_escalation_comment", "render_rebase_needed_audit_comment",
    "render_rebase_pushed_audit_comment", "render_study_hint_audit_comment",
    "render_study_hint_note_body", "render_transition_comment",
    "render_verify_comment",
    # report
    "DependencyResolution", "RunReport", "resolve_dependencies",
    # spawn
    "_current_spawn_map",
    "compose_rebase_prompt", "compose_speaking_spawn_prompt",
    "compose_spawn_prompt", "compose_thinking_spawn_prompt",
    "count_running_spawns", "count_running_speaking_spawns",
    "count_running_thinking_spawns", "find_live_spawn_dir_for_issue",
    "has_live_spawn_for_issue", "has_live_speaking_spawn_for_issue",
    "has_live_thinking_spawn_for_issue", "proactive_reap_dead_spawns",
    "render_spawn_started_comment", "render_speaking_spawn_started_comment",
    "render_thinking_spawn_started_comment",
    "spawn_agent", "spawn_rebase_agent", "spawn_speaking_agent",
    "spawn_thinking_agent",
    # state
    "DispatcherState", "load_state", "save_state",
    # trust
    "TrustDecision", "_author_login", "_current_sm_label", "_label_names",
    "evaluate_trust",
    # types
    "CloseIssueFn", "EditLabelsFn", "FindLinkedPRFn", "FindUnspawnedFn",
    "GitRunFn", "ListCommentsFn", "ListIssuesFn", "MasterCIStatusFn",
    "PostCommentFn", "PostMergeCleanupFn", "PRFilesFn", "PRMergeableFn",
    "PRMergeStatusFn", "VerifyFn",
    # verify
    "_verify_enabled", "default_verifier",
]
