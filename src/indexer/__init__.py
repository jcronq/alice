"""cortex_index — FTS5 + link-graph index over a markdown vault.

Public entry points:
    build_index.main() — rebuild the index from disk.
    yaml_lite.split_frontmatter() — minimal YAML frontmatter parser.
    yaml_lite.extract_wikilinks() — pull [[wikilink]] targets from a body.

Default vault location is ~/alice-mind/cortex-memory/ and the index DB is
written to ~/alice-mind/inner/state/cortex-index.db; both are overridable
via --vault and --db. The index is a derived projection of the vault — wipe
and rebuild produces identical state.
"""
