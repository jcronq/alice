"""Built-in :class:`AgentSpec` registrations.

Imported for its side effect: every registration calls
:meth:`Registry.register` on :data:`default_registry` at module-import
time. Phase 1 shipped two entries (``thinking``, ``speaking``); Phase 2
adds six more flavors that mirror the existing SM dispatcher spawn map
(see :data:`alice_forge.dispatcher.constants.SPAWN_MAP`):

* ``code-worker`` — ``(sm:selected, art:code)`` pre-cutover v1
  claude-cli worker pool.
* ``research-writer`` — ``(sm:selected, art:research_note)`` and
  ``(sm:selected, art:experiment)`` one-shot vault writer.
* ``reviewer`` — ``(sm:reviewing, art:code)`` structured-JSON Sonnet
  sub-agent.
* ``designer`` — per-issue thinking-agent design lane
  (``(sm:selected, art:code)`` post-cutover). Variant of ``thinking``
  with per-issue lifecycle + ``design-ready`` exit-comment rule.
* ``watcher`` — GitHub issue watchers + cortex-memory cue runner.
  Read-only with the ``send_message`` channel; observations route
  through ``inner/notes/``.
* ``config-worker`` — ``(sm:selected, art:config_change)`` constrained
  diff worker. Smaller-diff + schema-aware threshold (<15 LOC, no
  schema change) per the user's SM tactical-threshold feedback.

The :class:`KernelSpec` templates here are deliberately conservative
defaults: model + tool list + max_seconds that match today's
in-process invocations. Per-call overrides remain the caller's job
(via :func:`dataclasses.replace` on the spec before
:meth:`AgentSpec.build_spec`); Phase 2 lifts that into a richer
runtime override mechanism.

Source-of-truth for the registered specs (post-Phase 4 of #194, #321):

* Behavioral rules ARE the source. Pre-Phase 4 each SPAWN_MAP row in
  :data:`alice_forge.dispatcher.constants.SPAWN_MAP` carried an inline
  ``instruction_trailer`` / ``system_prompt_role`` pair and the
  registered specs mirrored that prose. Phase 4 deleted those inline
  fields; the dispatcher's :func:`compose_spawn_prompt` now reads
  :meth:`AgentSpec.assembled_system_prompt` directly. Edit a rule
  here and the next dispatcher run sees it.
* The reviewer's structured-JSON contract comes from
  :data:`alice_speaking.review.code_reviewer.CODE_REVIEWER_SYSTEM_PROMPT`.
  We record the dotted reference as the :class:`OutputSchema` ``name``
  so a future validator can resolve it at registration time without
  leaking the import into ``core`` (which cannot import sibling
  packages — see ``tests/test_core_isolation.py``).
"""

from __future__ import annotations

from ..kernel import KernelSpec
from . import policies
from .registry import default_registry
from .types import AgentSpec, BehavioralRule, OutputSchema


__all__ = ["register_builtins"]


# Default model for both hemispheres — matches today's
# alice.config.json fallback (the real config can still override at
# call time by passing a different KernelSpec into :func:`run_agent`).
_DEFAULT_MODEL = "claude-opus-4-7"

# Reviewer runs on Sonnet (cheaper / faster than Opus, sufficient for
# structured-output verdicts). Mirrors
# :data:`alice_thinking.design_pipeline.DEFAULT_REVIEWER_MODEL` and the
# ``claude-agent-sdk:sonnet`` runtime label in the dispatcher SPAWN_MAP.
_REVIEWER_MODEL = "claude-sonnet-4-6"


