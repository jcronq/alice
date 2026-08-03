"""Regression tests for the lint-safe frontmatter serializer (task-0617).

Backstory: memory-worker autopush silently failed for 6+ days
(2026-07-27 → 2026-08-02) after ``feat/markdown-lint-precommit`` merged.
The pre-commit lint (``scripts/lint_markdown.py``) rejected the entire
autopush batch on every attempt because memory-worker's frontmatter
serializers produced YAML-invalid output under three patterns:

1. Unquoted scalar values containing an internal ``:``.
2. Trailing value glued to the closing ``---`` fence.
3. Empty list entries emitted as bare ``-`` markers.

These tests pin the fix at both layers:

- :mod:`alice_thinking._frontmatter` (the shared safe helpers).
- The four production serializer sites that consume it (stage_b's
  ``_serialize_frontmatter``, stage_c/stage_d's ``_frontmatter_render``,
  correction_cascade_auto_propagate's ``_write_updated_frontmatter``,
  and phase's ``_write_frontmatter_fields``).

Each site's test asserts the emitted YAML round-trips through
:func:`yaml.safe_load`, which is exactly what ``scripts/lint_markdown.py``
runs at commit time.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from alice_thinking._frontmatter import (
    filter_list_items,
    quote_if_needed,
    render_frontmatter,
    render_kv,
)


# ---------------------------------------------------------------------------
# Shared helpers — the primitive safety guarantees
# ---------------------------------------------------------------------------


class TestQuoteIfNeeded:
    """Bug 1: unquoted scalar values containing an internal ``:``."""

    def test_quotes_title_with_colon(self) -> None:
        # Real-world exemplar from cortex-memory/reference/hub-decay-mechanism.md
        # line 3 that broke the 2026-07-27 autopush.
        raw = "Meta-research: decay mechanism & scoring"
        quoted = quote_if_needed(raw)
        assert quoted != raw, "value with internal ':' must be quoted"
        # Round-trip through the parser must yield back the original string.
        assert yaml.safe_load(f"title: {quoted}") == {"title": raw}

    def test_quotes_multiple_colons(self) -> None:
        raw = "value: with: multi: colons"
        quoted = quote_if_needed(raw)
        assert yaml.safe_load(f"key: {quoted}") == {"key": raw}

    def test_leaves_plain_string_unquoted(self) -> None:
        assert quote_if_needed("meta-research") == "meta-research"

    def test_leaves_date_unquoted(self) -> None:
        # Dates round-trip to a datetime.date via yaml.safe_load, which is a
        # scalar (not a dict), so we deliberately leave them alone. The
        # lint check only fails on YAMLError or nested-mapping reparse.
        assert quote_if_needed("2026-05-08") == "2026-05-08"

    def test_leaves_comma_string_unquoted(self) -> None:
        assert quote_if_needed("complete, inferred") == "complete, inferred"

    def test_leaves_wikilink_unquoted(self) -> None:
        # A bare "[[a]]" reparses as nested flow-lists (not a mapping),
        # so we don't force-quote it. Callers that need list semantics
        # go through render_kv's flow-list path, which does quote.
        assert quote_if_needed("[[a]]") == "[[a]]"


class TestFilterListItems:
    """Bug 3 defense: bare ``-`` from empty list entries."""

    def test_drops_empty_strings(self) -> None:
        assert filter_list_items(["a", "", "b"]) == ["a", "b"]

    def test_drops_whitespace_only(self) -> None:
        assert filter_list_items(["a", "   ", "\t\n", "b"]) == ["a", "b"]

    def test_drops_none(self) -> None:
        assert filter_list_items(["a", None, "b"]) == ["a", "b"]

    def test_preserves_nonempty(self) -> None:
        assert filter_list_items(["[[a]]", "[[b]]"]) == ["[[a]]", "[[b]]"]


class TestRenderFrontmatter:
    """Structural guarantees of the batteries-included renderer."""

    def test_bug1_title_with_colon_is_lint_clean(self) -> None:
        """Bug 1 regression: title containing ``:`` must round-trip."""
        fm = {
            "slug": "hub-decay-mechanism",
            "title": "Meta-research: decay mechanism & scoring",
            "access_count": 8,
        }
        rendered = render_frontmatter(fm)
        # Extract the frontmatter body (between the two --- fences) and
        # feed it to yaml.safe_load exactly like scripts/lint_markdown.py
        # does. No YAMLError => lint would pass.
        inner = _extract_inner(rendered)
        parsed = yaml.safe_load(inner)
        assert parsed["title"] == "Meta-research: decay mechanism & scoring"

    def test_bug2_newline_before_closing_fence(self) -> None:
        """Bug 2 regression: last value must never glue to closing ``---``.

        The exemplar was ``access_count: 8---`` in
        ``cortex-memory/reference/hub-vault-health.md`` line 12.
        """
        fm = {"key": "value", "access_count": 8}
        rendered = render_frontmatter(fm)
        # The closing fence lives on its own line: last value + \n + ---.
        assert "\n---\n" in rendered
        assert "8---" not in rendered
        # And the block re-parses cleanly.
        assert yaml.safe_load(_extract_inner(rendered)) == {
            "key": "value",
            "access_count": 8,
        }

    def test_bug3_empty_list_entries_dropped(self) -> None:
        """Bug 3 regression: empty entries in a list must not render as bare ``-``.

        The exemplar was a stray ``-`` on line 17 of
        ``cortex-memory/research/2026-05-08-post-midnight-wake-count-bug.md``,
        appearing between the ``references:`` block-list and the closing
        ``---`` fence.
        """
        fm = {
            "references": [
                "[[dark-cluster-phase2-hub-design]]",
                "[[2026-06-07-decay-recovery-intervention-synthesis]]",
                "",  # stray empty entry — the bug 3 pattern
            ]
        }
        rendered = render_frontmatter(fm, list_style="block")
        # No line consists of just "-" or "  - " (bare dash markers).
        for line in rendered.splitlines():
            assert line.strip() != "-", f"stray bare dash in: {line!r}"
        # Block-style rendering re-parses cleanly.
        parsed = yaml.safe_load(_extract_inner(rendered))
        assert parsed["references"] == [
            "[[dark-cluster-phase2-hub-design]]",
            "[[2026-06-07-decay-recovery-intervention-synthesis]]",
        ]

    def test_preferred_key_order(self) -> None:
        fm = {"z": 1, "a": 2, "title": "hi"}
        rendered = render_frontmatter(fm, preferred_keys=("title", "a"))
        lines = rendered.splitlines()
        # title first, then a, then z. Fences bracket them.
        assert lines[0] == "---"
        assert lines[1].startswith("title:")
        assert lines[2].startswith("a:")
        assert lines[3].startswith("z:")
        assert lines[4] == "---"

    def test_none_values_are_dropped(self) -> None:
        fm = {"keep": "yes", "drop": None}
        rendered = render_frontmatter(fm)
        assert "drop" not in rendered
        assert "keep: yes" in rendered

    def test_flow_list_items_with_commas_are_quoted(self) -> None:
        # If we bare-emit ``[complete, inferred]`` as a single item, the
        # flow-list parser splits it into two items. Test that items with
        # embedded commas force quoting inside the flow rendering.
        fm = {"tags": ["one, two", "three"]}
        rendered = render_frontmatter(fm)
        parsed = yaml.safe_load(_extract_inner(rendered))
        assert parsed["tags"] == ["one, two", "three"]


class TestRenderKv:
    def test_flow_style_is_default(self) -> None:
        assert render_kv("tags", ["a", "b"]) == ["tags: [a, b]"]

    def test_block_style(self) -> None:
        # Wikilinks start with ``[`` which YAML would nest-parse as a
        # flow list, so the renderer quotes them defensively. Bare
        # slugs (no leading bracket) stay unquoted.
        assert render_kv("tags", ["hub", "meta"], list_style="block") == [
            "tags:",
            "  - hub",
            "  - meta",
        ]
        lines = render_kv("refs", ["[[a]]", "[[b]]"], list_style="block")
        assert lines[0] == "refs:"
        # Two block items, each round-trips to the wikilink string.
        parsed = yaml.safe_load("\n".join(lines))
        assert parsed["refs"] == ["[[a]]", "[[b]]"]

    def test_bool_scalar(self) -> None:
        assert render_kv("done", True) == ["done: true"]
        assert render_kv("done", False) == ["done: false"]

    def test_scalar_with_colon_quoted(self) -> None:
        [line] = render_kv("title", "Meta-research: decay")
        # Round-trip check is the invariant, not the specific quote style.
        assert yaml.safe_load(line)["title"] == "Meta-research: decay"


# ---------------------------------------------------------------------------
# Per-site tests — each production serializer must emit lint-clean YAML
# ---------------------------------------------------------------------------


def _extract_inner(rendered: str) -> str:
    """Pull out just the YAML body between the ``---`` fences."""
    lines = rendered.splitlines()
    assert lines[0] == "---", f"expected opening fence, got {lines[0]!r}"
    end = lines.index("---", 1)
    return "\n".join(lines[1:end])


def _assert_lint_clean(text: str) -> None:
    """Assert the rendered text passes the pre-commit lint check.

    Mirrors ``scripts/lint_markdown.lint_frontmatter``: yaml.safe_load
    over the frontmatter body, YAMLError => fail.
    """
    lines = text.splitlines()
    assert lines[0] == "---", f"expected opening fence, got {lines[0]!r}"
    try:
        end = lines.index("---", 1)
    except ValueError:  # pragma: no cover — defensive
        pytest.fail("no closing frontmatter fence")
    inner = "\n".join(lines[1:end])
    yaml.safe_load(inner)  # raises YAMLError on lint failure


class TestStageBSerializer:
    def test_bug1_apply_diff_writes_lint_clean_frontmatter(
        self, tmp_path: pathlib.Path
    ) -> None:
        """``apply_diff`` writing a title with an internal ``:`` used to
        produce ``title: Meta-research: decay mechanism & scoring`` —
        YAML-invalid. Now it round-trips.
        """
        from alice_thinking.workflows.stage_b import (
            Diff,
            FrontmatterChange,
        )
        from alice_thinking.workflows.stage_b import steps as steps_mod

        target = tmp_path / "hub.md"
        target.write_text(
            "---\ntitle: old\n---\n\nbody\n", encoding="utf-8"
        )
        diff = Diff(
            frontmatter_changes=[
                FrontmatterChange(
                    key="title",
                    new_value="Meta-research: decay mechanism & scoring",
                )
            ]
        )
        assert steps_mod.apply_diff(target, diff) is True
        _assert_lint_clean(target.read_text())

    def test_bug2_serializer_always_newlines_before_closing_fence(self) -> None:
        """``_serialize_frontmatter`` must never glue the last value to
        the closing ``---`` (produces ``access_count: 8---``).
        """
        from alice_thinking.workflows.stage_b import steps as steps_mod

        rendered = steps_mod._serialize_frontmatter(
            {"key": "value", "access_count": "8"}
        )
        assert "\n---\n" in rendered
        assert "8---" not in rendered


class TestStageCSerializer:
    def test_bug1_atomize_title_with_colon(self) -> None:
        """Stage C's ``_frontmatter_render`` used to bare-emit any title,
        so an atomize pass could produce ``title: Foo: bar``.
        """
        from alice_thinking.memory_worker import stage_c

        rendered = stage_c._frontmatter_render(
            {
                "title": "Meta-research: decay mechanism & scoring",
                "tags": ["hub"],
                "access_count": 8,
            }
        )
        _assert_lint_clean(rendered)

    def test_preserves_tag_flow_list_shape(self) -> None:
        """Regression guard: don't change unrelated formatting."""
        from alice_thinking.memory_worker import stage_c

        rendered = stage_c._frontmatter_render(
            {"tags": ["meta-research", "hub", "vault-health"]}
        )
        assert "tags: [meta-research, hub, vault-health]" in rendered


