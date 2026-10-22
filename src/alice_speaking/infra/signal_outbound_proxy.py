"""Logging pass-through proxy in front of signal-cli's HTTP daemon.

Listens where signal-cli used to (``0.0.0.0:8080``) and forwards every
request verbatim to signal-cli on a loopback port. For ``POST
/api/v1/rpc`` bodies containing ``send`` calls it appends a capture
record to the outbound JSONL (see :mod:`alice_speaking.signal_outbound`)
*after* the upstream replies. Capture failures are swallowed — the
proxy must never change what clients see.

Run: ``python -m alice_speaking.infra.signal_outbound_proxy``.
Env:
  SIGNAL_PROXY_LISTEN    host:port to bind      (default 0.0.0.0:8080)
  SIGNAL_PROXY_UPSTREAM  signal-cli base URL    (default http://127.0.0.1:18080)
  ALICE_SIGNAL_OUTBOUND_LOG  JSONL path         (default /state/worker/signal-outbound.jsonl)
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Optional

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from alice_speaking import signal_outbound

log = logging.getLogger("signal_outbound_proxy")

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _fwd_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def build_app(
    upstream: str,
    log_path: pathlib.Path,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Starlette:
    client = httpx.AsyncClient(
        base_url=upstream,
        transport=transport,
        # signal-cli sends can take several seconds; /api/v1/events is a
        # long-lived SSE stream, so no read timeout.
        timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=None),
    )

    def _capture(
        body: bytes, resp_body: Optional[bytes], client_host: Optional[str]
    ) -> None:
        try:
            parsed = json.loads(body)
        except ValueError:
            return
        resp = None
        if resp_body:
            try:
                resp = json.loads(resp_body)
            except ValueError:
                resp = None
        try:
            recs = signal_outbound.records_from_rpc(parsed, resp, client=client_host)
            signal_outbound.append_records(recs, log_path)
        except Exception:  # noqa: BLE001 — capture must never break sends
            log.exception("outbound capture failed")

    async def proxy(request: Request) -> Response:
        body = await request.body()
        path = request.url.path
        url = (
            httpx.URL(path=path, query=request.url.query.encode())
            if request.url.query
            else path
        )
        req = client.build_request(
            request.method, url, headers=_fwd_headers(request.headers), content=body
        )
        client_host = request.client.host if request.client else None
        is_rpc = request.method == "POST" and path.rstrip("/") == "/api/v1/rpc"
        if is_rpc and b'"send"' in body:
            # Buffer RPC replies so we can record ok/error.
            try:
                upstream_resp = await client.send(req)
            except httpx.HTTPError as exc:
                _capture(body, None, client_host)
                return Response(f"signal-cli upstream error: {exc}", status_code=502)
            _capture(body, upstream_resp.content, client_host)
            return Response(
                upstream_resp.content,
                status_code=upstream_resp.status_code,
                headers=_fwd_headers(upstream_resp.headers),
            )
        try:
            upstream_resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            return Response(f"signal-cli upstream error: {exc}", status_code=502)
        return StreamingResponse(
            upstream_resp.aiter_raw(),
            status_code=upstream_resp.status_code,
            headers=_fwd_headers(upstream_resp.headers),
            background=BackgroundTask(upstream_resp.aclose),
        )

    methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    app = Starlette(routes=[Route("/{path:path}", proxy, methods=methods)])
    app.state.client = client
    return app


def main() -> None:
    import uvicorn

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    listen = os.environ.get("SIGNAL_PROXY_LISTEN", "0.0.0.0:8080")
    host, _, port = listen.rpartition(":")
    upstream = os.environ.get("SIGNAL_PROXY_UPSTREAM", "http://127.0.0.1:18080")
    log_path = signal_outbound.DEFAULT_LOG_PATH
    log.info(
        "signal outbound proxy %s -> %s, logging to %s", listen, upstream, log_path
    )
    app = build_app(upstream, log_path)
    uvicorn.run(
        app,
        host=host or "0.0.0.0",
        port=int(port),
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
