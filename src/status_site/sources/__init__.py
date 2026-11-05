"""Per-section source renderers for the status-site Overview.

Each module exposes a pure ``render(...)`` function returning the region
body (no heading, no markers). The server's ``GET /index.md`` handler
registers each renderer in a ``SpliceSection`` tuple and splices the
output between sentinel marker comments.

All renderers end their output with a single italic staleness-footer
line of the form::

    *last_rendered: 2026-11-05T18:32:07-04:00 · source: <source-name>*

The frontend post-render hook parses that line and adds a ``.stale`` CSS
class to the preceding ``h2`` when the delta exceeds the staleness
threshold (see ``static/index.html``). Hand-edit zone renderers don't
emit this footer — they're verbatim prose, not polled.

See ``docs/designs/status-site.md`` and
``cortex-memory/designs/2026-11-05-issue638-overview-auto-render.md``
for the full contract.
"""
