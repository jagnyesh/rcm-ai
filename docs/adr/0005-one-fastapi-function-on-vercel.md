# 0005. One FastAPI function on Vercel, not a separate backend

- Status: Proposed
- Date: 2026-11-19
- Deciders: the maintainer

## Context

Live mode needs server-side Python: rcm-ai's own extraction, support check, adjudication, drafting and citation check, run with a secret key. The CLI path (`_triage` in `src/rcm_ai/__init__.py`) also writes logs under `eval-results/`, relative to the working directory. On Vercel the filesystem is read-only except `/tmp`, which is per instance and doesn't persist.

## Decision

Add a small FastAPI app, for example `src/rcm_ai/web.py`, and point Vercel at it with `[tool.vercel] entrypoint = "rcm_ai.web:app"` in `pyproject.toml`. Vercel runs the whole app as one function. It has three routes:
- `GET /api/health` returns the git SHA and whether the key is configured, never the key itself;
- `POST /api/live` runs one live case and streams its steps (ADR 0006);
- everything else is the static page from `public/`.

The app imports rcm-ai's functions directly (`load_case`, `build_graph`, `ex.triage_note`, `ap.draft`, the citation checker) and never calls the CLI. It builds the graph once per instance, at import time, so a warm instance doesn't rebuild it. It writes nothing to disk: logs that the CLI would write are dropped, or kept in memory for the response.

`vercel.json` sets `maxDuration` to 300 seconds for the function, the Hobby maximum, and excludes `tests/`, `docs/` and `data/raw/` from the bundle.

## Options considered

1. **One FastAPI function in the same repo (chosen).** One deploy, one place for code, the same code as the eval.
2. **A separate API on a container host, with the page on Vercel or GitHub Pages.** No 300-second limit and no cold start, but two hosts to run and pay for.
3. **Several file-based functions under `api/`.** Works, but FastAPI gives request validation and streaming responses with less code, and Vercel detects it with no configuration.

## Consequences

- `fastapi` becomes a dependency. Adding it to `[project] dependencies` is the simplest route, since Vercel installs from `pyproject.toml` and `uv.lock`. It is unverified whether Vercel installs dependency groups, so a `web` group would need checking on the first preview deploy.
- Two things need checking on the first preview deploy:
  - whether Vercel installs the `src/` package itself, or the entrypoint needs `src` on `sys.path`;
  - whether `build_graph(Path("data"))` finds the data. Vercel says relative paths resolve from the project root, so it should.
- A cold instance pays for importing rdflib and the SDK and building the graph, about 1 to 2 seconds measured locally. That only touches live mode.
