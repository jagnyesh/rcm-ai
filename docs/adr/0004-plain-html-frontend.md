# 0004. A plain HTML and JavaScript frontend with no build step

- Status: Proposed
- Date: 2026-11-19
- Deciders: the maintainer

## Context

The demo page is one screen: a case picker, the note, the evidence, the verdict with its steps, the letter, and a sources panel. the maintainer's existing interactive pages, the rcm-ai architecture explorer and merge-works, are single self-contained HTML files with no build step. The portfolio site uses Astro.

## Decision

Write the page as static HTML, CSS and plain JavaScript in `public/` (for example `public/index.html`, `public/app.js`). Vercel serves `public/` from its CDN. The page:
- reads the recorded JSON (ADR 0003);
- for live mode, calls `/api/live` and reads its streamed events (ADR 0006).

It uses the explorer's look so the two pages feel like one project, and links to the explorer and the repo.

## Options considered

1. **Plain HTML and JavaScript (chosen).** No toolchain beyond the Python one, nothing to update, matches the maintainer's other pages.
2. **Next.js or another React framework.** Vercel's home turf, but it adds Node, a build and a framework for one screen. It would also make the repo half TypeScript.
3. **Astro, as on the portfolio site.** Familiar to the maintainer, but it adds a Node build to a Python repo for little gain.
4. **Server-rendered HTML from FastAPI with templates.** No JavaScript, but every page view becomes a function call and pays the cold start.

## Consequences

- No `package.json` in the rcm-ai repo.
- Plain JavaScript reads a streamed response with `fetch` and a stream reader. That is about 30 lines, worth a comment in the file.
- Whether Vercel serves `public/` alongside a FastAPI app needs checking on the first preview deploy. If it doesn't, FastAPI's `StaticFiles` serves the same folder.
