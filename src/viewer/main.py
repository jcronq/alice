"""FastAPI app wiring views + JSON APIs over Alice's logs."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import pathlib
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from . import (
    aggregators,
    cluster_registry,
    labels as kind_labels,
    lobe_labeler,
    narrative as narrative_mod,
    sources,
    stage_d_store,
)
from .settings import Paths, load as load_paths


def _lobe_llm_enabled() -> bool:
    """Feature flag: LLM-derived display labels for lobes.

    Off by default so test environments and cold containers without
    google-adk installed don't hit the LAN endpoint. Set
    ``ALICE_LOBE_LLM_LABELS=1`` (or ``true``/``yes``) to opt in.
    """
    val = os.environ.get("ALICE_LOBE_LLM_LABELS", "").strip().lower()
    return val in ("1", "true", "yes", "on")


async def _refresh_llm_labels(
    registry: dict[str, Any],
    nodes: list[Any],
) -> bool:
    """Compute LLM display labels for any pending lobes, in parallel.

    Pending = newly minted, or member set drifted past
    :data:`cluster_registry.LLM_RELABEL_DRIFT_THRESHOLD` since the
    last LLM compute. The Qwen calls run concurrently via
    :func:`asyncio.gather`; one slow lobe doesn't serialise the
    whole batch.

    Returns ``True`` if the registry was mutated (caller saves), else
    ``False``. Returns ``False`` immediately when the feature flag is
    off so the LAN endpoint is never touched in disabled environments.
    """
    if not _lobe_llm_enabled():
        return False
    pending = cluster_registry.pending_llm_label_ids(registry)
    if not pending:
        return False
    node_path = {n.id: n.path for n in nodes}
    node_label = {n.id: n.label for n in nodes}
    entries = registry.get("entries") or {}

    async def _one(sid: str) -> tuple[str, str]:
        entry = entries.get(sid) or {}
        member_ids = entry.get("current_members") or []
        members: list[dict[str, str]] = []
        for nid in member_ids[: lobe_labeler.MAX_MEMBERS_FED]:
            path = node_path.get(nid)
            snippet = (
                lobe_labeler.extract_first_chunk(path) if path else ""
            )
            members.append(
                {"label": node_label.get(nid, nid), "snippet": snippet}
            )
        label = await lobe_labeler.compute_label_async(members)
        return sid, label

    results = await asyncio.gather(
        *(_one(sid) for sid in pending), return_exceptions=False
    )
    labels_by_id = {sid: lbl for sid, lbl in results if lbl}
    return cluster_registry.apply_llm_labels(registry, labels_by_id)


BASE_DIR = pathlib.Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def create_app(paths: Paths | None = None) -> FastAPI:
    # Plan 05 Phase 6: load personae before constructing the FastAPI
    # app so the chrome (title, header, narrative copy) can use the
    # configured agent name instead of hardcoding "Alice".
    from core.config.personae import (
        PersonaeError,
        load as load_personae,
        placeholder as placeholder_personae,
    )

    resolved_paths = paths or load_paths()
    try:
        personae = load_personae(resolved_paths.mind_dir)
    except FileNotFoundError:
        personae = placeholder_personae()
    except PersonaeError:
        # Surface but don't crash the viewer — the operator should be
        # able to read narrative even with a half-edited personae.yml.
        personae = placeholder_personae()

    app = FastAPI(title=f"{personae.agent.name} Viewer", version="0.1.0")
    app.state.paths = resolved_paths
    app.state.personae = personae
    # Plan 06 Phase 4: load mind/config/model.yml so the viewer's
    # narrative + run_summary calls can route to the operator's
    # configured backend. Missing file → subscription default
    # (today's behaviour).
    from core.config.model import load as load_model_config

    app.state.model_config = load_model_config(app.state.paths.mind_dir)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["localtime"] = _localtime
    templates.env.filters["ago"] = _ago
    templates.env.filters["tokens"] = _tokens
    templates.env.filters["pretty_json"] = _pretty_json
    templates.env.filters["humanize_kind"] = kind_labels.humanize
    templates.env.filters["kind_family"] = kind_labels.family
    templates.env.filters["tool_results"] = sources.parse_tool_results
    # Plan 05 Phase 6: every viewer template can reach the personae
    # without each route having to thread it through. Templates
    # use ``{{ personae.agent.name }}`` etc.
    templates.env.globals["personae"] = personae

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def _configured_models(p: Paths) -> list[dict[str, Any]]:
        """Build per-hemisphere model routing rows for the sidebar.

        Reads ``mind/config/model.yml`` + ``mind/config/pi-models.json``
        fresh on each call so swapping a hemisphere onto a different
        backend (e.g. local pi endpoint) is visible the next time the
        sidebar refreshes via ``sse:change``. Both files are KB-scale —
        no caching needed.

        For ``pi`` backends the model string is ``<provider>/<id>``;
        resolve the provider to ``baseUrl`` and the entry's pretty
        ``name`` from ``pi-models.json``. Missing or malformed config
        files degrade to "show what we have" rather than raising —
        the sidebar should never blow up the whole page render.
        """
        from core.config.model import (
            ModelConfigError,
            load as load_model_config,
        )

        try:
            cfg = load_model_config(p.mind_dir)
        except ModelConfigError:
            cfg = None

        pi_providers: dict[str, dict[str, Any]] = {}
        pi_path = p.mind_dir / "config" / "pi-models.json"
        if pi_path.is_file():
            try:
                pi_providers = (
                    json.loads(pi_path.read_text()).get("providers") or {}
                )
            except (json.JSONDecodeError, OSError):
                pi_providers = {}

        rows: list[dict[str, Any]] = []
        for hemisphere in ("speaking", "thinking", "viewer"):
            spec = cfg.hemisphere(hemisphere) if cfg else None
            backend = spec.backend if spec else "subscription"
            model = spec.model if spec else ""
            row: dict[str, Any] = {
                "agent": hemisphere,
                "backend": backend,
                "model": model,
                "display_model": model,
                "provider": None,
                "endpoint": None,
            }
            if backend == "pi" and "/" in model:
                provider_key, model_id = model.split("/", 1)
                provider = pi_providers.get(provider_key) or {}
                row["provider"] = provider_key
                row["endpoint"] = provider.get("baseUrl") or None
                for entry in provider.get("models", []) or []:
                    if entry.get("id") == model_id:
                        row["display_model"] = entry.get("name") or model_id
                        break
                else:
                    row["display_model"] = model_id
            rows.append(row)
        return rows

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
            "current_objective": sources.read_current_objective(p.inner),
            "pending_surfaces": len(surfaces),
            "pending_emergencies": len(emergencies),
            "last_wake_ts": last_wake.start_ts if last_wake else None,
            "last_turn_ts": last_turn.start_ts if last_turn else None,
            "total_wakes": len(wakes),
            "total_turns": len(turns),
            "event_count": len(events),
            "speaking_usage": aggregators.latest_speaking_usage(events),
            "thinking_avg": aggregators.thinking_usage_average(events),
            "configured_models": _configured_models(p),
        }

    # ------------------------------------------------------------------
    # Views

    @app.get("/", response_class=HTMLResponse)
    async def timeline(
        request: Request, limit: int = 50, hemisphere: str | None = None
    ):
        """Timeline of *runs* — one row per thinking wake or speaking turn.

        A run is a contiguous span of work. Thinking runs go from
        ``wake_start`` to ``wake_end``/``timeout``/``exception``;
        speaking runs go from ``signal_turn_start`` (or
        surface_dispatch / emergency_dispatch) to the matching turn_end.
        Click a row to drill into the per-event trace through that span.
        """
        p: Paths = app.state.paths
        events = sources.load_all(p)
        runs = aggregators.group_runs(events, paths=p)
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
        runs = aggregators.group_runs(events, paths=p)
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
        """Return one run + its event trace as JSON for the timeline modal.

        Falls through to ``group_sm_runs`` so live SM-dispatcher spawns
        (which ``group_runs`` doesn't see) also resolve — and synthesises
        an event trace from the spawn dir's files since SM workers don't
        emit structured events (#126).

        For ``sm_spawn`` runs the trace also includes the worker's full
        claude session JSONL (every tool call) and the issue's GitHub
        timeline (label changes, ``[SM] *`` audit comments, PR linkage),
        merged chronologically (#137).
        """
        p: Paths = app.state.paths
        events = sources.load_all(p)
        runs = aggregators.group_runs(events, paths=p)
        match = next((r for r in runs if r.run_id == run_id), None)
        trace = list(match.events) if match else []
        if match is None:
            sm_runs = aggregators.group_sm_runs(p)
            match = next((r for r in sm_runs if r.run_id == run_id), None)
        if match is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if match.kind == "sm_spawn" and not trace:
            spawn_dir = sources._find_sm_spawn_dir(p, match.run_id)
            if spawn_dir is not None:
                # The spawn id format is ``spawn-<N>-<unix-ts>``; pull
                # the issue number out so the timeline fetcher can hit
                # the right gh endpoint. Bad/legacy ids fall through
                # to no-timeline mode (the fetcher is a no-op without
                # an issue number).
                issue_number = sources._sm_spawn_issue_number(match.run_id)
                trace = sources.sm_spawn_trace_events(
                    spawn_dir,
                    match.start_ts,
                    repo=os.environ.get("ALICE_SM_REPO", "jcronq/alice"),
                    issue_number=issue_number,
                    fetch_timeline=sources.fetch_issue_timeline_cached,
                )
        return JSONResponse(
            {
                "run": match.to_dict(),
                "events": [e.to_dict() for e in trace],
            }
        )

    @app.get("/api/runs")
    async def api_runs(limit: int = 200, hemisphere: str | None = None) -> JSONResponse:
        """Newest-first list of runs as JSON. Used by the timeline's
        live refresh; also handy for ad-hoc inspection via curl."""
        p: Paths = app.state.paths
        events = sources.load_all(p)
        runs = aggregators.group_runs(events, paths=p)
        if hemisphere:
            runs = [r for r in runs if r.hemisphere == hemisphere]
        return JSONResponse([r.to_dict() for r in runs[:limit]])

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_index(request: Request, limit: int = 50):
        """SM-dispatcher spawns — live + finished, newest start first.

        Sibling of ``/wakes`` and ``/turns`` for SM worker runs (#124);
        a filtered view onto the same data ``group_sm_runs`` produces.
        """
        p: Paths = app.state.paths
        runs = aggregators.group_sm_runs(p)
        total = len(runs)
        page = runs[:limit]
        return templates.TemplateResponse(
            request,
            "runs.html",
            {
                "runs": page,
                "total_runs": total,
                "limit": limit,
                "next_offset": limit,
                "has_more": total > limit,
                "state": _state_context(),
                "active": "runs",
            },
        )

    @app.get("/runs/page", response_class=HTMLResponse)
    async def runs_page(request: Request, offset: int = 0, limit: int = 50):
        """HTML partial for one page of SM-runs rows + (optionally) a new
        infinite-scroll sentinel. Mirrors ``/timeline/page``."""
        p: Paths = app.state.paths
        runs = aggregators.group_sm_runs(p)
        page = runs[offset : offset + limit]
        next_offset = offset + limit
        return templates.TemplateResponse(
            request,
            "_runs_partial.html",
            {
                "runs": page,
                "limit": limit,
                "next_offset": next_offset,
                "has_more": next_offset < len(runs),
                # _runs_partial.html's sentinel hard-codes /timeline/page;
                # signal we want the SM-only feed instead.
                "page_url": "/runs/page",
            },
        )

    @app.get("/wakes", response_class=HTMLResponse)
    async def wakes_index(request: Request, limit: int = 50):
        from . import run_summary

        p: Paths = app.state.paths
        events = sources.load_all(p)
        wakes = aggregators.group_wakes(events)
        wakes.reverse()
        total = len(wakes)
        page = wakes[:limit]
        summaries = {
            w.wake_id: aggregators.summarize_wake(w, run_summary) for w in page
        }
        return templates.TemplateResponse(
            request,
            "wakes.html",
            {
                "wakes": page,
                "summaries": summaries,
                "total_wakes": total,
                "limit": limit,
                "next_offset": limit,
                "has_more": total > limit,
                "state": _state_context(),
                "active": "wakes",
            },
        )

    @app.get("/wakes/page", response_class=HTMLResponse)
    async def wakes_page(request: Request, offset: int = 0, limit: int = 50):
        """HTML partial for one page of wake rows + (optionally) a new
        infinite-scroll sentinel. Called by HTMX when the previous
        sentinel scrolls into view."""
        from . import run_summary

        p: Paths = app.state.paths
        events = sources.load_all(p)
        wakes = aggregators.group_wakes(events)
        wakes.reverse()
        page = wakes[offset : offset + limit]
        summaries = {
            w.wake_id: aggregators.summarize_wake(w, run_summary) for w in page
        }
        next_offset = offset + limit
        return templates.TemplateResponse(
            request,
            "_wakes_partial.html",
            {
                "wakes": page,
                "summaries": summaries,
                "limit": limit,
                "next_offset": next_offset,
                "has_more": next_offset < len(wakes),
            },
        )

    @app.get("/api/wakes/{wake_id}/summary", response_class=HTMLResponse)
    async def wake_summary_cell(request: Request, wake_id: str):
        """HTMX poll target for the per-row 'summarising…' fallback.
        Returns the same summary cell partial — when the Haiku call
        has filled the cache, the response omits the polling
        attributes so the swap stops the poll naturally."""
        from . import run_summary

        p: Paths = app.state.paths
        events = sources.load_all(p)
        wakes = aggregators.group_wakes(events)
        wake = next((w for w in wakes if w.wake_id == wake_id), None)
        if wake is None:
            return HTMLResponse("<span>(unknown wake)</span>")
        summary = aggregators.summarize_wake(wake, run_summary) or ""
        return templates.TemplateResponse(
            request,
            "_wake_summary_cell.html",
            {"w": wake, "summary": summary},
        )

    @app.get("/wakes/{wake_id}", response_class=HTMLResponse)
    async def wake_detail(request: Request, wake_id: str):
        from . import run_summary

        p: Paths = app.state.paths
        events = sources.load_all(p)
        wakes = aggregators.group_wakes(events)
        wake = next((w for w in wakes if w.wake_id == wake_id), None)
        summary = aggregators.summarize_wake(wake, run_summary) if wake else None
        thought = (
            sources.find_wake_thought(events, wake.start_ts, wake.end_ts)
            if wake
            else None
        )
        return templates.TemplateResponse(
            request,
            "wake_detail.html",
            {
                "wake": wake,
                "summary": summary,
                "thought": thought,
                "state": _state_context(),
                "active": "wakes",
            },
        )

    def _enrich_turns_with_outbound(turns, p: Paths) -> None:
        # *_turn_end events don't carry the actual message text Speaking
        # sent, but speaking-turns.jsonl does. Patch matching turns
        # in-place. Applies to any turn kind that writes a turn-log
        # entry (signal, cli, discord).
        turn_log_events = sources.read_turn_log(p.turn_log)
        for tev in turn_log_events:
            rec = tev.detail or {}
            outbound = rec.get("outbound")
            sender_name = rec.get("sender_name")
            if not outbound:
                continue
            for t in turns:
                if t.outbound:
                    continue
                if t.sender_name != sender_name:
                    continue
                anchor = t.end_ts if t.end_ts else t.start_ts
                if abs(tev.ts - anchor) <= 10.0:
                    t.outbound = outbound
                    break

    @app.get("/turns", response_class=HTMLResponse)
    async def turns_index(request: Request, limit: int = 50):
        p: Paths = app.state.paths
        events = sources.load_all(p)
        turns = aggregators.group_turns(events)
        _enrich_turns_with_outbound(turns, p)
        turns.reverse()
        total = len(turns)
        page = turns[:limit]
        return templates.TemplateResponse(
            request,
            "turns.html",
            {
                "turns": page,
                "total_turns": total,
                "limit": limit,
                "next_offset": limit,
                "has_more": total > limit,
                "state": _state_context(),
                "active": "turns",
            },
        )

    @app.get("/turns/page", response_class=HTMLResponse)
    async def turns_page(request: Request, offset: int = 0, limit: int = 50):
        """HTML partial for one page of turn rows + (optionally) a new
        infinite-scroll sentinel. Called by HTMX when the previous
        sentinel scrolls into view."""
        p: Paths = app.state.paths
        events = sources.load_all(p)
        turns = aggregators.group_turns(events)
        _enrich_turns_with_outbound(turns, p)
        turns.reverse()
        page = turns[offset : offset + limit]
        next_offset = offset + limit
        return templates.TemplateResponse(
            request,
            "_turns_partial.html",
            {
                "turns": page,
                "limit": limit,
                "next_offset": next_offset,
                "has_more": next_offset < len(turns),
            },
        )

    @app.get("/turns/{turn_id}", response_class=HTMLResponse)
    async def turn_detail(request: Request, turn_id: str):
        p: Paths = app.state.paths
        events = sources.load_all(p)
        turns = aggregators.group_turns(events)
        _enrich_turns_with_outbound(turns, p)
        turn = next((t for t in turns if t.turn_id == turn_id), None)
        summary = aggregators.summarize_turn(turn) if turn else None
        return templates.TemplateResponse(
            request,
            "turn_detail.html",
            {
                "turn": turn,
                "summary": summary,
                "state": _state_context(),
                "active": "turns",
            },
        )

    @app.get("/interactions", response_class=HTMLResponse)
    async def interactions(
        request: Request,
        limit: int = 100,
        kind: str | None = None,  # "signal" | "surface" | "emergency" | None
        sender: str | None = None,  # filter by sender name (signal turns)
    ):
        p: Paths = app.state.paths
        events = sources.load_all(p)
        turns = aggregators.group_turns(events)
        _enrich_turns_with_outbound(turns, p)
        # group_arcs reads turn.outbound off the Turn objects we just
        # patched, so the enrichment carries through.
        arcs = aggregators.group_arcs(events, turns=turns)

        total_arcs = len(arcs)
        # Per-kind counts (pre-filter)
        kind_counts = {
            "signal": sum(1 for a in arcs if a.kind == "signal"),
            "surface": sum(1 for a in arcs if a.kind == "surface"),
            "emergency": sum(1 for a in arcs if a.kind == "emergency"),
        }
        # Distinct senders for the filter pill row (signal arcs only)
        senders = sorted({a.sender for a in arcs if a.kind == "signal" and a.sender})

        # Apply filters
        filtered = arcs
        if kind in ("signal", "surface", "emergency"):
            filtered = [a for a in filtered if a.kind == kind]
        if sender:
            filtered = [a for a in filtered if a.sender == sender]

        # Newest-first cap
        filtered = filtered[: max(1, min(limit, 500))]

        pending_surfaces = sum(1 for e in events if e.kind == "surface_pending")
        pending_emergencies = sum(1 for e in events if e.kind == "emergency_pending")
        pending_notes = sum(1 for e in events if e.kind == "note_pending")

        return templates.TemplateResponse(
            request,
            "interactions.html",
            {
                "arcs": filtered,
                "total_arcs": total_arcs,
                "kind_counts": kind_counts,
                "senders": senders,
                "filter_kind": kind,
                "filter_sender": sender,
                "limit": limit,
                "pending_surfaces": pending_surfaces,
                "pending_emergencies": pending_emergencies,
                "pending_notes": pending_notes,
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

    @app.get("/context", response_class=HTMLResponse)
    async def context_view(request: Request):
        """Live snapshot of the speaking daemon's context composition.

        The page itself just renders the chrome + an empty block grid;
        the actual snapshot fetch happens via a separate ``/api/context``
        call so the operator sees the page immediately and can hit
        refresh without a hard reload.

        Page-level config (model context window, last-turn input_tokens
        for the "live transcript" bucket) is injected as a JSON script
        tag so the page-load JS doesn't need a second fetch.
        """
        import os as _os

        state = _state_context()
        speaking_usage = state.get("speaking_usage") or {}
        ctx_obj = speaking_usage.get("context") or {}
        # The model's real context cost for the last turn is
        # input + cache_creation + cache_read — the bare "input" field
        # only counts the uncached new prompt for that turn (often <100
        # tokens). The cache reads ARE in context; they're just billed
        # cheaper. Mirrors aggregators._iter_context.
        last_turn_total = (
            int(ctx_obj.get("input") or 0)
            + int(ctx_obj.get("cache_creation") or 0)
            + int(ctx_obj.get("cache_read") or 0)
        )
        # Default 1M (Alice runs Opus 4.7 with the [1m] context-window
        # opt-in); override per-host via env when running a 200k model.
        try:
            ctx_window = int(_os.environ.get("ALICE_CONTEXT_WINDOW", "1000000"))
        except ValueError:
            ctx_window = 1_000_000
        page_data = {
            "context_window": ctx_window,
            "last_turn_input_tokens": last_turn_total or None,
        }
        return templates.TemplateResponse(
            request,
            "context.html",
            {
                "state": state,
                "active": "context",
                "context_page_data": json.dumps(page_data),
            },
        )

    @app.get("/api/context")
    async def api_context(request: Request):
        """Fetch the live context snapshot from the worker's CLI socket
        (default ``/state/alice.sock``) and decompose it into donut-ready
        components."""
        from . import context_probe_client as _probe_client

        try:
            snapshot = await _probe_client.fetch_snapshot()
        except FileNotFoundError as exc:
            return JSONResponse(
                {"error": str(exc), "kind": "no_socket"},
                status_code=503,
            )
        except TimeoutError as exc:
            return JSONResponse(
                {"error": str(exc), "kind": "timeout"},
                status_code=504,
            )
        except RuntimeError as exc:
            return JSONResponse(
                {"error": str(exc), "kind": "rpc_failed"},
                status_code=502,
            )
        decomposed = _probe_client.decompose(snapshot)
        return JSONResponse(decomposed)

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
                yield {
                    "event": "error",
                    "data": json.dumps({"message": f"bucket fill failed: {exc}"}),
                }
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
                    yield {
                        "event": "error",
                        "data": json.dumps({"message": ev["message"]}),
                    }
                    return
                elif ev["type"] == "done":
                    narrative_mod.cache_put(merge_ckey, "".join(full_text))
                    yield {"event": "done", "data": "{}"}
                    return

        return EventSourceResponse(gen())

    @app.get("/canvas", response_class=HTMLResponse)
    async def canvas_index(request: Request, page: int = 1, page_size: int = 20):
        p: Paths = app.state.paths
        all_canvases = sources.list_canvases(p.inner)
        page_size = max(1, min(page_size, 100))
        total = len(all_canvases)
        last_page = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, last_page))
        start = (page - 1) * page_size
        canvases = all_canvases[start : start + page_size]
        return templates.TemplateResponse(
            request,
            "canvas_index.html",
            {
                "canvases": canvases,
                "page": page,
                "page_size": page_size,
                "last_page": last_page,
                "total": total,
                "state": _state_context(),
                "active": "canvas",
            },
        )

    @app.get("/canvas/{slug}", response_class=HTMLResponse)
    async def canvas_view(request: Request, slug: str):
        p: Paths = app.state.paths
        # HTML slide decks under ``inner/canvas/`` — serve raw.
        canvas = sources.read_canvas(p.inner, slug)
        if canvas is not None:
            return HTMLResponse(canvas["body"])
        # Markdown content moved to /research-papers (2026-05-20 nav
        # split). Redirect any stale slug that still resolves over there
        # so worker-generated /canvas/<slug> links keep working.
        if p.mind_dir is not None:
            paper = sources.read_research_paper(p.mind_dir, slug)
            if paper is not None:
                return RedirectResponse(
                    url=f"/research-papers/{slug}", status_code=307
                )
            # Unflagged research-note fallback (issue #175) — same
            # redirect target; ``research_papers_view`` renders the
            # banner.
            fallback = sources.read_research_note(p.mind_dir, slug)
            if fallback is not None:
                return RedirectResponse(
                    url=f"/research-papers/{slug}", status_code=307
                )
        return HTMLResponse(
            f"<h1>canvas not found: {slug}</h1>"
            f"<p><a href='/canvas'>← back to index</a></p>",
            status_code=404,
        )

    @app.get("/research-papers", response_class=HTMLResponse)
    async def research_papers_index(request: Request):
        p: Paths = app.state.paths
        papers = (
            sources.list_research_papers(p.mind_dir)
            if p.mind_dir is not None
            else []
        )
        return templates.TemplateResponse(
            request,
            "research_papers_index.html",
            {
                "papers": papers,
                "state": _state_context(),
                "active": "research-papers",
            },
        )

    @app.get("/research-papers/{slug}", response_class=HTMLResponse)
    async def research_papers_view(request: Request, slug: str):
        p: Paths = app.state.paths
        paper = None
        fallback_banner = False
        if p.mind_dir is not None:
            paper = sources.read_research_paper(p.mind_dir, slug)
            # Issue #175 fallback: unflagged research notes still render
            # with a small "this isn't canvas-paper format" banner so
            # backlinks don't 404.
            if paper is None:
                paper = sources.read_research_note(p.mind_dir, slug)
                if paper is not None:
                    fallback_banner = True
        if paper is None:
            return HTMLResponse(
                f"<h1>research paper not found: {slug}</h1>"
                f"<p><a href='/research-papers'>← back to index</a></p>",
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "canvas_paper.html",
            {
                "canvas": paper,
                "fallback_banner": fallback_banner,
                "state": _state_context(),
                "active": "research-papers",
            },
        )

    @app.get("/designs", response_class=HTMLResponse)
    async def designs_index(request: Request):
        p: Paths = app.state.paths
        designs = sources.list_designs(p.mind_dir)
        return templates.TemplateResponse(
            request,
            "designs_index.html",
            {
                "designs": designs,
                "state": _state_context(),
                "active": "designs",
            },
        )

    @app.get("/designs/{slug}", response_class=HTMLResponse)
    async def designs_view(request: Request, slug: str):
        p: Paths = app.state.paths
        design = sources.read_design(p.mind_dir, slug)
        if design is None:
            return HTMLResponse(
                f"<h1>design not found: {slug}</h1>"
                f"<p><a href='/designs'>← back to index</a></p>",
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "designs_view.html",
            {
                "design": design,
                "state": _state_context(),
                "active": "designs",
            },
        )

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

    @app.get("/running", response_class=HTMLResponse)
    async def running_view(request: Request):
        p: Paths = app.state.paths
        jobs = sources.list_running_jobs(p)
        return templates.TemplateResponse(
            request,
            "running.html",
            {
                "jobs": [j.to_dict() for j in jobs],
                "state": _state_context(),
                "active": "running",
            },
        )

    @app.get("/api/running")
    async def api_running() -> JSONResponse:
        p: Paths = app.state.paths
        jobs = sources.list_running_jobs(p)
        return JSONResponse([j.to_dict() for j in jobs])

    @app.get("/api/running/partial", response_class=HTMLResponse)
    async def api_running_partial(request: Request):
        """HTMX-driven partial refresh for the /running tab. Avoids the
        sidebar + full base.html round-trip every 5s — same data, just
        the rows."""
        p: Paths = app.state.paths
        jobs = sources.list_running_jobs(p)
        return templates.TemplateResponse(
            request,
            "_running_rows.html",
            {"jobs": [j.to_dict() for j in jobs]},
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
        nodes, edges, cluster_metrics = sources.load_memory_graph_bundle(p.mind_dir)
        # Compute in-degree for sizing.
        in_deg: dict[str, int] = {}
        for e in edges:
            in_deg[e.target] = in_deg.get(e.target, 0) + 1
        node_cluster = cluster_metrics.get("node_cluster", {})

        # Phase 2 + LLM labels: run the persistent registry against the
        # fresh clusters so each lobe gets its stable ``cl-<slug>`` ID,
        # then compute / refresh the LLM display label for any minted
        # or drifted lobe. Both are decorated onto cluster_metrics so
        # the lobe view can render ``llm_label`` primary with the
        # stable slug as a chip.
        clusters_list = cluster_metrics.get("clusters", [])
        topical_clusters = [c for c in clusters_list if not c["is_misc"]]
        registry = None
        stable_by_cid: dict[str, str] = {}
        if topical_clusters:
            reg_path = cluster_registry.registry_path()
            prev_registry = cluster_registry.load_registry(reg_path)
            registry, _alerts = cluster_registry.rebuild(
                cluster_members={
                    c["id"]: c["member_ids"] for c in topical_clusters
                },
                cluster_top_hubs={
                    c["id"]: [h["id"] for h in c["top_hubs"]]
                    for c in topical_clusters
                },
                label_by_id={n.id: n.label for n in nodes},
                prev_registry=prev_registry,
            )
            llm_mutated = await _refresh_llm_labels(registry, nodes)
            if registry != prev_registry or llm_mutated:
                cluster_registry.save_registry(reg_path, registry)
            # Reverse-index by member set: every fresh cid that
            # survived the rebuild has exactly one entry whose
            # ``last_member_set`` matches its members.
            members_to_stable = {
                frozenset(e["last_member_set"]): sid
                for sid, e in registry["entries"].items()
                if e.get("status") == "live"
                and e.get("last_rebuild") == registry["last_rebuild"]
            }
            for c in topical_clusters:
                stable = members_to_stable.get(frozenset(c["member_ids"]))
                if stable:
                    stable_by_cid[c["id"]] = stable

        # Build a fresh decorated clusters list — cluster_metrics is a
        # shared cached object, so we must not mutate it in place.
        entries = (registry or {}).get("entries", {})
        decorated_clusters = []
        for c in clusters_list:
            stable = stable_by_cid.get(c["id"])
            entry = entries.get(stable) if stable else None
            decorated_clusters.append(
                {
                    **c,
                    "cluster_slug": stable,
                    "llm_label": (entry or {}).get("llm_label"),
                }
            )
        response_metrics = {**cluster_metrics, "clusters": decorated_clusters}

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
                        "cluster_id": node_cluster.get(n.id),
                    }
                    for n in nodes
                ],
                "edges": [{"source": e.source, "target": e.target} for e in edges],
                "cluster_metrics": response_metrics,
            }
        )

    @app.get("/api/cluster-snapshot")
    async def api_cluster_snapshot() -> JSONResponse:
        """Bidirectional cluster lookup, designed for the cortex-memory/query
        skill and Stage D pair selection. Slugs are the primary key — the
        payload is greppable by note id without parsing.

        Shape (per the agreed spec with Alice):

            {
              "generated_at": <unix-ts>,
              "nodes":    {"<slug>": {"cluster_id", "cluster_label",
                                       "in_degree", "out_degree",
                                       "last_modified",
                                       "neighbor_clusters"}},
              "clusters": {"cl-foo": {"label", "id", "size", "is_misc",
                                       "dominant_folder", "member_slugs",
                                       "top_hubs"}},
              "cross_edges": {"cl-foo|cl-bar": {"weight", "weight_normalized"}}
            }

        Per-node fields:

        - ``cluster_id`` is ``null`` for non-topical notes (dailies,
          operational instructions, root-level index/README notes,
          unresolved wikilink ghosts). These are intentionally excluded
          from cluster analysis upstream by ``_is_topical`` — they bridge
          across domains and would force a hairball verdict. Consumers
          (cortex-memory/query, Stage D) should treat null as **skip**,
          not as "find nearest cluster".
        - ``neighbor_clusters`` is an undirected count of edges from this
          node to each cluster (cluster_label → edge_count). Useful for
          bridge-note detection: a node whose neighbor_clusters touches 4+
          distinct clusters is a structural bridge regardless of which
          cluster it nominally belongs to.
        - ``last_modified`` is the file mtime, used by Stage D's "recent
          research corpus" gates without requiring per-file stat() calls.

        Per-cluster ``top_hubs``:

        - Ordered by **in-degree within the topical subgraph**, descending,
          with alphabetical tiebreak on the node id. Each hub entry carries
          both ``in_degree`` and ``out_degree`` so consumers can distinguish
          sinks (high in / low out) from crossroads (high in / high out).
          Phase 3 may add named alternative metrics (PageRank, betweenness)
          on a sibling field; in-degree stays the default.

        Cross-edges:

        - Keys are ``"<label_a>|<label_b>"`` with ``label_a < label_b``
          lexicographically — **symmetric and aggregated**. ``weight`` is
          the total number of directed wikilinks bridging the two lobes
          (either direction summed). ``weight_normalized`` is
          ``weight / sqrt(|A| * |B|)``.

        Cadence:

        - Cached at the ``sources.load_memory_graph_bundle`` layer:
          stat-only signature over ``cortex-memory/`` + ``memory/`` (with
          ``cortex-memory/dailies/`` excluded — those churn on every
          wake) decides hit vs miss. Hit reuses the cached
          ``(nodes, edges, cluster_metrics)``; miss re-walks and
          re-runs label propagation. The registry rebuild + LLM-label
          refresh still run on every request (both cheap; LLM refresh
          short-circuits when no lobes are pending). ``generated_at``
          is the response time and reflects when the *registry* state
          was assembled, not the underlying graph snapshot.

        Phase 1 omits per-cluster modularity (Phase 3) and Jaccard-tracked
        identity drift (Phase 2). Both will be added in place without
        breaking existing keys.
        """
        p: Paths = app.state.paths
        nodes, edges, cm = sources.load_memory_graph_bundle(p.mind_dir)
        in_deg: dict[str, int] = {}
        out_deg: dict[str, int] = {}
        for e in edges:
            in_deg[e.target] = in_deg.get(e.target, 0) + 1
            out_deg[e.source] = out_deg.get(e.source, 0) + 1
        node_cluster = cm.get("node_cluster", {})
        clusters = cm.get("clusters", [])
        cross_cluster_edges = cm.get("cross_cluster_edges", [])

        # Phase 2: stable cluster IDs from the persistent Jaccard-tracked
        # registry. The fresh ``cl-<top-hub>`` labels in
        # ``cm["clusters"][i]["label"]`` are recomputed every request and
        # rot across rebuilds; the registry's labels are minted at birth
        # and frozen forever (with ``absorbed_into`` pointers when
        # clusters retire). The misc bucket bypasses the registry — it's
        # not an identity, it's a junk drawer.
        label_by_cid: dict[str, str] = {}
        for c in clusters:
            if c["is_misc"]:
                label_by_cid[c["id"]] = c["label"]
        topical_clusters = [c for c in clusters if not c["is_misc"]]
        if topical_clusters:
            reg_path = cluster_registry.registry_path()
            prev_registry = cluster_registry.load_registry(reg_path)
            new_registry, _alerts = cluster_registry.rebuild(
                cluster_members={c["id"]: c["member_ids"] for c in topical_clusters},
                cluster_top_hubs={
                    c["id"]: [h["id"] for h in c["top_hubs"]] for c in topical_clusters
                },
                label_by_id={n.id: n.label for n in nodes},
                prev_registry=prev_registry,
            )
            llm_mutated = await _refresh_llm_labels(new_registry, nodes)
            # Persist if the registry mutated (new entries, drift updates,
            # retirement bumps, or fresh LLM labels).
            if new_registry != prev_registry or llm_mutated:
                cluster_registry.save_registry(reg_path, new_registry)
            # Resolve fresh cid -> stable id by re-running the matching:
            # every fresh cid that survived the rebuild now has a single
            # entry whose ``last_member_set`` is identical to that fresh
            # cluster's members. Reverse-index by member set to find it.
            members_to_stable: dict[frozenset, str] = {
                frozenset(e["last_member_set"]): sid
                for sid, e in new_registry["entries"].items()
                if e.get("status") == "live"
                and e.get("last_rebuild") == new_registry["last_rebuild"]
            }
            for c in topical_clusters:
                stable = members_to_stable.get(frozenset(c["member_ids"]))
                # Fallback to the fresh content-derived label if the
                # registry didn't resolve (shouldn't happen in v1; defensive).
                label_by_cid[c["id"]] = stable or c["label"]
        else:
            for c in clusters:
                label_by_cid[c["id"]] = c["label"]

        # neighbor_clusters: for each node, count undirected edges into
        # each cluster_label. Self-cluster edges are kept so a consumer
        # can read in-cluster vs cross-cluster ratio. Both topical and
        # non-topical nodes get populated — non-topical bridges (e.g.
        # cortex-memory/index) genuinely link out into clusters and that
        # signal matters even though the bridge node itself isn't in a
        # cluster. Endpoints with no cluster (both endpoints non-topical)
        # contribute nothing.
        neighbor_clusters: dict[str, dict[str, int]] = {}
        for e in edges:
            sc = node_cluster.get(e.source)
            tc = node_cluster.get(e.target)
            # Source's view of target's cluster (if target is topical).
            tlabel = label_by_cid.get(tc) if tc else None
            if tlabel:
                nc = neighbor_clusters.setdefault(e.source, {})
                nc[tlabel] = nc.get(tlabel, 0) + 1
            # Target's view of source's cluster (if source is topical) —
            # undirected accounting.
            slabel = label_by_cid.get(sc) if sc else None
            if slabel:
                nc = neighbor_clusters.setdefault(e.target, {})
                nc[slabel] = nc.get(slabel, 0) + 1

        clusters_out = {}
        for c in clusters:
            stable_label = label_by_cid.get(c["id"], c["label"])
            entry = {
                "label": stable_label,
                "id": c["id"],
                "size": c["size"],
                "is_misc": c["is_misc"],
                "dominant_folder": c["dominant_folder"],
                "member_slugs": c["member_ids"],
                "top_hubs": c["top_hubs"],
            }
            # Carry registry-derived stability fields when the cluster
            # has a real (non-misc) entry. drift + current/birth top hub
            # are the diagnostic signals Alice's Stage D selection and
            # the cortex-memory/query skill use to decide how much trust
            # to put in the cluster name.
            if not c["is_misc"] and topical_clusters:
                reg_entry = new_registry["entries"].get(stable_label)
                if reg_entry:
                    entry["drift"] = reg_entry.get("drift", 0.0)
                    entry["birth_top_hub"] = reg_entry.get("birth_top_hub")
                    entry["current_top_hub"] = reg_entry.get("current_top_hub")
                    entry["created"] = reg_entry.get("created")
            clusters_out[stable_label] = entry
        nodes_out = {
            n.id: {
                "cluster_id": node_cluster.get(n.id),
                "cluster_label": label_by_cid.get(node_cluster.get(n.id)),
                "in_degree": in_deg.get(n.id, 0),
                "out_degree": out_deg.get(n.id, 0),
                "last_modified": n.mtime,
                "neighbor_clusters": neighbor_clusters.get(n.id, {}),
            }
            for n in nodes
        }
        cross_out = {}
        for e in cross_cluster_edges:
            sl = label_by_cid.get(e["source"], e["source"])
            tl = label_by_cid.get(e["target"], e["target"])
            key = f"{sl}|{tl}" if sl < tl else f"{tl}|{sl}"
            cross_out[key] = {
                "weight": e["weight"],
                "weight_normalized": e["weight_normalized"],
            }
        return JSONResponse(
            {
                "generated_at": time.time(),
                "nodes": nodes_out,
                "clusters": clusters_out,
                "cross_edges": cross_out,
            }
        )

    @app.get("/api/cluster-registry")
    async def api_cluster_registry() -> JSONResponse:
        """Persistent cluster identity registry — Phase 2 of the
        lobe-context handoff to Alice.

        The viewer's alice-mind mount is read-only, so the registry
        lives in ``$ALICE_VIEWER_CACHE_DIR/clusters/registry.json``
        rather than the spec'd ``inner/state/`` location. Alice's
        thinking polls this endpoint during wakes to read entries
        and drift alerts; the data is identical to what the spec
        describes — only the storage path differs.

        Response shape:

            {
              "schema_version": 1,
              "generated_at": <unix-ts>,
              "registry_path": "<host path for diagnostics>",
              "last_rebuild": "<ISO date>",
              "entries":      {"cl-foo": {<full registry entry>}},
              "drift_alerts": [{"id", "drift", "birth_top_hub",
                                "current_top_hub", "birth_size",
                                "current_size", "created"}, ...]
            }

        ``drift_alerts`` is the v1 substitute for the spec's "fire a
        surface to ``inner/notes/``" mechanism (also blocked by the RO
        mount). Thinking polls; drift ≥ 0.30 entries appear in the list
        until they drop below threshold or get retired/renamed.

        Calling this endpoint re-runs the cluster computation and
        rebuild step — same cost as ``/api/cluster-snapshot`` because
        Alice may want fresh registry state without paying for the full
        snapshot payload.
        """
        p: Paths = app.state.paths
        nodes, edges, cm = sources.load_memory_graph_bundle(p.mind_dir)
        topical_clusters = [c for c in cm.get("clusters", []) if not c["is_misc"]]
        reg_path = cluster_registry.registry_path()
        prev_registry = cluster_registry.load_registry(reg_path)
        if topical_clusters:
            new_registry, alerts = cluster_registry.rebuild(
                cluster_members={c["id"]: c["member_ids"] for c in topical_clusters},
                cluster_top_hubs={
                    c["id"]: [h["id"] for h in c["top_hubs"]] for c in topical_clusters
                },
                label_by_id={n.id: n.label for n in nodes},
                prev_registry=prev_registry,
            )
            llm_mutated = await _refresh_llm_labels(new_registry, nodes)
            if new_registry != prev_registry or llm_mutated:
                cluster_registry.save_registry(reg_path, new_registry)
        else:
            new_registry, alerts = prev_registry, []
        return JSONResponse(
            {
                "schema_version": new_registry.get("schema_version", 1),
                "generated_at": time.time(),
                "registry_path": str(reg_path),
                "last_rebuild": new_registry.get("last_rebuild"),
                "entries": new_registry.get("entries", {}),
                "drift_alerts": alerts,
            }
        )

    @app.get("/api/interaction-graph")
    async def api_interaction_graph(window_seconds: int | None = None) -> JSONResponse:
        """Interaction graph nodes + edges.

        ``window_seconds`` (optional): if given, drop nodes whose ts is
        older than ``now - window_seconds``. Nodes with ts==0 (e.g. the
        ``directive`` cluster anchor) are always kept. Edges referencing
        dropped nodes are also dropped. We filter at the route boundary
        because ``aggregators.build_interaction_graph`` is shared with
        in-flight work and we want to avoid touching its signature.
        """
        p: Paths = app.state.paths
        events = sources.load_all(p)
        wakes = aggregators.group_wakes(events)
        turns = aggregators.group_turns(events)
        nodes, edges = aggregators.build_interaction_graph(events, wakes, turns)

        if window_seconds is not None and window_seconds > 0:
            cutoff = time.time() - window_seconds
            kept_ids: set[str] = set()
            kept_nodes = []
            for n in nodes:
                # ts==0 sentinels (directive anchor) are always kept.
                if n.ts == 0 or n.ts >= cutoff:
                    kept_nodes.append(n)
                    kept_ids.add(n.id)
            kept_edges = [
                e for e in edges if e.source in kept_ids and e.target in kept_ids
            ]
            nodes, edges = kept_nodes, kept_edges

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
                    {"source": e.source, "target": e.target, "kind": e.kind}
                    for e in edges
                ],
            }
        )

    @app.get("/api/memory/search")
    async def api_memory_search(q: str = "", limit: int = 25) -> JSONResponse:
        """Rank memory notes by token-AND match across label/frontmatter/body."""
        p: Paths = app.state.paths
        results = sources.search_memory(p.mind_dir, q, limit=limit)
        return JSONResponse({"query": q, "results": results})

    @app.get("/api/memory/note")
    async def api_memory_note(id: str) -> JSONResponse:
        """Return a memory note's body + frontmatter for the graph modal."""
        p: Paths = app.state.paths
        # node ids are relative-path-without-suffix, e.g. "memory/foo" or
        # "memory/sources/bar/baz". Rejoin with .md and ensure the result
        # stays inside mind/.
        if id.startswith("unresolved::"):
            return JSONResponse(
                {
                    "id": id,
                    "label": id.split("::", 1)[1],
                    "body": "",
                    "unresolved": True,
                }
            )
        candidate = (p.mind_dir / f"{id}.md").resolve()
        try:
            candidate.relative_to(p.mind_dir.resolve())
        except ValueError:
            return JSONResponse({"error": "path escape"}, status_code=400)
        if not candidate.is_file():
            return JSONResponse({"error": "not found", "id": id}, status_code=404)
        body = candidate.read_text(errors="replace")
        st = candidate.stat()
        return JSONResponse(
            {
                "id": id,
                "path": str(candidate),
                "rel_path": str(candidate.relative_to(p.mind_dir)),
                "label": candidate.stem,
                "body": body,
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
        )

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        s = _state_context()
        # Strip the non-JSON Paths object.
        s.pop("paths", None)
        return JSONResponse(s)

    # ------------------------------------------------------------------
    # Viewer chat — proxies to the speaking daemon's viewer-chat HTTP
    # ingress. The daemon owns conversation state + identity; the viewer
    # is a thin UI layer. See ``alice_speaking.transports.viewer_chat``
    # for the wire shape.

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_view(request: Request):
        return templates.TemplateResponse(
            request,
            "chat.html",
            {
                "state": _state_context(),
                "active": "chat",
            },
        )

    @app.get("/api/chat/history")
    async def api_chat_history(channel: str | None = None, limit: int = 100):
        from . import chat_client

        try:
            payload = await chat_client.fetch_history(
                channel=channel, limit=limit
            )
        except RuntimeError as exc:
            return JSONResponse(
                {"error": str(exc), "kind": "daemon_error"}, status_code=502
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"error": str(exc), "kind": "unreachable"}, status_code=503
            )
        return JSONResponse(payload)

    @app.post("/api/chat/send")
    async def api_chat_send(request: Request):
        from . import chat_client

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "expected JSON body"}, status_code=400
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "expected JSON object"}, status_code=400
            )
        text = body.get("text")
        channel = body.get("channel")
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                {"error": "text must be a non-empty string"}, status_code=400
            )
        try:
            ack = await chat_client.send_message(
                text=text,
                channel=channel if isinstance(channel, str) else None,
            )
        except RuntimeError as exc:
            return JSONResponse(
                {"error": str(exc), "kind": "daemon_error"}, status_code=502
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"error": str(exc), "kind": "unreachable"}, status_code=503
            )
        return JSONResponse(ack, status_code=202)

    @app.get("/api/chat/stream")
    async def api_chat_stream(request: Request, channel: str | None = None):
        """Proxy the daemon's SSE stream through to the browser.

        Re-encodes each daemon event as an SSE ``message`` so the
        browser's :class:`EventSource` reads ``event.data`` and parses
        the JSON itself. Heartbeats are passed through as comments.
        """
        from . import chat_client

        async def gen():
            try:
                async for event in chat_client.stream_events(channel=channel):
                    if await request.is_disconnected():
                        return
                    yield {"event": "message", "data": json.dumps(event)}
            except RuntimeError as exc:
                yield {
                    "event": "error",
                    "data": json.dumps({"message": str(exc)}),
                }
            except Exception as exc:  # noqa: BLE001
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {"message": f"stream failed: {type(exc).__name__}: {exc}"}
                    ),
                }

        return EventSourceResponse(gen())

    @app.get("/api/sidebar", response_class=HTMLResponse)
    async def api_sidebar(request: Request):
        """Sidebar partial. Re-rendered on every SSE `change` event so
        the speaking-stats / thinking-avg / queue counters update live
        without needing a full page reload."""
        return templates.TemplateResponse(
            request,
            "_sidebar.html",
            {"state": _state_context()},
        )

    # ------------------------------------------------------------------
    # Stage D review — dual-judge synthesis labeling pipeline.
    #
    # Spec: cortex-memory/research/2026-05-08-stage-d-cap-redesign-quality-gated.md
    # (sections 9-10) and 2026-05-09-stage-d-labeling-pipeline.md.
    #
    # Storage and join semantics live in ``stage_d_store``; routes here
    # are thin orchestration. The attempts log is read-only from the
    # viewer's side — only the labels sidecar is appended to.

    # Labels we accept from the one-keystroke UI. ``unlabeled`` is the
    # explicit "clear my prior label" signal — a fresh append wins on
    # read so we never delete history.
    _STAGE_D_VALID_LABELS = {"T1", "T2", "T3", "T4", "ship", "reject", "unlabeled"}

    def _stage_d_load_joined() -> list[dict[str, Any]]:
        p: Paths = app.state.paths
        return stage_d_store.load_review_rows(p.mind_dir)

    @app.get("/stage-d-review", response_class=HTMLResponse)
    async def stage_d_review(
        request: Request,
        since: str | None = None,
        status: str | None = None,
        days: int = 7,
        focus: str | None = None,
    ):
        """Morning review surface for the dual-judge Stage D pipeline.

        ``since`` overrides ``days`` when provided (ISO date). Default
        window is the last 7 nights, configurable via ``?days=N``.
        ``status`` filters to one of {disagreement, shipped, dropped,
        unlabeled}. ``focus`` is the attempt id to scroll to / give
        focus to on load — used so the post-label htmx swap can advance
        focus without losing scroll position.
        """
        joined = _stage_d_load_joined()

        # Window resolution: explicit ``since`` wins, else compute from days.
        effective_since = since
        if not effective_since:
            try:
                d = max(1, int(days))
            except (TypeError, ValueError):
                d = 7
            floor = dt.date.today() - dt.timedelta(days=d)
            effective_since = floor.isoformat()

        # Banner counts run over the windowed corpus *before* the status
        # filter — so the counts reflect what the operator can see if
        # they clear the status filter, not just what the current view
        # shows. Otherwise the totals would be circular.
        windowed = stage_d_store.filter_attempts(joined, since=effective_since)
        summary = stage_d_store.summarize(windowed)

        filtered = stage_d_store.filter_attempts(windowed, status=status)
        rows = stage_d_store.default_sort(filtered)

        return templates.TemplateResponse(
            request,
            "stage_d_review.html",
            {
                "rows": rows,
                "summary": summary,
                "since": effective_since,
                "days": days,
                "status": status,
                "focus": focus,
                "valid_labels": sorted(_STAGE_D_VALID_LABELS),
                "state": _state_context(),
                "active": "stage-d-review",
            },
        )

    @app.get("/stage-d-review/rows", response_class=HTMLResponse)
    async def stage_d_review_rows(
        request: Request,
        since: str | None = None,
        status: str | None = None,
        days: int = 7,
        focus: str | None = None,
    ):
        """HTML partial — just the rows. Used by htmx to refresh the
        list after a label without bouncing the whole page (so scroll
        position is preserved).
        """
        joined = _stage_d_load_joined()
        effective_since = since
        if not effective_since:
            try:
                d = max(1, int(days))
            except (TypeError, ValueError):
                d = 7
            floor = dt.date.today() - dt.timedelta(days=d)
            effective_since = floor.isoformat()
        windowed = stage_d_store.filter_attempts(joined, since=effective_since)
        filtered = stage_d_store.filter_attempts(windowed, status=status)
        rows = stage_d_store.default_sort(filtered)
        return templates.TemplateResponse(
            request,
            "_stage_d_rows_partial.html",
            {
                "rows": rows,
                "focus": focus,
                "status": status,
                "since": effective_since,
                "days": days,
                "valid_labels": sorted(_STAGE_D_VALID_LABELS),
            },
        )

    @app.get("/stage-d-review/summary", response_class=HTMLResponse)
    async def stage_d_review_summary(
        request: Request,
        since: str | None = None,
        days: int = 7,
    ):
        """HTML partial — the counts banner. Re-rendered after each
        label so the unlabeled count ticks down live.
        """
        joined = _stage_d_load_joined()
        effective_since = since
        if not effective_since:
            try:
                d = max(1, int(days))
            except (TypeError, ValueError):
                d = 7
            floor = dt.date.today() - dt.timedelta(days=d)
            effective_since = floor.isoformat()
        windowed = stage_d_store.filter_attempts(joined, since=effective_since)
        summary = stage_d_store.summarize(windowed)
        return templates.TemplateResponse(
            request,
            "_stage_d_summary_partial.html",
            {"summary": summary, "since": effective_since, "days": days},
        )

    @app.get("/api/stage-d-attempts")
    async def api_stage_d_attempts(
        since: str | None = None,
        status: str | None = None,
    ) -> JSONResponse:
        """Joined attempts as JSON. ``since=YYYY-MM-DD`` and
        ``status=disagreement|shipped|dropped|unlabeled`` supported."""
        joined = _stage_d_load_joined()
        filtered = stage_d_store.filter_attempts(joined, since=since, status=status)
        return JSONResponse(stage_d_store.default_sort(filtered))

    @app.get("/api/stage-d-summary")
    async def api_stage_d_summary(since: str | None = None) -> JSONResponse:
        """Counts banner: ``{shipped, dropped, disagreement, total, unlabeled}``."""
        joined = _stage_d_load_joined()
        windowed = stage_d_store.filter_attempts(joined, since=since)
        return JSONResponse(stage_d_store.summarize(windowed))

    @app.post("/api/stage-d-label")
    async def api_stage_d_label(request: Request) -> JSONResponse:
        """Append a label record to ``stage-d-labels.jsonl``.

        Accepts JSON ``{"attempt_id", "label", "axes"?}`` *or*
        form-urlencoded with the same keys (htmx default). The labels
        log is append-only — re-labeling is just another append, and
        the join logic picks the newest entry.
        """
        # Parse body without depending on python-multipart: try JSON
        # first, fall back to manually-parsed form-urlencoded.
        body = await request.body()
        ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        data: dict[str, Any] = {}
        if ct == "application/json" or (body and body.lstrip().startswith(b"{")):
            try:
                data = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        else:
            from urllib.parse import parse_qsl

            data = dict(parse_qsl(body.decode("utf-8", errors="replace")))

        attempt_id = (data.get("attempt_id") or "").strip()
        label = (data.get("label") or "").strip()
        axes = data.get("axes") or data.get("label_axes")

        if not attempt_id:
            return JSONResponse({"error": "attempt_id required"}, status_code=400)
        if label not in _STAGE_D_VALID_LABELS:
            return JSONResponse(
                {
                    "error": f"invalid label {label!r}; expected one of "
                    f"{sorted(_STAGE_D_VALID_LABELS)}"
                },
                status_code=400,
            )
        if axes is not None and not isinstance(axes, dict):
            # Accept JSON-string axes from form-encoded clients.
            try:
                axes = json.loads(axes)
            except (TypeError, ValueError):
                axes = None

        p: Paths = app.state.paths
        try:
            rec = stage_d_store.append_label(
                p.mind_dir,
                attempt_id=attempt_id,
                label=label,
                axes=axes if isinstance(axes, dict) else None,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "label_record": rec})

    # ------------------------------------------------------------------
    # SSE live tail

    @app.get("/stream/changes")
    async def stream_changes(request: Request):
        """Coarse change-watcher. Polls ``load_all_signature`` on a
        short tick and emits an ``event: change`` SSE message whenever
        the signature shifts (new log lines, new artifact files).

        The signature itself is cheap (a handful of stat() calls + a
        bounded directory walk over inner/), and the consumers downstream
        bind ``hx-trigger="sse:change"`` to their refetch endpoints so
        the actual rendering work only happens when there's new data.
        Sends a periodic comment-line heartbeat so intermediate proxies
        don't time out the connection on idle workspaces.
        """
        p: Paths = app.state.paths
        last_sig = sources.load_all_signature(p)

        # Emit one initial change so consumers can do a first refetch on
        # connect — handy if the page was rendered slightly stale by a
        # request that beat the latest log append. Cheap and idempotent.
        async def gen():
            yield {"event": "change", "data": ""}
            ticks = 0
            nonlocal last_sig
            while True:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(2.0)
                ticks += 1
                try:
                    sig = sources.load_all_signature(p)
                except Exception:  # noqa: BLE001
                    continue
                if sig != last_sig:
                    last_sig = sig
                    yield {"event": "change", "data": ""}
                elif ticks % 15 == 0:
                    # ~30s heartbeat keeps long-lived proxies happy.
                    yield {"event": "ping", "data": ""}

        return EventSourceResponse(gen())

    @app.get("/stream")
    async def stream(request: Request):
        p: Paths = app.state.paths
        thinking_path = p.thinking_log
        speaking_path = p.speaking_log

        # Start from current end-of-file so we only ship new events.
        offsets = {
            thinking_path: thinking_path.stat().st_size
            if thinking_path.is_file()
            else 0,
            speaking_path: speaking_path.stat().st_size
            if speaking_path.is_file()
            else 0,
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


def _tokens(n: int | float | None) -> str:
    if not n:
        return "0"
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.2f}M"


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
    reload = bool(os.environ.get("ALICE_VIEWER_RELOAD"))
    kwargs: dict[str, object] = {
        "host": host,
        "port": port,
        "factory": True,
        "reload": reload,
    }
    if reload:
        # Scope the watcher to the viewer package — without this, uvicorn
        # watches the process CWD (the repo root inside the container),
        # which means every unrelated edit anywhere in the monorepo
        # triggers a restart. Jinja2 templates already auto-reload, but
        # they live under this same dir so they're covered too. Issue #130.
        kwargs["reload_dirs"] = [str(BASE_DIR)]
    uvicorn.run("viewer.main:create_app", **kwargs)


if __name__ == "__main__":  # pragma: no cover
    run()