class TestStageDSerializer:
    def test_bug1_synthesis_note_title_with_colon(self) -> None:
        from alice_thinking.memory_worker import stage_d

        rendered = stage_d._render_frontmatter(
            {
                "title": "Recombination: X × Y",
                "tags": ["recombination"],
            }
        )
        _assert_lint_clean(rendered)


class TestCorrectionCascadeAutoPropagate:
    def test_bug3_empty_reference_entry_filtered(self) -> None:
        """The block-style ``references:`` rendering must drop empty items."""
        from alice_thinking.memory_worker import correction_cascade_auto_propagate as ccap

        # Feed a fresh dict — the function mutates its input to bump `updated`.
        fm = {
            "slug": "example",
            "references": [
                "[[a]]",
                "",  # empty entry — the bug 3 pattern
                "[[b]]",
            ],
        }
        rendered = ccap._write_updated_frontmatter(fm, "body\n")
        for line in rendered.splitlines():
            assert line.strip() != "-", (
                f"stray bare dash in serializer output: {line!r}"
            )
        _assert_lint_clean(rendered)


class TestPhaseWriteFrontmatterFields:
    def test_bug1_conflict_note_value_with_colon(self) -> None:
        """``_write_frontmatter_fields`` must quote scalars with ``:``."""
        from alice_thinking import phase

        original = "---\nstatus: open\n---\n\nbody\n"
        updated = phase._write_frontmatter_fields(
            original,
            updates={
                "naming_decision": (
                    "2026-05-20 09:43 EDT — Jason picked stage-b renaming"
                )
            },
        )
        _assert_lint_clean(updated)

    def test_no_frontmatter_prepends_fresh_block(self) -> None:
        from alice_thinking import phase

        updated = phase._write_frontmatter_fields(
            "body without frontmatter\n",
            updates={"title": "New: with colon"},
        )
        _assert_lint_clean(updated)
        assert "body without frontmatter" in updated
