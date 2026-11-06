"""Tests for the ported dashboard widget primitives (#641).

The static CSS files (``design-tokens.css``, ``widgets.css``) are
copied verbatim from ``agentry-dashboard/dashboard/template.html``
and carried alongside ``index.html`` in the packaged frontend. These
tests guard the vendored shape:

* the files are served by the status-site server
* ``index.html`` links both files from its ``<head>``
* every CSS class the renderer emits actually exists in widgets.css
* every token the renderers reference exists in design-tokens.css
"""

from __future__ import annotations

import io
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from status_site.server import (
    STATIC_DIR,
    STATIC_FILES,
    CONTENT_TYPES,
    Config,
    make_server,
)


# ─── static files: packaging + MIME ────────────────────────────────


def test_design_tokens_and_widgets_in_static_files_set():
    assert "design-tokens.css" in STATIC_FILES
    assert "widgets.css" in STATIC_FILES


def test_css_content_type_registered():
    assert CONTENT_TYPES[".css"].startswith("text/css")


def test_static_dir_actually_contains_both_files():
    tokens = STATIC_DIR / "design-tokens.css"
    widgets = STATIC_DIR / "widgets.css"
    assert tokens.is_file(), "design-tokens.css missing from static/"
    assert widgets.is_file(), "widgets.css missing from static/"


def test_index_html_links_both_stylesheets():
    html = (STATIC_DIR / "index.html").read_text()
    assert 'href="design-tokens.css"' in html
    assert 'href="widgets.css"' in html


# ─── token / class presence ────────────────────────────────────────


def _read_tokens_css() -> str:
    return (STATIC_DIR / "design-tokens.css").read_text()


def _read_widgets_css() -> str:
    return (STATIC_DIR / "widgets.css").read_text()


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
    assert token in _read_tokens_css()


def test_light_and_dark_modes_both_defined():
    css = _read_tokens_css()
    # Light palette lives on the top-level :root; dark via media + attr.
    assert "prefers-color-scheme: dark" in css
    assert ':root[data-theme="dark"]' in css


def test_tabular_numerics_utility_defined():
    css = _read_tokens_css()
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
    assert widget_class in _read_widgets_css()


@pytest.mark.parametrize(
    "fresh_state",
    ["ok", "amber", "red", "owed"],
)
def test_data_fresh_rule_covers_each_state(fresh_state: str):
    assert f'[data-fresh="{fresh_state}"]' in _read_widgets_css()


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


@pytest.mark.parametrize("name", ["design-tokens.css", "widgets.css"])
def test_server_returns_css_body_and_mime(status_cfg, name):
    port = _serve_once(status_cfg)
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"/{name}")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/css")
    # Smoke: the body is non-empty and has the expected shape.
    if name == "design-tokens.css":
        assert ":root" in body
        assert "--color-bg" in body
    else:
        assert ".fx-card" in body
        assert ".fx-pill" in body