_THINKING_RULES = (
    BehavioralRule(
        id="no-real-world-writes",
        injection=(
            "You may only read and write files inside ~/alice-mind/. "
            "Never modify the host filesystem, never install packages, "
            "never run destructive shell commands. Escalate any "
            "real-world change via inner/surface/."
        ),
    ),
    BehavioralRule(
        id="no-direct-signal",
        injection=(
            "Do not send Signal messages directly. If something needs "
            "to reach Jason or Katie, drop a fleeting note in "
            "inner/notes/ and let speaking decide whether to voice it."
        ),
    ),
    BehavioralRule(
        id="design-not-build",
        injection=(
            "Your job is to think, design, and groom — not to build. "
            "When a note surfaces a problem that needs code, write the "
            "proposal in the vault and let speaking dispatch the build."
        ),
    ),
)


_SPEAKING_RULES = (
    BehavioralRule(
        id="ask-before-external-action",
        injection=(
            "Ask Jason before taking actions that leave the machine and "
            "were not explicitly requested: emails, public posts, "
            "third-party uploads, irreversible config changes."
        ),
    ),
    BehavioralRule(
        id="signal-via-send-message",
        injection=(
            "Reach Signal only through the send_message MCP tool. Never "
            "shell out to signal-cli, notify, or curl — the daemon "
            "wraps quiet-hours + surface handling around send_message."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Phase 2 — behavioral-rule blocks
# ---------------------------------------------------------------------------


# code-worker rules — v1 claude-cli worker behavioral surface.
# Post-Phase 4 of #194 (#321), these rules are the SOLE source for
# the worker prompt body; :func:`compose_spawn_prompt` reads them via
# :meth:`AgentSpec.assembled_system_prompt`. Pre-cutover the rule set
# mirrored the inline ``instruction_trailer`` on the SPAWN_MAP row,
# which has since been deleted. (At sm:selected, ``art:code`` now
# routes to the per-issue designer/builder lanes — these rules apply
# wherever the v1 worker pool is still the spawn lane.)
_CODE_WORKER_RULES = (
    BehavioralRule(
        id="open-pr-closes-issue",
        injection=(
            "Open a pull request whose body contains "
            "``Closes #<issue_number>`` so the GitHub auto-link closes "
            "the issue when the PR merges. Use a conventional commit "
            "title; keep the body short and focused on the change."
        ),
    ),
    BehavioralRule(
        id="self-merge-on-green",
        injection=(
            "Self-merge the PR once CI is green. Don't wait for a "
            "human review — the dispatcher's reviewer sub-agent at "
            "sm:reviewing is the structured gate."
        ),
    ),
    BehavioralRule(
        id="no-verify-forbidden",
        injection=(
            "Never pass ``--no-verify`` to ``git commit`` or ``git "
            "push``. If a pre-commit / pre-push hook fails, fix the "
            "underlying issue and create a new commit — do not amend "
            "or skip hooks."
        ),
    ),
)


# research-writer rules — drive the ``art:research_note`` and
# ``art:experiment`` worker lanes. Post-Phase 4 of #194 (#321),
# :func:`compose_spawn_prompt` reads these rules verbatim; the
# previous mirrored ``instruction_trailer`` fields on the SPAWN_MAP
# rows have been deleted. Both flavors write a markdown note under
# ``cortex-memory/research/`` and post a ``[SM] transition
# from=selected to=done`` audit comment to drive the state machine
# forward.
_RESEARCH_WRITER_RULES = (
    BehavioralRule(
        id="write-research-note-under-vault",
        injection=(
            "Produce a research note at "
            "``~/alice-mind/cortex-memory/research/<date>-<slug>.md`` "
            "with YAML frontmatter. For ``art:experiment`` tasks the "
            "frontmatter must carry ``hypothesis``, ``null``, and "
            "``verdict`` keys."
        ),
    ),
    BehavioralRule(
        id="transition-to-done-on-completion",
        injection=(
            "When the note is committed, relabel the linked issue "
            "``sm:selected`` → ``sm:done`` and post an audit comment "
            "of the form ``[SM] transition from=selected to=done "
            "reason=\"research note at <path>\"``. The dispatcher "
            "uses that comment to close the issue."
        ),
    ),
)


# reviewer rules — strict JSON verdict, no prose. The full schema is
# carried via the :class:`OutputSchema` reference rather than inlined
# here so we don't drift from the canonical
# :data:`alice_speaking.review.code_reviewer.CODE_REVIEWER_SYSTEM_PROMPT`.
_REVIEWER_RULES = (
    BehavioralRule(
        id="strict-json-verdict",
        injection=(
            "Return a single STRICT JSON object matching the code "
            "reviewer schema (verdict / confidence / summary / "
            "feedback / patterns). No markdown fences, no prose "
            "outside the JSON object, no trailing commentary."
        ),
    ),
    BehavioralRule(
        id="verdict-gate",
        injection=(
            "``verdict: approved`` means the dispatcher will close the "
            "issue at sm:reviewing → sm:done; ``verdict: "
            "needs_revision`` means sm:reviewing → sm:building. Be "
            "deliberate — minor nits do not justify "
            "``needs_revision``."
        ),
    ),
)


# designer rules — per-issue thinking-agent variant. Inherits the
# thinking sandbox + design-not-build constraints; adds the
# ``[SM] design-ready`` exit comment contract from the SM v2 spec
# (see ``cortex-memory/research/2026-05-13-sm-v2-pipeline-revision.md``
# §3 and :data:`SPAWN_MAP[("sm:selected", "art:code")]`).
_DESIGNER_RULES = _THINKING_RULES + (
    BehavioralRule(
        id="design-ready-exit-comment",
        injection=(
            "When the design draft is ready, write it to "
            "``~/alice-mind/cortex-memory/designs/<date>-issue<N>-"
            "<slug>.md`` and post a ``[SM] design-ready "
            "note=[[<wikilink>]]`` comment on the issue to advance "
            "it to ``sm:design_review``."
        ),
    ),
)


# watcher rules — read + send_message only; observations and durable
# findings route through ``~/alice-mind/inner/notes/`` so thinking
# can process them on her next wake. Per the user's "notes-not-direct-
# action" feedback in CLAUDE.md (Memory Protocol § Speaking-side
# vault retrieval).
_WATCHER_RULES = (
    BehavioralRule(
        id="notes-not-direct-action",
        injection=(
            "You are a watcher, not an actor. Observations, "
            "concerns, and durable findings go into "
            "``~/alice-mind/inner/notes/`` as fleeting notes for "
            "thinking to process. Never edit code, never open PRs, "
            "never restart services — escalate via the note channel."
        ),
    ),
    BehavioralRule(
        id="signal-only-when-warranted",
        injection=(
            "Reach Signal via the ``send_message`` MCP tool only when "
            "the observation is time-sensitive and clearly worth "
            "interrupting Jason for. Default to writing a note."
        ),
    ),
)


# config-worker rules — smaller-diff + schema-aware threshold. Mirrors
# the user's SM tactical-threshold feedback: config_change tasks must
# stay under 15 LOC with no schema change, otherwise the work belongs
# in art:code (with a design pass) rather than the constrained
# config-worker lane.
_CONFIG_WORKER_RULES = (
    BehavioralRule(
        id="config-files-only",
        injection=(
            "Edit and Write only the config files explicitly named "
            "in the issue. Do not touch source code, tests, or "
            "documentation in the same change. If the fix requires "
            "broader edits, stop and escalate the issue back to "
            "``sm:draft`` for re-triage as ``art:code``."
        ),
    ),
    BehavioralRule(
        id="smaller-diff-threshold",
        injection=(
            "Keep the diff under 15 lines of code (added + removed). "
            "If the change would exceed that, or would alter a "
            "schema (config schema, JSON schema, API contract), stop "
            "and escalate — this lane is for tactical config tweaks "
            "only."
        ),
    ),
    BehavioralRule(
        id="open-pr-closes-issue",
        injection=(
            "Open a pull request whose body contains "
            "``Closes #<issue_number>`` and self-merge once CI is "
            "green. Never pass ``--no-verify``."
        ),
    ),
)


# ---------------------------------------------------------------------------
# KernelSpec templates
# ---------------------------------------------------------------------------


def _thinking_template() -> KernelSpec:
    return KernelSpec(
        model=_DEFAULT_MODEL,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Grep",
            "Glob",
            "WebFetch",
            "WebSearch",
            "mcp__alice__send_message",
            "mcp__alice__run_experiment",
        ],
        max_seconds=0,
        thinking="medium",
    )


def _speaking_template() -> KernelSpec:
    return KernelSpec(
        model=_DEFAULT_MODEL,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebFetch",
        ],
        max_seconds=0,
        thinking=None,
    )


def _code_worker_template() -> KernelSpec:
    """v1 code-worker — full SDK tool surface, no MCP. Matches what the
    claude-cli pool already gets when the dispatcher spawns a worker
    on ``(sm:selected, art:code)`` pre-cutover."""
    return KernelSpec(
        model=_DEFAULT_MODEL,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebFetch",
        ],
        max_seconds=0,
        thinking=None,
    )


