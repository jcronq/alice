"""FastAPI app wiring views + JSON APIs over Alice's logs."""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from . import aggregators, labels as kind_labels, narrative as narrative_mod, sources
from .settings import Paths, load as load_paths


BASE_DIR = pathlib.Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def create_app(paths: Paths | None = None) -> FastAPI:
    app = FastAPI(title="Alice Viewer", version="0.1.0")
    app.state.paths = paths or load_paths()

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["localtime"] = _localtime
    templates.env.filters["ago"] = _ago
    templates.env.filters["pretty_json"] = _pretty_json
    templates.env.filters["humanize_kind"] = kind_labels.humanize
    templates.env.filters["kind_family"] = kind_labels.family

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def _state_context() -> dict[str, Any]:
        p: Paths = app.state.paths
        events = sources.load_all(p)
        wakes = aggregators.group_wakes(events)
        turns = aggregators.group_turns(events)
        surfaces = [e for e in events if e.kind == "surface_pending"]
        emergencies = [e for e in events if e.kind == "emergency_pending"]
        last_wake = wakes[-1] if wakes else None
        last_turn = turns[-1] if turns else None
        return {
            "directive": sources.read_directive(p.inner),
            "paths": p,
            "pending_surfaces": len(surfaces),
            "pending_emergencies": len(emergencies),
            "last_wake_ts": last_wake.start_ts if last_wake else None,
            "last_turn_ts": last_turn.start_ts if last_turn else None,
            "total_wakes": len(wakes),
            "total_turns": len(turns),
            "event_count": len(events),
        }

    # ------------------------------------------------------------------
    # Views

    @app.get("/", response_class=HTMLResponse)
    async def timeline(request: Request, limit: int = 50, hemisphere: str | None = None):
        """Timeline of *runs* — one row per thinking wake or speaking turn.

        A run is a contiguous span of work. Thinking runs go from
        ``wake_start`` to ``wake_end``/``timeout``/``exception``;
        speaking runs go from ``signal_turn_start`` (or
        surface_dispatch / emergency_dispatch) to the matching turn_end.
        Click a row to drill into the per-event trace through that span.
        """
        p: Paths = app.state.paths
        events = sources.load_all(p)
        runs = aggregators.group_runs(events)
        if hemisphere:
            runs = [r for r in runs if r.hemisphere == hemisphere]
        total = len(runs)
        page = runs[:limit]
        return templates.TemplateResponse(
            request,
            "timeline.html",
            {
                "runs": page,
                "total_runs": total,
                "hemisphere": hemisphere,
                "limit": limit,
                "next_offset": limit,
                "has_more": total > limit,
                "state": _state_context(),
                "active": "timeline",
            },
        )

    @app.get("/timeline/page", response_class=HTMLResponse)
    async def timeline_page(
        request: Request,
        offset: int = 0,
        limit: int = 50,
        hemisphere: str | None = None,
    ):
        """HTML partial for one page of timeline rows + (optionally) a
        new infinite-scroll sentinel. Called by HTMX when the previous
        sentinel scrolls into view."""
        p: Paths = app.state.paths
        events = sources.load_all(p)
        runs = aggregators.group_runs(events)
        if hemisphere:
            runs = [r for r in runs if r.hemisphere == hemisphere]
        page = runs[offset : offset + limit]
        next_offset = offset + limit
        return templates.TemplateResponse(
            request,
            "_runs_partial.html",
            {
                "runs": page,
                "hemisphere": hemisphere,
                "limit": limit,
                "next_offset": next_offset,
                "has_more": next_offset < len(runs),
            },
        )

    @app.get("/api/runs/{run_id}")
    async def api_run_detail(run_id: str) -> JSONResponse:
        """Return one run + its event trace as JSON for the timeline modal."""
        p: Paths = app.state.paths
        events = sources.load_all(p)
        runs = aggregators.group_runs(events)
        match = next((r for r in runs if r.run_id == run_id), None)
        if match is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(
            {
                "run": match.to_dict(),
                "events": [e.to_dict() for e in match.events],
            }
        )

    @app.get("/api/runs")
    async def api_runs(limit: int = 200, hemisphere: str | None = None) -> JSONResponse:
        """Newest-first list of runs as JSON. Used by the timeline's
        live refresh; also handy for ad-hoc inspection via curl."""
        p: Paths = app.state.paths
        events = sources.load_all(p)
        runs = aggregators.group_runs(events)
        if hemisphere:
            runs = [r for r in runs if r.hemisphere == hemisphere]
        return JSONResponse([r.to_dict() for r in runs[:limit]])

    @app.get("/wakes", response_class=HTMLResponse)
    async def wakes_index(request: Request):
        p: Paths = app.state.paths
        events = sources.load_all(p)
        wakes = aggregators.group_wakes(events)
        wakes.reverse()
        return templates.TemplateResponse(
            request,
            "wakes.html",
            {
                "wakes": wakes,
                "state": _state_context(),
                "active": "wakes",
            },
        )

    @app.get("/wakes/{wake_id}", response_class=HTMLResponse)
    async def wake_detail(request: Request, wake_id: str):
        p: Paths = app.state.paths
        events = sources.load_all(p)
        wakes = aggregators.group_wakes(events)
        wake = next((w for w in wakes if w.wake_id == wake_id), None)
        return templates.TemplateResponse(
            request,
            "wake_detail.html",
            {
                "wake": wake,
                "state": _state_context(),
                "active": "wakes",
            },
        )

    @app.get("/turns", response_class=HTMLResponse)
    async def turns_index(request: Request):
        p: Paths = app.state.paths
        events = sources.load_all(p)
        turns = aggregators.group_turns(events)
        turns.reverse()
        return templates.TemplateResponse(
            request,
            "turns.html",
            {
                "turns": turns,
                "state": _state_context(),
                "active": "turns",
            },
        )

    @app.get("/turns/{turn_id}", response_class=HTMLResponse)
    async def turn_detail(request: Request, turn_id: str):
        p: Paths = app.state.paths
        events = sources.load_all(p)
        turns = aggregators.group_turns(events)
        turn = next((t for t in turns if t.turn_id == turn_id), None)
        return templates.TemplateResponse(
            request,
            "turn_detail.html",
            {
                "turn": turn,
                "state": _state_context(),
                "active": "turns",
            },
        )

    @app.get("/interactions", response_class=HTMLResponse)
    async def interactions(request: Request):
        p: Paths = app.state.paths
        events = sources.load_all(p)
        wakes = aggregators.group_wakes(events)
        turns = aggregators.group_turns(events)
        surfaces = [e for e in events if e.kind in ("surface_pending", "surface_resolved")]
        emergencies = [e for e in events if e.kind in ("emergency_pending", "emergency_resolved")]
        notes = [e for e in events if e.kind in ("note_pending", "note_consumed")]
        surfaces.sort(key=lambda e: e.ts, reverse=True)
        emergencies.sort(key=lambda e: e.ts, reverse=True)
        notes.sort(key=lambda e: e.ts, reverse=True)
        return templates.TemplateResponse(
            request,
            "interactions.html",
            {
                "surfaces": surfaces,
                "emergencies": emergencies,
                "notes": notes,
                "wakes": wakes,
                "turns": turns,
                "state": _state_context(),
                "active": "interactions",
            },
        )

    @app.get("/memory", response_class=HTMLResponse)
    async def memory_view(request: Request):
        return templates.TemplateResponse(
            request,
            "memory.html",
            {
                "state": _state_context(),
                "active": "memory",
            },
        )

    @app.get("/graph", response_class=HTMLResponse)
    async def interaction_graph_view(request: Request):
        return templates.TemplateResponse(
            request,
            "graph.html",
            {
                "state": _state_context(),
                "active": "graph",
            },
        )

    @app.get("/narrative", response_class=HTMLResponse)
    async def narrative_view(request: Request, window: str = "1h"):
        return templates.TemplateResponse(
            request,
            "narrative.html",
            {
                "window": window,
                "windows": list(narrative_mod.WINDOW_PRESETS.keys()),
                "state": _state_context(),
                "active": "narrative",
            },
        )

    @app.get("/api/narrative/stream")
    async def narrative_stream(request: Request, window: str = "24h", nocache: int = 0):
        """Bucketed narrative: per-time-bucket summaries (cached 7d on disk),
        merged on demand via a streamed LLM call."""
        p: Paths = app.state.paths
        window_seconds, window_label = narrative_mod.window_from_label(window)
        slots = narrative_mod.build_buckets(p, window_seconds, window)
        total_events = sum(len(s.events) for s in slots)

        # Final merge cache is keyed by the concatenation of bucket content hashes.
        merge_hash = "-".join(f"{s.start}:{s.content_hash}" for s in slots)
        import hashlib as _h
        merge_ckey = _h.sha256(merge_hash.encode()).hexdigest()[:16]
        cached_merge = None if nocache else narrative_mod.cache_get(merge_ckey)

        async def gen():
            progress_queue: asyncio.Queue = asyncio.Queue()

            async def progress_cb(info: dict):
                await progress_queue.put(info)

            meta = {
                "event_count": total_events,
                "bucket_total": len(slots),
                "bucket_seconds": narrative_mod.bucket_seconds_for(window),
                "window": window,
                "cached_final": cached_merge is not None,
            }
            yield {"event": "meta", "data": json.dumps(meta)}

            # Fast path: the final merged narrative is still in memory.
            if cached_merge is not None:
                yield {"event": "chunk", "data": json.dumps({"text": cached_merge})}
                yield {"event": "done", "data": "{}"}
                return

            # Shortcut: empty window.
            if total_events == 0:
                empty = "_Nothing happened in this window._"
                narrative_mod.cache_put(merge_ckey, empty)
                yield {"event": "chunk", "data": json.dumps({"text": empty})}
                yield {"event": "done", "data": "{}"}
                return

            # Kick off bucket cache fill; drain progress updates to the client
            # as it runs so the UI shows "3/24 buckets ready…".
            fill_task = asyncio.create_task(
                narrative_mod.ensure_bucket_cache(slots, progress_cb=progress_cb)
            )
            try:
                while not fill_task.done():
                    try:
                        info = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
                        yield {"event": "bucket_progress", "data": json.dumps(info)}
                    except asyncio.TimeoutError:
                        pass
                    if await request.is_disconnected():
                        fill_task.cancel()
                        return
                # Drain any final progress events.
                while not progress_queue.empty():
                    info = progress_queue.get_nowait()
                    yield {"event": "bucket_progress", "data": json.dumps(info)}
                summaries = await fill_task
            except Exception as exc:  # noqa: BLE001
                yield {"event": "error", "data": json.dumps({"message": f"bucket fill failed: {exc}"})}
                return

            # Merge step — streamed.
            merge_prompt = narrative_mod.render_merge_prompt(summaries, window_label)
            full_text: list[str] = []
            async for ev in narrative_mod.stream_narrative(merge_prompt):
                if await request.is_disconnected():
                    return
                if ev["type"] == "chunk":
                    full_text.append(ev["text"])
                    yield {"event": "chunk", "data": json.dumps({"text": ev["text"]})}
                elif ev["type"] == "result":
                    yield {"event": "result", "data": json.dumps(ev)}
                elif ev["type"] == "error":
                    yield {"event": "error", "data": json.dumps({"message": ev["message"]})}
                    return
                elif ev["type"] == "done":
                    narrative_mod.cache_put(merge_ckey, "".join(full_text))
                    yield {"event": "done", "data": "{}"}
                    return

        return EventSourceResponse(gen())

    @app.get("/activity", response_class=HTMLResponse)
    async def activity_view(request: Request, window: str = "24h"):
        p: Paths = app.state.paths
        events = sources.load_all(p)
        window_seconds, resolution = _parse_window(window)
        buckets = aggregators.activity_buckets(
            events,
            resolution_seconds=resolution,
            window_seconds=window_seconds,
            now_ts=time.time(),
        )
        hist = aggregators.tool_histogram(events)
        return templates.TemplateResponse(
            request,
            "activity.html",
            {
                "window": window,
                "buckets_json": json.dumps(buckets),
                "tool_histogram": hist,
                "state": _state_context(),
                "active": "activity",
            },
        )

    # ------------------------------------------------------------------
    # JSON APIs (fuel for d3)

    @app.get("/api/events")
    async def api_events(
        limit: int = 500,
        hemisphere: str | None = None,
        kind: str | None = None,
    ) -> JSONResponse:
        p: Paths = app.state.paths
        events = sources.load_all(p)
        if hemisphere:
            events = [e for e in events if e.hemisphere == hemisphere]
        if kind:
            events = [e for e in events if e.kind == kind]
        events = events[-limit:]
        return JSONResponse([e.to_dict() for e in events])

    @app.get("/api/memory-graph")
    async def api_memory_graph() -> JSONResponse:
        p: Paths = app.state.paths
        nodes, edges = sources.read_memory_graph(p.mind_dir)
        # Compute in-degree for sizing.
        in_deg: dict[str, int] = {}
        for e in edges:
            in_deg[e.target] = in_deg.get(e.target, 0) + 1
        return JSONResponse(
            {
                "nodes": [
                    {
                        "id": n.id,
                        "label": n.label,
                        "folder": n.folder,
                        "size": n.size,
                        "mtime": n.mtime,
                        "in_degree": in_deg.get(n.id, 0),
                    }
                    for n in nodes
                ],
                "edges": [{"source": e.source, "target": e.target} for e in edges],
            }
        )

    @app.get("/api/interaction-graph")
    async def api_interaction_graph() -> JSONResponse:
        p: Paths = app.state.paths
        events = sources.load_all(p)
        wakes = aggregators.group_wakes(events)
        turns = aggregators.group_turns(events)
        nodes, edges = aggregators.build_interaction_graph(events, wakes, turns)
        return JSONResponse(
            {
                "nodes": [
                    {
                        "id": n.id,
                        "label": n.label,
                        "kind": n.kind,
                        "ts": n.ts,
                        "meta": _safe_meta(n.meta),
                    }
                    for n in nodes
                ],
                "edges": [
                    {"source": e.source, "target": e.target, "kind": e.kind} for e in edges
                ],
            }
        )

    @app.get("/api/memory/note")
    async def api_memory_note(id: str) -> JSONResponse:
        """Return a memory note's body + frontmatter for the graph modal."""
        p: Paths = app.state.paths
        # node ids are relative-path-without-suffix, e.g. "memory/foo" or
        # "memory/sources/bar/baz". Rejoin with .md and ensure the result
        # stays inside mind/.
        if id.startswith("unresolved::"):
            return JSONResponse({
                "id": id,
                "label": id.split("::", 1)[1],
                "body": "",
                "unresolved": True,
            })
        candidate = (p.mind_dir / f"{id}.md").resolve()
        try:
            candidate.relative_to(p.mind_dir.resolve())
        except ValueError:
            return JSONResponse({"error": "path escape"}, status_code=400)
        if not candidate.is_file():
            return JSONResponse({"error": "not found", "id": id}, status_code=404)
        body = candidate.read_text(errors="replace")
        st = candidate.stat()
        return JSONResponse({
            "id": id,
            "path": str(candidate),
            "rel_path": str(candidate.relative_to(p.mind_dir)),
            "label": candidate.stem,
            "body": body,
            "size": st.st_size,
            "mtime": st.st_mtime,
        })

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        s = _state_context()
        # Strip the non-JSON Paths object.
        s.pop("paths", None)
        return JSONResponse(s)

    # ------------------------------------------------------------------
    # SSE live tail

    @app.get("/stream")
    async def stream(request: Request):
        p: Paths = app.state.paths
        thinking_path = p.thinking_log
        speaking_path = p.speaking_log

        # Start from current end-of-file so we only ship new events.
        offsets = {
            thinking_path: thinking_path.stat().st_size if thinking_path.is_file() else 0,
            speaking_path: speaking_path.stat().st_size if speaking_path.is_file() else 0,
        }

        async def gen():
            while True:
                if await request.is_disconnected():
                    return
                for path, hemisphere in (
                    (thinking_path, "thinking"),
                    (speaking_path, "speaking"),
                ):
                    new_lines = _read_since(path, offsets[path])
                    if new_lines:
                        offsets[path] = path.stat().st_size
                        for raw in new_lines:
                            try:
                                rec = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            rec["_hemisphere"] = hemisphere
                            yield {"event": "event", "data": json.dumps(rec)}
                await asyncio.sleep(1.0)

        return EventSourceResponse(gen())

    return app


