# 0009. Link the demo from jagnyesh.github.io on its vercel.app address

- Status: Proposed
- Date: 2026-11-19
- Deciders: the maintainer

## Context

The portfolio site (Astro, on GitHub Pages) lists projects from `src/data/projects.ts`. Each project has one optional `demo` link; rcm-ai's points to the architecture explorer. Vercel gives each project a free `<project>.vercel.app` address. the maintainer's site has no custom domain.

## Decision

- Deploy the demo at the free address, for example `rcm-ai.vercel.app` if that name is free, or whatever Vercel assigns.
- In `projects.ts`, change `demo` to a list (`demos`) so a project can carry several links, and give rcm-ai two: "Live demo" and "Architecture explorer". Update `ProjectCard.astro` to render the list. The site's link checker (`npm run check:links`) then covers the new link.
- Add a "Live demo" line to the rcm-ai README next to the explorer link.
- The demo page links back to the explorer, the repo and the portfolio site.

## Options considered

1. **The vercel.app address, with two links on the card (chosen).** Free, no DNS to manage.
2. **A custom domain, such as `demo.<the maintainer's domain>`.** Looks more polished, but the maintainer would buy a domain and set DNS records. It is easy to add later without changing the deploy.
3. **Replace the explorer link with the demo link.** Simpler card, but it hides the explorer, which answers "how does it work" better than the demo does.

## Consequences

- A small change to the portfolio site, in its own pull request, after the demo is live.
- If the maintainer later adds a custom domain, only the URL in `projects.ts` and the README changes.
