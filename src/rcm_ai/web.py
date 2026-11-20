"""The web demo's one function (ADR 0005): a health check now, and the gated live path later.

Recorded mode needs none of this: the page in public/ reads public/demo-cases.json from the CDN.
This app writes nothing to disk. Live mode (ADR 0006, 0008) is off in this version and answers 503
until it is built and its API key, spending cap and access code are set up.
"""

import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from rcm_ai.graph import IngestError, build_graph
from rcm_ai.provenance import ProvenanceError

app = FastAPI(title="rcm-ai demo", docs_url=None, redoc_url=None, openapi_url=None)


def data_dir() -> Path:
    """RCM_AI_DATA if set, else data/ in the working directory (the project root on Vercel), else the checkout's."""
    if env := os.environ.get("RCM_AI_DATA"):
        return Path(env)
    cwd = Path.cwd() / "data"
    return cwd if cwd.is_dir() else Path(__file__).resolve().parents[2] / "data"


@lru_cache(maxsize=1)
def _graph():
    return build_graph(data_dir())


@app.get("/api/health")
def health() -> JSONResponse:
    body = {"commit": os.environ.get("VERCEL_GIT_COMMIT_SHA"), "live_mode": "off"}
    try:
        graph = _graph()
    except (IngestError, ProvenanceError, OSError) as e:
        body |= {"status": "error", "graph": {"loaded": False, "error": f"{type(e).__name__}: {e}"}}
        return JSONResponse(body, status_code=503)
    body |= {"status": "ok", "graph": {"loaded": True, "sources": [
        {"path": s.path, "parsed": s.parsed, "rejected": len(s.rejected), "in_graph": s.in_graph}
        for s in graph.ingest]}}
    return JSONResponse(body)


@app.post("/api/live")
def live() -> JSONResponse:
    return JSONResponse({"detail": "Live mode is not enabled on this deployment. The recorded cases are on the page."},
                        status_code=503)


if not os.environ.get("VERCEL"):
    # Local preview only (`uv run uvicorn rcm_ai.web:app`); on Vercel the CDN serves public/.
    from fastapi.staticfiles import StaticFiles

    _public = Path(__file__).resolve().parents[2] / "public"
    if _public.is_dir():
        app.mount("/", StaticFiles(directory=_public, html=True), name="public")
