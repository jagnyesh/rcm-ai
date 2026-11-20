# 0003. Precompute recorded results as committed JSON, checked by a test

- Status: Proposed
- Date: 2026-11-19
- Deciders: the maintainer

## Context

The graph arm is deterministic: the same case and the same pinned fixtures always give the same verdict and path. Building the graph takes about half a second, and a whole `rcm-ai triage` run takes about 1.5 seconds locally. On Vercel, a function that hasn't run recently starts cold, so the first request also pays for importing rdflib and the Anthropic SDK.

The repo already pins its inputs and fails tests when they drift: `MANIFEST.json` holds the fixtures' sha256 values, and `test_review.py` checks review tags.

## Decision

Add a command (for example `rcm-ai export-demo`) that runs the graph arm on every bundled case and writes one JSON file the page reads. The file holds, per case:
- the claim, note and evidence;
- the verdict, with its path, findings and reasons, and each hop's source;
- the recorded letter where there is one.

It also records the git SHA, the eval set's sha256 and the fixture manifest. Commit the file, and add a test that regenerates it and fails if it differs from the committed copy.

Vercel serves the file as a static asset from its CDN. Recorded mode makes no function call at all.

## Options considered

1. **Committed JSON, checked by a test (chosen).** Instant, no cold start, works even if the Python function fails. Matches how the repo already pins data.
2. **Run the graph per request in the function.** Always current, but every visit pays the cold start, and recorded mode breaks if the function breaks.
3. **Generate the JSON during the Vercel build.** Always current without a commit, but it depends on how Vercel's Python build step runs custom commands, which wasn't verified, and a build failure takes the demo down.

## Consequences

- When the code, prompts or data change, the maintainer reruns the export command; the test says so if it is forgotten.
- The page shows the SHA it was built from, as the architecture explorer does.
- Live mode (ADR 0005) still builds the graph in the function, because it adjudicates evidence that is extracted at request time.
