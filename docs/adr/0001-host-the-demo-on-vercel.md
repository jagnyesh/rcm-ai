# 0001. Host the live demo on Vercel's Hobby plan, from the rcm-ai repo

- Status: Proposed
- Date: 2026-11-19
- Deciders: the maintainer

## Context

rcm-ai runs as a command-line tool today. Its architecture explorer is a static page on GitHub Pages (`jagnyesh.github.io/rcm-ai/...`). A live demo has to run Python: rdflib builds the graph, `adjudicate` decides, and the live path calls the Anthropic API. GitHub Pages serves static files only, so it can't run that code or hold an API key.

What the demo needs from a host:
- Python 3.13, which `pyproject.toml` requires (`requires-python = ">=3.13"`).
- A request time long enough for two or three Opus 5.5 calls at effort high, run one after another.
- A secret store for the API key.
- Per-IP rate limiting.
- Little to operate. the maintainer has not run cloud infrastructure before, so fewer moving parts matters.

## Decision

Deploy the demo as a Vercel project on the Hobby (free) plan, connected to the rcm-ai GitHub repo. The demo code lives in that repo, so the deployed demo always runs the same code and data as the eval. The portfolio site stays on GitHub Pages.

Vercel facts this rests on (Vercel docs, 2026-11-19):
- Python 3.13 is supported, chosen by `.python-version` (the repo already has `3.13`) or `requires-python`.
- Dependencies install from `pyproject.toml` and `uv.lock` with uv.
- FastAPI apps deploy with no build configuration.
- On Hobby, with Fluid compute (on by default), a function runs up to 300 seconds, with 2 GB of memory.
- Python function bundles can be up to 500 MB. rcm-ai with its environment is about 40 MB.
- Environment variables can be stored as write-only Secrets.
- WAF rate limiting is available on every plan; Hobby gets one rate-limit rule per project.

## Options considered

1. **Vercel Hobby (chosen).** Free, deploys on every push, preview deploys per branch, built-in secrets and rate limiting. The limits: 300 seconds per request, one rate-limit rule, and no spend controls. On Hobby, going over the included monthly usage pauses the feature for 30 days rather than billing.
2. **Vercel Pro ($20 a month per seat).** Longer functions (800 s), more WAF rules, spend management. Not needed for a demo of this size; easy to move to later.
3. **A container host (Render, Fly.io, Railway, Cloud Run).** A long-running server avoids cold starts, but the maintainer would run and patch a server, and a container that is always on costs money.
4. **Static only, on GitHub Pages.** Precomputed results with no live path. Free and can't break, but it can't show the model reading a note, which is half the system. ADR 0003 keeps this as the default mode anyway.

## Consequences

- One account to create (Vercel, signed in with GitHub) and one project to import.
- Every live request has to finish inside 300 seconds, so the live path runs at most three model calls (ADR 0006).
- No spend controls on Hobby, so the cost cap lives at the Anthropic side (ADR 0008).
- Vercel's Hobby terms for personal versus commercial use were not checked here; a personal portfolio demo is the expected case, but the maintainer should read the Terms of Service once.
- Runtime logs on Hobby are kept one hour, which is enough for debugging a demo.
