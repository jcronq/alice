"""Tests for the viewer.main:run uvicorn entrypoint (#130).

The viewer container's CMD calls ``python -m viewer.main`` which
falls through to ``run()``. The container had been caching its FastAPI
route table at startup, so a PR that added/moved a route only took
effect after a manual ``docker compose restart alice-viewer``. The fix
wires ``ALICE_VIEWER_RELOAD=1`` (set in sandbox/docker-compose.yml) to
uvicorn's ``reload`` flag, scoped via ``reload_dirs`` to just the
``src/viewer/`` tree so unrelated repo edits don't churn.
"""

from __future__ import annotations

from unittest import mock

from viewer import main as viewer_main


def test_run_no_reload_when_env_unset(monkeypatch):
    monkeypatch.delenv("ALICE_VIEWER_RELOAD", raising=False)
    monkeypatch.delenv("ALICE_VIEWER_HOST", raising=False)
    monkeypatch.delenv("ALICE_VIEWER_PORT", raising=False)

    fake_uvicorn = mock.MagicMock()
    with mock.patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
        viewer_main.run()

    fake_uvicorn.run.assert_called_once()
    args, kwargs = fake_uvicorn.run.call_args
    assert args == ("viewer.main:create_app",)
    assert kwargs["reload"] is False
    assert kwargs["factory"] is True
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 7777
    # When reload is off, reload_dirs must not be passed (uvicorn rejects
    # it without reload) — see uvicorn.config.Config.__init__.
    assert "reload_dirs" not in kwargs


def test_run_reload_scoped_to_viewer_dir(monkeypatch):
    monkeypatch.setenv("ALICE_VIEWER_RELOAD", "1")
    monkeypatch.setenv("ALICE_VIEWER_HOST", "127.0.0.1")
    monkeypatch.setenv("ALICE_VIEWER_PORT", "8888")

    fake_uvicorn = mock.MagicMock()
    with mock.patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
        viewer_main.run()

    args, kwargs = fake_uvicorn.run.call_args
    assert args == ("viewer.main:create_app",)
    assert kwargs["reload"] is True
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8888
    # The watcher must be scoped to the viewer package dir, NOT the
    # process CWD (which inside the container is the repo root and
    # would trigger restarts on every monorepo edit). Issue #130.
    assert kwargs["reload_dirs"] == [str(viewer_main.BASE_DIR)]
    assert viewer_main.BASE_DIR.name == "viewer"
