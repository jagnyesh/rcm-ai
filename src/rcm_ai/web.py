"""The web demo's one function (ADR 0005): a health check and the gated live path.

Recorded mode needs none of this: the page in public/ reads public/demo-cases.json from the CDN.
This app writes nothing to disk. Live mode (ADR 0006, 0008, and rcm_ai.live) runs only when
LIVE_MODE is "on" and the API key and access code are set, or "stand-in" with the access code
(canned answers, no model call); otherwise it answers 503.
"""

import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from rcm_ai import live as lv
from rcm_ai.evaluate import load_eval_set
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


@lru_cache(maxsize=1)
def _cases() -> dict:
    return {ec.case.id: ec.case for ec in load_eval_set(data_dir() / "eval").cases}


def _backend(case) -> lv.Backend:
    return lv.stand_in_backend(case.evidence) if lv.mode() == "stand-in" else lv.anthropic_backend()


@app.get("/api/health")
def health() -> JSONResponse:
    body = {"commit": os.environ.get("VERCEL_GIT_COMMIT_SHA"), "live_mode": lv.mode() if lv.status()[0] else "off"}
    try:
        graph = _graph()
    except (IngestError, ProvenanceError, OSError) as e:
        body |= {"status": "error", "graph": {"loaded": False, "error": f"{type(e).__name__}: {e}"}}
        return JSONResponse(body, status_code=503)
    body |= {"status": "ok", "graph": {"loaded": True, "sources": [
        {"path": s.path, "parsed": s.parsed, "rejected": len(s.rejected), "in_graph": s.in_graph}
        for s in graph.ingest]}}
    return JSONResponse(body)


class LiveRequest(BaseModel):
    case: str
    note: str | None = None  # the case's own note when omitted


@app.post("/api/live", response_model=None)
def live(request: LiveRequest, x_access_code: str | None = Header(default=None)) -> JSONResponse | StreamingResponse:
    """One case through extraction, the support check, the graph and, if overturnable, the draft
    and its citation check, streamed as one JSON line per step. Every gate runs before any call."""
    on, why = lv.status()
    if not on:
        return JSONResponse({"detail": why}, status_code=503)
    if not lv.code_ok(x_access_code):
        return JSONResponse({"detail": "That access code isn't right."}, status_code=403)
    case = _cases().get(request.case)
    if case is None:
        return JSONResponse({"detail": f"No bundled case {request.case!r}."}, status_code=404)
    note = case.note if request.note is None else request.note
    if not note.strip() or len(note) > lv.NOTE_LIMIT:
        return JSONResponse({"detail": f"The note must be 1 to {lv.NOTE_LIMIT} characters."}, status_code=422)
    return StreamingResponse(lv.stream(case.claim, note, _graph(), _backend(case)),
                             media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


if not os.environ.get("VERCEL"):
    # Local preview only (`uv run uvicorn rcm_ai.web:app`); on Vercel the CDN serves public/.
    from fastapi.staticfiles import StaticFiles

    _public = Path(__file__).resolve().parents[2] / "public"
    if _public.is_dir():
        app.mount("/", StaticFiles(directory=_public, html=True), name="public")
