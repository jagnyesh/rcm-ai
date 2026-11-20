# 0006. Live model calls stream progress; the API key stays server-side in its own workspace

- Status: Proposed
- Date: 2026-11-19
- Deciders: the maintainer

## Context

A live run is up to three calls to `claude-opus-5-5` at effort high, one after another:
1. extraction;
2. the support check, if the extraction validates;
3. a draft, for an overturnable verdict.

The repo's records don't hold call durations, but each call at effort high is likely tens of seconds, so a run can take a minute or more. Hobby allows 300 seconds per request.

The 2026-11-01 eval run made 61 calls with 97,919 input and 57,502 output tokens. At Opus 5.5's $4 and $20 per million tokens, that is about $1.54, or about 2.5 cents per call. A live run of three calls is therefore about 8 cents, and under 15 cents allowing for long drafts.

## Decision

**Streaming.** `POST /api/live` returns a streamed response, one JSON line per event. The events are: extraction started, extraction done (with the spans), support check done, verdict (with the path), draft done (with the letter and its citation check), or an error. The page shows each as it arrives, so a slow call reads as progress, not a hang. The request has one overall deadline, under the 300-second limit, and returns `needs_human` with the reason "the model didn't answer in time" if it passes.

**Same model and settings as the eval.** The demo uses the repo's own `MODEL`, `EFFORT`, `MAX_TOKENS` and prompt files, so what a visitor sees is the system the README measures. The page names the model, the effort and the git SHA.

**Key management.**
- the maintainer creates a separate Anthropic Console workspace for the demo (for example `rcm-ai-demo`), sets a monthly spend limit on it, and creates one API key there.
- The key goes into Vercel as a Secret environment variable named `ANTHROPIC_API_KEY`, for the Production environment only. Secrets are write-only after saving.
- The key is read only by the server, never sent to the browser, never logged, and never in the repo. `.env` is already gitignored.
- Preview deployments get no key, so live mode is off on previews unless the maintainer adds one on purpose.
- If the key leaks, the maintainer deletes it in the Console and makes a new one; the spend limit caps the damage meanwhile.

## Options considered

1. **One streamed request (chosen).** One request per run, and the page shows each step as it arrives.
2. **One request per step, called by the page in turn.** Each request is short, but the page would orchestrate the pipeline and pass intermediate results back, so a visitor could send the server a doctored extraction.
3. **A cheaper or faster model for the demo.** Faster and cheaper, but then the demo isn't the system the README measures. the maintainer can still decide this.
4. **The visitor brings their own key.** No cost to the maintainer, but the hiring manager won't have one, and asking for a key on a page is a bad pattern to teach.

## Consequences

- If the API is slow or down, the run ends as a hand-off with a reason, which is how rcm-ai already treats a refused or truncated response.
- the maintainer watches spend in the Anthropic Console, where the workspace shows its usage.
- The live path counts calls, as the eval's run records do, and reports them on the page.
