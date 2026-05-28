"""Issue #424: cheap liveness probe for the container HEALTHCHECK.

The Docker HEALTHCHECK in ``sandbox/Dockerfile`` used to curl ``/``,
which on macOS/Rancher-Desktop's virtiofs bind mount took ~5s on the
first paint (uvicorn warmup + jinja template compile + reading mind
state through the slow mount) and tripped the 3s probe wall every
retry. ``/healthz`` is the no-I/O replacement: it returns 200 without
loading events, touching the vault, or rendering a template, so the
probe is bounded by uvicorn scheduling instead of cold-cache latency.

These tests pin the contract so a future refactor can't silently put
mind-state reads back on the liveness path.
"""

from __future__ import annotations

import pathlib
from unittest import mock

from fastapi.testclient import TestClient

from viewer import main as viewer_main
from viewer.main import create_app
from viewer.settings import Paths


def _make_paths(tmp_path: pathlib.Path) -> Paths:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "thinking.log").write_text("")
    (state / "speaking.log").write_text("")
    inner_state = tmp_path / "inner" / "state"
    inner_state.mkdir(parents=True, exist_ok=True)
    (inner_state / "speaking-turns.jsonl").write_text("")
    return Paths(
        thinking_log=state / "thinking.log",
        speaking_log=state / "speaking.log",
        turn_log=inner_state / "speaking-turns.jsonl",
        mind_dir=tmp_path,
        state_dir=state,
    )


def test_healthz_returns_200_ok(tmp_path: pathlib.Path) -> None:
    app = create_app(_make_paths(tmp_path))
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    # Body should be a short, fixed string — no template render.
    assert r.text == "ok"


def test_healthz_does_not_touch_mind_state(tmp_path: pathlib.Path) -> None:
    """The whole point of /healthz is that the probe doesn't read the
    vault. ``sources.load_all`` is the entry point for every heavy
    view; if /healthz ever calls it (directly or through a helper) the
    macOS/virtiofs cold-paint problem returns. Patch it and assert it
    stays untouched across a probe."""
    app = create_app(_make_paths(tmp_path))
    client = TestClient(app)
    with mock.patch.object(
        viewer_main.sources, "load_all", autospec=True
    ) as load_all:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert load_all.call_count == 0


def test_healthz_survives_missing_state_dir(tmp_path: pathlib.Path) -> None:
    """A probe that requires the state dir to exist defeats the
    purpose of having a probe at all — uvicorn could be up before the
    state dir is mounted on a slow container start. The route must
    answer 200 even with an unconfigured mind directory."""
    # Build paths that point at nonexistent files; create_app already
    # handles missing personae.yml, so the app should construct and
    # the probe should still answer.
    bogus = tmp_path / "does-not-exist"
    paths = Paths(
        thinking_log=bogus / "thinking.log",
        speaking_log=bogus / "speaking.log",
        turn_log=bogus / "speaking-turns.jsonl",
        mind_dir=bogus,
        state_dir=bogus,
    )
    app = create_app(paths)
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"
