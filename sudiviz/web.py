"""FastAPI server backing the live web visualization.

Endpoints
---------
GET  /              → index.html (Cytoscape.js + sidebar)
GET  /static/*      → CSS/JS assets
GET  /graph         → Cytoscape elements payload
GET  /diagnose      → fix suggestions
WS   /ws            → live updates (full graph push every refresh tick)

Discovery is cached for `refresh_interval` seconds so multiple browsers do not
trigger N parallel sweeps. The WS broadcast loop is the single source that
performs discovery — all HTTP endpoints read the cached value.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import networkx as nx

from sudiviz.discovery.aws import discover_all
from sudiviz.discovery.models import CloudProvider
from sudiviz.graph.analyzer import diagnose, mark_orphaned_edges
from sudiviz.graph.builder import build_graph
from sudiviz.graph.visualizer import export_cytoscape_json
from sudiviz.web_templates.icons import all_cytoscape_uris, all_img_uris

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "web_templates"

# Render index.html once at startup: substitute {{ICON_<kind>}} placeholders
# with real base64 data URIs. Falls back to the placeholder string itself
# (which renders as a broken image) if the icon directory is absent.
def _render_index_html() -> str:
    html = (TEMPLATE_DIR / "index.html").read_text()
    # Cytoscape background-image: percent-encoded SVG (base64 SVG is rejected by Cytoscape)
    for kind, uri in all_cytoscape_uris().items():
        html = html.replace("{{ICON_" + kind + "}}", uri)
    # Legend <img> src: base64 (works in HTML img tags)
    for kind, uri in all_img_uris().items():
        html = html.replace("{{ICON_IMG_" + kind + "}}", uri)
    return html

_INDEX_HTML = _render_index_html()


@dataclass
class ServerConfig:
    vpc_id: Optional[str] = None
    service_tag: Optional[str] = None
    profile: Optional[str] = None
    region: Optional[str] = None
    provider: str = "aws"
    project: Optional[str] = None
    refresh_interval: float = 30.0


@dataclass
class CachedState:
    """Latest discovery + graph + diagnosis. Mutated by the refresh loop."""

    graph: Optional[nx.DiGraph] = None
    cytoscape: dict[str, Any] = field(default_factory=dict)
    diagnosis: dict[str, Any] = field(default_factory=dict)
    last_refresh: Optional[datetime] = None
    error: Optional[str] = None


class WebSocketHub:
    """Tiny pub/sub hub for fan-out broadcasts to connected browsers."""

    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: Any) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: Any) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        msg = json.dumps(payload, default=str)
        async with self._lock:
            stale: list[Any] = []
            for ws in self._clients:
                try:
                    await ws.send_text(msg)
                except Exception:  # noqa: BLE001
                    stale.append(ws)
            for ws in stale:
                self._clients.discard(ws)


def create_app(config: ServerConfig):  # type: ignore[no-untyped-def]
    """Build the FastAPI app. Importing FastAPI here keeps it optional."""
    try:
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "FastAPI is not installed. Install the web extras: `pip install sudiviz[web]`."
        ) from exc

    # Per-(provider, region) cache → CachedState
    region_cache: dict[str, CachedState] = {}
    hub = WebSocketHub()

    def _cache_key(provider: Optional[str], region: Optional[str]) -> str:
        p = provider or config.provider or "aws"
        r = region or config.region or "default"
        return f"{p}:{r}"

    def _get_state(provider: Optional[str] = None, region: Optional[str] = None) -> CachedState:
        key = _cache_key(provider, region)
        if key not in region_cache:
            region_cache[key] = CachedState()
        return region_cache[key]

    async def _discover(provider: str, region: Optional[str]):
        """Dispatch discovery to the correct provider."""
        if provider == "gcp":
            try:
                from sudiviz.discovery.gcp import discover_all_gcp
            except ImportError as exc:
                raise RuntimeError(
                    "GCP SDK not installed. Install with: pip install sudiviz[gcp]"
                ) from exc
            return await discover_all_gcp(
                project=config.project,
                region=region or config.region,
                vpc_network=config.vpc_id,
                service_tag=config.service_tag,
            )
        return await discover_all(
            vpc_id=config.vpc_id,
            service_tag=config.service_tag,
            profile=config.profile,
            region=region or config.region,
        )

    async def refresh_region(
        region: Optional[str],
        provider: Optional[str] = None,
    ) -> CachedState:
        effective_provider = provider or config.provider or "aws"
        state = _get_state(effective_provider, region)
        try:
            discovery = await _discover(effective_provider, region)
            graph = mark_orphaned_edges(build_graph(discovery))
            state.graph = graph
            state.cytoscape = export_cytoscape_json(graph, region=discovery.region)
            diag_dict = diagnose(graph).to_dict()
            diag_dict["region"] = discovery.region  # lets the WS client filter by region
            diag_dict["provider"] = effective_provider  # lets the WS client filter by provider
            state.diagnosis = diag_dict
            state.last_refresh = datetime.utcnow()
            state.error = None
            # Only broadcast to WS clients for the primary/default provider+region.
            primary_provider = config.provider or "aws"
            if (effective_provider == primary_provider) and (region is None or region == config.region):
                await hub.broadcast({"type": "graph", "graph": state.cytoscape})
                await hub.broadcast({"type": "diagnosis", "diagnosis": state.diagnosis})
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh loop error")
            state.error = str(exc)
        return state

    async def refresh_loop() -> None:
        while True:
            await refresh_region(None)
            await asyncio.sleep(config.refresh_interval)

    @asynccontextmanager
    async def lifespan(app):  # noqa: ARG001 — fastapi requires this signature
        task = asyncio.create_task(refresh_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="sudiviz", lifespan=lifespan)

    # Starlette ≥1.0 rejects WebSocket upgrades whose Origin does not match the
    # server host.  Adding CORSMiddleware with a permissive local-dev policy
    # fixes the 403 errors browsers see on /ws.
    from starlette.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.mount("/static", StaticFiles(directory=str(TEMPLATE_DIR)), name="static")

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)

    @app.get("/graph")
    async def graph_endpoint(
        region: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> JSONResponse:
        state = _get_state(provider, region)
        if state.error:
            return JSONResponse({"error": state.error}, status_code=503)
        if not state.cytoscape:
            state = await refresh_region(region, provider)
        if state.error:
            return JSONResponse({"error": state.error}, status_code=503)
        return JSONResponse(state.cytoscape)

    @app.get("/diagnose")
    async def diagnose_endpoint(
        region: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> JSONResponse:
        state = _get_state(provider, region)
        if state.error:
            return JSONResponse({"error": state.error}, status_code=503)
        if not state.diagnosis:
            state = await refresh_region(region, provider)
        if state.error:
            return JSONResponse({"error": state.error}, status_code=503)
        return JSONResponse(state.diagnosis)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        primary = _get_state()
        return JSONResponse(
            {
                "ok": primary.error is None,
                "last_refresh": primary.last_refresh.isoformat() if primary.last_refresh else None,
                "error": primary.error,
            }
        )

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await hub.connect(ws)
        primary = _get_state()
        if primary.cytoscape:
            await ws.send_text(json.dumps({"type": "graph", "graph": primary.cytoscape}, default=str))
        if primary.diagnosis:
            await ws.send_text(json.dumps({"type": "diagnosis", "diagnosis": primary.diagnosis}, default=str))
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await hub.disconnect(ws)

    return app


def serve(
    config: Optional[ServerConfig] = None,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Start the web server (blocking call)."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("uvicorn is not installed. `pip install sudiviz[web]`") from exc

    cfg = config or ServerConfig()
    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="info")