def _research_writer_template() -> KernelSpec:
    """Research / experiment writer — vault writes + WebSearch for
    grounding claims (per the user's "ground claims outside vault"
    feedback). No MCP send_message — the dispatcher posts the
    transition comment, not the worker."""
    return KernelSpec(
        model=_DEFAULT_MODEL,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebFetch",
            "WebSearch",
        ],
        max_seconds=0,
        thinking="medium",
    )


def _reviewer_template() -> KernelSpec:
    """Sonnet code reviewer — read-side tools only. The reviewer
    consumes the PR diff in its user prompt; it does not need shell
    or filesystem mutation tools. ``append_system_prompt`` is set to
    a textual reference to the canonical
    :data:`CODE_REVIEWER_SYSTEM_PROMPT` so callers know which
    constant to inject at dispatch time without core importing
    alice_speaking."""
    return KernelSpec(
        model=_REVIEWER_MODEL,
        allowed_tools=[
            "Read",
            "Glob",
            "Grep",
        ],
        max_seconds=0,
        thinking=None,
        append_system_prompt=(
            "Inject the canonical reviewer prompt from "
            "``alice_speaking.review.code_reviewer:"
            "CODE_REVIEWER_SYSTEM_PROMPT`` at dispatch time."
        ),
    )


def _designer_template() -> KernelSpec:
    """Per-issue thinking-agent designer. Same tool surface as the
    long-lived thinking-agent so the designer can read the vault and
    write design drafts; the lifecycle / scope axes on the
    :class:`AgentSpec` mark it as a per-issue spawn rather than
    background."""
    return _thinking_template()


