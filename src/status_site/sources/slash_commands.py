"""Status-site reference tab for Signal slash commands (issue #624).

Reads the :data:`alice_speaking.slash_commands.SLASH_COMMANDS` registry
and renders a one-page reference so Jason + Katie can see the current
command set + ACL at a glance without searching through chat history.

The tab renders through the generic A2UI surface framework the shell
already uses for ``page:resources`` — a single text component whose
``props.markdown`` carries the rendered body. Two sections:

1. **Overview** — one-liner describing the dispatcher's scope and the
   two conventions (``/verb`` prefix, ``//foo`` escape).
2. **Commands** — a markdown table grouped by category showing the
   verb, who can run it, and the one-line help text.

The renderer is pure — it only reads the compile-time registry — so it
has no DB/connection concerns and runs trivially in tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from status_site.a2ui import (
    component,
    create_surface,
    update_components,
)
from status_site.sources.common import staleness_footer


SURFACE_ID = "slash-commands"
SOURCE_NAME = "slash-commands-registry"


def _principals_label(acl) -> str:
    """Render the ACL tuple as a short principals list.

    ``"all"`` → ``everyone (Jason, Katie, others)`` for Overview copy;
    a tuple is shown verbatim. Lower-cased principal ids are rendered
    as display-cased words for readability.
    """
    if acl == "all":
        return "all principals"
    return ", ".join(sorted({str(p) for p in acl}))


def render_slash_commands_md(
    *,
    now: Optional[datetime] = None,
) -> str:
    """Build the full ``/slash-commands.md`` body.

    Reads the registry fresh on each call so a daemon hot-reload (new
    verbs added) is reflected on the next page refresh without a
    status-site restart.
    """
    try:
        from alice_speaking.slash_commands import SLASH_COMMANDS
    except Exception as exc:  # noqa: BLE001
        return (
            "# Signal slash commands\n\n"
            f"*(cannot load registry: `{exc}`)*\n\n"
            f"{staleness_footer(SOURCE_NAME, now=now)}\n"
        )

    header = (
        "# Signal slash commands\n\n"
        "Messages that begin with `/verb` are intercepted at the "
        "signal-ingest layer, BEFORE the normal Speaking turn pipeline. "
        "A literal `/` at the start of a legitimate chat message uses "
        "the `//foo` escape convention.\n\n"
        "Unknown verbs fall through as literal content — nothing is lost."
        "\n"
    )

    # Group verbs by category to match the way /help prints them.
    groups: dict[str, list] = {}
    for cmd in SLASH_COMMANDS:
        groups.setdefault(cmd.category, []).append(cmd)

    body: list[str] = [header]
    for category, cmds in groups.items():
        body.append(f"\n## {category.title()}\n")
        body.append("| Verb | Who | Description |\n")
        body.append("| --- | --- | --- |\n")
        for cmd in cmds:
            acl_label = _principals_label(cmd.acl)
            body.append(f"| `/{cmd.verb}` | {acl_label} | {cmd.help} |\n")

    body.append("\n")
    body.append(staleness_footer(SOURCE_NAME, now=now))
    body.append("\n")
    return "".join(body)


def build_envelopes(*, now: Optional[datetime] = None) -> list[dict]:
    """Wrap :func:`render_slash_commands_md` in an A2UI surface envelope.

    Mirrors ``sources/resources.build_envelopes``: a ``createSurface``
    opener followed by an ``updateComponents`` call whose single body
    component carries the rendered markdown.
    """
    md = render_slash_commands_md(now=now)
    return [
        create_surface(SURFACE_ID, data={}),
        update_components(
            SURFACE_ID,
            [
                component(
                    "root",
                    "list",
                    props={"variant": "slash-commands-page"},
                    children=["body"],
                ),
                component(
                    "body",
                    "text",
                    props={"markdown": md},
                ),
            ],
        ),
    ]


def render(cfg=None) -> dict:  # noqa: ARG001 — cfg accepted for section-renderer uniformity
    """Section renderer for ``page:slash-commands``."""
    return {"a2ui": build_envelopes()}