# ---------------------------------------------------------------------------
# Helpers


def _read_since(path: pathlib.Path, offset: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size <= offset:
        # File was truncated (e.g. rotated) — reset from 0.
        if size < offset:
            offset = 0
        else:
            return []
    try:
        with path.open("r", errors="replace") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return []
    return [line for line in data.splitlines() if line.strip()]


def _localtime(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:  # noqa: BLE001
        return str(ts)


def _ago(ts: float | None) -> str:
    if not ts:
        return "—"
    delta = time.time() - ts
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _pretty_json(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)


def _safe_meta(meta: Any) -> Any:
    """Strip non-JSON-safe values from node meta so /api responses never break."""
    if not isinstance(meta, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in meta.items():
        try:
            json.dumps(v, default=str)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = str(v)
    return out


def _parse_window(w: str) -> tuple[int, int]:
    """Returns (window_seconds, resolution_seconds)."""
    presets = {
        "1h": (3600, 60),
        "6h": (6 * 3600, 300),
        "24h": (24 * 3600, 900),
        "7d": (7 * 86400, 3600),
        "30d": (30 * 86400, 6 * 3600),
    }
    return presets.get(w, presets["24h"])


def run() -> None:  # pragma: no cover
    """Entry point for `alice-viewer` console script."""
    import os
    import uvicorn

    host = os.environ.get("ALICE_VIEWER_HOST", "0.0.0.0")
    port = int(os.environ.get("ALICE_VIEWER_PORT", "7777"))
    uvicorn.run(
        "alice_viewer.main:create_app",
        host=host,
        port=port,
        factory=True,
        reload=bool(os.environ.get("ALICE_VIEWER_RELOAD")),
    )


if __name__ == "__main__":  # pragma: no cover
    run()