def _watcher_template() -> KernelSpec:
    """Watcher — read-side + ``send_message`` outbound. Matches the
    GitHub watcher and cortex-memory cue runner shape: scan, write a
    note (note write happens in the supervisor, not the agent), and
    optionally ping Signal when something is time-sensitive."""
    return KernelSpec(
        model=_DEFAULT_MODEL,
        allowed_tools=[
            "Read",
            "Glob",
            "Grep",
            "WebFetch",
            "WebSearch",
            "mcp__alice__send_message",
        ],
        max_seconds=0,
        thinking=None,
    )


def _config_worker_template() -> KernelSpec:
    """Config-worker — read + bash + file edits, no MCP. Constrained
    enough to encode "config-files only" intent at the tool layer;
    the path constraint and the 15-LOC threshold are enforced via
    behavioral rules."""
    return KernelSpec(
        model=_DEFAULT_MODEL,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
        ],
        max_seconds=0,
        thinking=None,
    )


# ---------------------------------------------------------------------------
# AgentSpec factories
# ---------------------------------------------------------------------------


def _thinking_spec() -> AgentSpec:
    return AgentSpec(
        name="thinking",
        persona="thinking",
        kernel_spec=_thinking_template(),
        runtime="claude-agent-sdk",
        scope="background",
        lifecycle="always-on",
        tool_policy=policies.full_access,
        behavioral_constraints=_THINKING_RULES,
    )


