"""Permission-rules generator for the experiment subagent.

The subprocessed ``claude`` CLI takes a ``--permission-rules-file`` whose
contents constrain what the subagent can do. We generate this per-experiment
so the writable repo copy (``/tmp/alice-copy-<id>/``) only opens up when
``repo_under_test`` is set.

Design rules (folded from the v2 spec + review concerns):

- Bash: allow with deny-list. Deny invocations that leave the sandbox or
  could recursively spawn another agent. The recursion guardrail (Concern 3)
  is the explicit ``claude`` deny.
- File ops (Read / Write / Edit / Glob / Grep): allowed under ``/tmp/`` and
  ``~/alice-mind/`` (vault is the canonical write target for the card path,
  even though the card is written by the runner — the subagent still needs
  to walk vault for prior art / context).
- Read-only on ``/home/alice/alice/`` (the real repo) so prior art lookups
  work without putting the actual codebase at risk.
- Read-write on ``/tmp/alice-copy-<id>/`` only when the experiment passed a
  ``repo_under_test`` (Concern 1).
- No MCP tools other than ``submit_result``. The subagent's MCP server is
  intentionally scoped down — the runner adds ``submit_result`` separately.

Format: the ``claude`` CLI loads settings via ``--settings <file-or-json>``.
The settings JSON has a ``permissions`` object with two lists, ``allow``
and ``deny``. Each rule is a string of the form ``<ToolName>`` for
blanket allow/deny, or ``<ToolName>(<argument-pattern>)`` for
argument-restricted rules. Bash arguments are matched as shell-command
prefixes; File-tool arguments are path prefixes. See
``https://docs.claude.com/en/docs/claude-code/iam`` for the canonical
shape.

The spec calls this the "permission-rules file" — same content, different
CLI flag name in practice. We use the term internally to match the spec.

The rules are stable and deterministic so the unit tests can pin the deny
set exactly.
"""

from __future__ import annotations

import json
import pathlib
from typing import Optional


__all__ = [
    "DENY_BASH_PREFIXES",
    "ALLOW_FILE_PATH_PREFIXES",
    "READ_ONLY_PATH_PREFIXES",
    "FILE_TOOLS",
    "generate_permission_rules",
    "render_rules_dict",
]


# Bash command prefixes the subagent MUST NOT invoke. These are matched by
# the ``claude`` CLI as command-line prefixes — e.g. a deny rule
# ``Bash(claude:*)`` covers both ``claude`` and ``claude --child ...``. The
# explicit ``claude`` entry is the structural recursion guardrail
# (Concern 3 in the review).
DENY_BASH_PREFIXES: tuple[str, ...] = (
    "claude",
    "git push",
    "signal-cli",
    "curl",
    "docker",
    "sudo",
    "ssh",
)


# Read/Write/Edit/Glob/Grep allowed under these path prefixes. ``/tmp/`` for
# the ephemeral working dir + the writable repo copy; the vault so prior
# art and context paths resolve.
ALLOW_FILE_PATH_PREFIXES: tuple[str, ...] = (
    "/tmp/",
    "/home/alice/alice-mind/",
)


# Real repo lives here. Read-only — the subagent can browse code for context
# but a writable copy at /tmp/alice-copy-<id>/ is the only path it can
# modify (and only if repo_under_test is set).
READ_ONLY_PATH_PREFIXES: tuple[str, ...] = (
    "/home/alice/alice/",
)


# Tool names that take path arguments. These get path-prefix-restricted
# rules; Bash gets command-prefix rules (above). Glob/Grep are read-only by
# nature so they're listed for read but never for write.
FILE_TOOLS_READ_WRITE: tuple[str, ...] = ("Read", "Write", "Edit")
FILE_TOOLS_READ_ONLY: tuple[str, ...] = ("Glob", "Grep")
FILE_TOOLS: tuple[str, ...] = FILE_TOOLS_READ_WRITE + FILE_TOOLS_READ_ONLY


def render_rules_dict(
    *, writable_repo_copy: Optional[pathlib.Path] = None
) -> dict[str, list[str]]:
    """Build the ``{"allow": [...], "deny": [...]}`` dict the CLI expects.

    Pure / deterministic so tests can pin the exact deny list. If
    ``writable_repo_copy`` is provided, Read/Write/Edit get rules for that
    path on top of the standard /tmp + alice-mind allow.
    """
    allow: list[str] = []
    deny: list[str] = []

    # Blanket-allow Bash; deny entries below carve out the restricted prefixes.
    # The CLI's matcher treats ``Bash(prefix:*)`` as "any command starting
    # with prefix" — this is how we deny e.g. ``git push`` without banning
    # all of git.
    allow.append("Bash")
    for prefix in DENY_BASH_PREFIXES:
        deny.append(f"Bash({prefix}:*)")

    # File-tool path allowlists. The CLI matches the first argument
    # path-prefix-style (see iam docs). We list every allowed prefix
    # per tool so the rule set is explicit.
    for tool in FILE_TOOLS_READ_WRITE:
        for prefix in ALLOW_FILE_PATH_PREFIXES:
            allow.append(f"{tool}({prefix}**)")
        if writable_repo_copy is not None:
            # Trailing slash + ** matches any descendant. The runner is
            # responsible for ensuring the path actually exists; the rule
            # is purely a permission gate.
            copy_str = str(writable_repo_copy).rstrip("/") + "/"
            allow.append(f"{tool}({copy_str}**)")
        # Read-only allowance for the real repo on Read-class tools only;
        # Write/Edit are denied below (see READ_ONLY_PATH_PREFIXES handling).
        if tool == "Read":
            for prefix in READ_ONLY_PATH_PREFIXES:
                allow.append(f"{tool}({prefix}**)")

    # Read-only tools (Glob/Grep): the standard allowlist + the real repo
    # read-only path. No deny needed — these can't mutate anything.
    for tool in FILE_TOOLS_READ_ONLY:
        for prefix in ALLOW_FILE_PATH_PREFIXES:
            allow.append(f"{tool}({prefix}**)")
        for prefix in READ_ONLY_PATH_PREFIXES:
            allow.append(f"{tool}({prefix}**)")
        if writable_repo_copy is not None:
            copy_str = str(writable_repo_copy).rstrip("/") + "/"
            allow.append(f"{tool}({copy_str}**)")

    # Explicit deny: Write/Edit on the real repo. The allow rules above
    # don't cover it, but a future loosening of the allow set shouldn't
    # silently unlock writes to ``/home/alice/alice/``. Belt-and-braces.
    for tool in ("Write", "Edit"):
        for prefix in READ_ONLY_PATH_PREFIXES:
            deny.append(f"{tool}({prefix}**)")

    return {"allow": allow, "deny": deny}


def generate_permission_rules(
    target_path: pathlib.Path,
    *,
    writable_repo_copy: Optional[pathlib.Path] = None,
) -> pathlib.Path:
    """Render the permission-rules JSON file for one experiment.

    The output shape is the ``--settings`` JSON the ``claude`` CLI loads:
    ``{"permissions": {"allow": [...], "deny": [...]}}``. The bare rules
    dict (``render_rules_dict``) is the inner ``permissions`` payload —
    we wrap it here so the CLI accepts the file directly.

    Writes to ``target_path`` and returns it. The caller is responsible for
    cleanup (the runner GC'd this alongside ``/tmp/alice-copy-<id>/``).
    """
    rules = render_rules_dict(writable_repo_copy=writable_repo_copy)
    settings = {"permissions": rules}
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(settings, indent=2))
    return target_path
