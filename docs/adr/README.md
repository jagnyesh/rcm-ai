# Architecture decision records: rcm-ai live demo

Each record states one decision about the live demo and its deployment, the options weighed, and what follows from it. Format: a short MADR-style record (context, decision, options, consequences). All are **Proposed** until the maintainer accepts them; change the status line to `Accepted` or `Rejected` when decided.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-host-the-demo-on-vercel.md) | Host the live demo on Vercel's Hobby plan, from the rcm-ai repo | Proposed |
| [0002](0002-recorded-mode-first-live-mode-gated.md) | Recorded mode by default; live mode behind an access code | Proposed |
| [0003](0003-precompute-recorded-results-as-committed-json.md) | Precompute recorded results as committed JSON, checked by a test | Proposed |
| [0004](0004-plain-html-frontend.md) | A plain HTML and JavaScript frontend with no build step | Proposed |
| [0005](0005-one-fastapi-function-on-vercel.md) | One FastAPI function on Vercel, not a separate backend | Proposed |
| [0006](0006-live-llm-calls-and-key-management.md) | Live model calls stream progress; the API key stays server-side in its own workspace | Proposed |
| [0007](0007-synthetic-data-only-no-phi.md) | Synthetic data only: bundled cases, no free-form claims, nothing stored | Proposed |
| [0008](0008-rate-limits-and-cost-cap.md) | Rate limit and cap cost in three layers | Proposed |
| [0009](0009-link-from-the-portfolio-site.md) | Link the demo from jagnyesh.github.io on its vercel.app address | Proposed |

Facts about Vercel in these records come from Vercel's own docs, read on 2026-11-19. Recheck them before acting on them.