def _speaking_spec() -> AgentSpec:
    return AgentSpec(
        name="speaking",
        persona="speaking",
        kernel_spec=_speaking_template(),
        runtime="claude-agent-sdk",
        scope="conversational",
        lifecycle="always-on",
        tool_policy=policies.full_access,
        behavioral_constraints=_SPEAKING_RULES,
    )


def _code_worker_spec() -> AgentSpec:
    return AgentSpec(
        name="code-worker",
        persona="code-worker",
        kernel_spec=_code_worker_template(),
        runtime="claude-cli",
        scope="on-demand",
        lifecycle="per-issue",
        tool_policy=policies.full_access,
        behavioral_constraints=_CODE_WORKER_RULES,
    )


def _research_writer_spec() -> AgentSpec:
    return AgentSpec(
        name="research-writer",
        persona="research-writer",
        kernel_spec=_research_writer_template(),
        runtime="claude-cli",
        scope="on-demand",
        lifecycle="per-issue",
        tool_policy=policies.full_access,
        behavioral_constraints=_RESEARCH_WRITER_RULES,
    )


def _reviewer_spec() -> AgentSpec:
    return AgentSpec(
        name="reviewer",
        persona="reviewer",
        kernel_spec=_reviewer_template(),
        runtime="claude-agent-sdk",
        scope="on-demand",
        lifecycle="per-issue",
        tool_policy=policies.read_only,
        behavioral_constraints=_REVIEWER_RULES,
        # Phase 1 OutputSchema is a name-only placeholder; we record
        # the dotted path to the canonical schema constant so Phase 2+
        # validation can resolve it without core importing
        # alice_speaking. See module docstring.
        output_schema=OutputSchema(
            name=(
                "alice_speaking.review.code_reviewer:"
                "CODE_REVIEWER_SYSTEM_PROMPT"
            ),
        ),
    )


def _designer_spec() -> AgentSpec:
    return AgentSpec(
        name="designer",
        persona="thinking",
        kernel_spec=_designer_template(),
        runtime="claude-agent-sdk",
        scope="per-issue",
        lifecycle="per-issue",
        tool_policy=policies.full_access,
        behavioral_constraints=_DESIGNER_RULES,
    )


def _watcher_spec() -> AgentSpec:
    return AgentSpec(
        name="watcher",
        persona="watcher",
        kernel_spec=_watcher_template(),
        runtime="claude-agent-sdk",
        scope="background",
        lifecycle="always-on",
        tool_policy=policies.read_only_with_signal,
        behavioral_constraints=_WATCHER_RULES,
    )


def _config_worker_spec() -> AgentSpec:
    return AgentSpec(
        name="config-worker",
        persona="config-worker",
        kernel_spec=_config_worker_template(),
        runtime="claude-cli",
        scope="on-demand",
        lifecycle="per-issue",
        tool_policy=policies.config_writer,
        behavioral_constraints=_CONFIG_WORKER_RULES,
    )


def register_builtins(registry=default_registry) -> None:
    """Register the Phase 1 + Phase 2 built-in agents.

    Idempotent for the default singleton (re-registration is a no-op
    when the names are already present). Tests can pass a fresh
    :class:`Registry` to get an isolated population.
    """
    for spec in (
        _thinking_spec(),
        _speaking_spec(),
        _code_worker_spec(),
        _research_writer_spec(),
        _reviewer_spec(),
        _designer_spec(),
        _watcher_spec(),
        _config_worker_spec(),
    ):
        if spec.name in registry:
            continue
        registry.register(spec)


# Populate the module-level singleton on import. Tests that need an
# empty registry build their own :class:`Registry` instance.
register_builtins()
