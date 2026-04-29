"""cortex_index — FTS5 + link-graph index over the cortex-memory vault.

Public entry points:
    build_index.main() — rebuild the index from disk.
    yaml_lite.parse() — minimal YAML frontmatter parser.

The index file lives at ~/alice-mind/cortex-memory/.index/cortex.db (sqlite),
and is rebuilt on demand. Both hemispheres may query it; only thinking writes.
"""
