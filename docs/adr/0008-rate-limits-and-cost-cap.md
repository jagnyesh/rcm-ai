# 0008. Rate limit and cap cost in three layers

- Status: Proposed
- Date: 2026-11-19
- Deciders: the maintainer

## Context

Hobby has no spend management on the Vercel side, and the cost that matters is the Anthropic API, not Vercel. Serverless instances share no memory, so an in-process counter doesn't limit anything across instances, and keeping a shared counter needs a database (for example Redis from the Vercel Marketplace). Hobby gets one WAF rate-limit rule per project; the window can be 10 seconds to 10 minutes, keyed by IP.

## Decision

Three layers, from the outside in:

1. **Access code.** Live mode needs a code, sent in a request header and compared on the server with a constant-time check against a `DEMO_ACCESS_CODE` Secret environment variable. Without it, `/api/live` returns 403 before any model call. the maintainer changes the code after the interview season, or removes the variable to turn live mode off.
2. **Vercel WAF rate limit.** One rule on the path `/api/live`, keyed by IP: for example 5 requests per 10 minutes, returning 429. It is set in the Vercel dashboard (Firewall, then a new rule with the action Rate Limit).
3. **Anthropic workspace spend limit.** The demo's own workspace has a monthly limit, for example $20. That is about 150 or more live runs at roughly 8 to 15 cents each. When it's reached, API calls fail, and live mode ends as a hand-off with the reason shown, while recorded mode keeps working.

Per request, the live path also:
- runs one case, with at most three model calls;
- keeps the repo's `max_tokens`;
- rejects a note over the length cap before any call.

## Options considered

1. **Three layers, no database (chosen).** Nothing extra to run. The spend limit is the hard cap, and it sits with the party that bills.
2. **A shared counter in Redis for a daily cap.** A precise daily cap and per-code quotas, but one more service, account and secret to look after.
3. **WAF only, no access code.** Per-IP limits don't stop someone rotating addresses, and Hobby has no spend controls.
4. **Vercel Pro for spend management.** That caps Vercel's bill, not Anthropic's.

## Consequences

- The worst case is the monthly spend limit, which the maintainer picks.
- WAF rate limiting on Hobby includes a set number of allowed requests a month (1,000,000 per Vercel's docs), far above a demo's traffic.
- Before the interview, the maintainer runs one live case to warm an instance and confirm the key and the limit.
