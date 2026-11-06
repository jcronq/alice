"""Tests for the ported dashboard widget primitives (#649, slice-2).

The widget CSS (tokens + ``fx-*`` classes) is consolidated into a
single ``design-tokens.css`` file next to ``index.html``. These tests
guard the vendored shape:

* the file is served by the status-site server with ``text/css``
* ``index.html`` links it from its ``<head>``
* every CSS class the renderer / client emits actually exists
* every token the renderers reference exists in the stylesheet
* the shell HTML references the typed-widget components (slice-2)
"""

from __future__ import annotations

import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from status_site.server import (
    CONTENT_TYPES,
    STATIC_DIR,
    STATIC_FILES,
    Config,
    make_server,
)


# ─── static files: packaging + MIME ────────────────────────────────


def test_design_tokens_in_static_files_set():
    assert "design-tokens.css" in STATIC_FILES


def test_css_content_type_registered():
    assert CONTENT_TYPES[".css"].startswith("text/css")


def test_static_dir_actually_contains_design_tokens():
    tokens = STATIC_DIR / "design-tokens.css"
    assert tokens.is_file(), "design-tokens.css missing from static/"


def test_index_html_links_design_tokens_stylesheet():
    html = (STATIC_DIR / "index.html").read_text()
    assert 'href="design-tokens.css"' in html


# ─── token / class presence ────────────────────────────────────────


def _read_css() -> str:
    return (STATIC_DIR / "design-tokens.css").read_text()


@pytest.mark.parametrize(
    "token",
    [
        "--color-bg",
        "--color-panel",
        "--color-fg",
        "--color-fg-muted",
        "--color-border-line",
        "--color-ok",
        "--color-warn",
        "--color-bad",
        "--font-sans",
        "--font-serif",
        "--dash-gap-xs",
        "--dash-gap-sm",
        "--dash-gap-md",
        "--dash-gap-lg",
        "--dash-pad-xs",
        "--dash-pad-sm",
        "--dash-pad-lg",
        "--dash-radius",
    ],
)
def test_design_token_present(token: str):
    assert token in _read_css()


def test_root_selector_present():
    assert ":root {" in _read_css()


def test_light_and_dark_modes_both_defined():
    css = _read_css()
    # Light palette lives on the top-level :root; dark via media + attr.
    assert "prefers-color-scheme: dark" in css
    assert ':root[data-theme="dark"]' in css


def test_tabular_numerics_utility_defined():
    css = _read_css()
    assert "font-variant-numeric: tabular-nums" in css


@pytest.mark.parametrize(
    "widget_class",
    [
        ".fx-card",
        ".fx-ms",
        ".fx-msrow",
        ".fx-tbl",
        ".fx-bar",
        ".fx-pill",
        ".fx-pill.blocked",
        ".fx-pill.done",
        ".fx-pill.active",
        ".fx-urg",
        ".fx-urg.NOW",
        ".fx-urg.UNSET",
        ".fx-urg.TODAY",
        ".fx-urg.WHEN",
        ".fx-urg.FYI",
        ".fx-mark",
        ".fx-mark.blocked",
        ".fx-mark.queued",
        ".fx-tile",
        ".fx-stamp",
        ".fx-pill.freshness-tile",
    ],
)
def test_widget_class_present(widget_class: str):
    assert widget_class in _read_css()


@pytest.mark.parametrize(
    "fresh_state",
    ["ok", "amber", "red", "owed"],
)
def test_data_fresh_rule_covers_each_state(fresh_state: str):
    assert f'[data-fresh="{fresh_state}"]' in _read_css()


# ─── HTML: the shell references the typed-widget components ────────


def test_index_html_references_typed_widgets():
    """A future accidental revert back to a markdown-only shell should
    fail this smoke test — the typed widget surface is what slice-2
    ships.
    """
    body = (STATIC_DIR / "index.html").read_bytes()
    assert b"RunningNowBoard" in body or b"FxTable" in body
    # Theme toggle + freshness tile are also slice-2 primitives.
    assert b"data-theme" in body
    assert b"FreshnessTile" in body or b"fx-tile" in body


# ─── HTTP: the server actually serves the CSS ──────────────────────


@pytest.fixture
def status_cfg(tmp_path: Path) -> Config:
    status = tmp_path / "status"
    status.mkdir()
    surface = tmp_path / "surface"
    surface.mkdir()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (tmp_path / "key").write_text("test-key")
    return Config(
        status_dir=status,
        surface_dir=surface,
        upload_dir=uploads,
        key="test-key",
        port=0,
    )


def _serve_once(cfg: Config) -> int:
    srv = make_server(cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def test_server_returns_css_body_and_mime(status_cfg):
    port = _serve_once(status_cfg)
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/design-tokens.css")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/css")
    # Smoke: the body is non-empty and carries both halves of the
    # consolidated stylesheet (tokens + widget classes).
    assert ":root {" in body
    assert "--font-serif" in body
    assert "--color-ok" in body
    assert ".fx-tile" in body
    assert ".fx-urg" in body
    assert ".fx-pill" in body
    assert "data-fresh" in body
    assert '[data-theme="dark"]' in body
