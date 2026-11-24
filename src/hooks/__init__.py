"""Alice hook plumbing: periodic reapers + supporting utilities.

User-global hook *scripts* that fire on Claude Code events live under
``sandbox/claude-hooks/``; the pieces in this package are the repo-tested
periodic sweepers (e.g. :mod:`.orphan_reaper`) that run outside any Claude
session and so belong in the regular test suite.
"""
