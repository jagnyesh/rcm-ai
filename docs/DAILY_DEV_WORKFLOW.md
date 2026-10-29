# Daily Dev Workflow — 3-Day Prototype Edition

**Scope:** an exploratory revenue-cycle-management (RCM) + AI prototype, built solo, single lane, over ~3 days.
**Design rule:** keep only the practices that catch real bugs; drop anything that exists to coordinate parallel work or to govern merges at scale.
**Stamped:** 2026-10-29. Point-in-time; edit this file in place if the workflow changes.

---

## 1. Two tiers, decided by what a change touches

| Tier | What's in it | What it costs you |
|---|---|---|
| **Careful** | Money math (charges, allowed/paid amounts, adjustments, balances); payer/denial rule logic (CARC/RARC mapping, edits, eligibility); anything that reads claim/patient-shaped data; every LLM prompt or model call whose output is a code, a denial reason, or text a human would send to a payer; golden/eval fixtures | Tests first, then locked (§3). 5-line intent note in the commit body (what / why / constraint / non-goal). Fresh-context review before merge (§4). Wire-level check if it calls a model (§5). |
| **Fast** | UI, plumbing, scripts, docs, refactors with no behavior change in Careful paths | Just build it. Tests where cheap. |

When in doubt, it's Careful. Classify before writing code; it's the only planning step most changes need.

---

## 2. The loop (per slice)

```
branch          → feat/<slug> or fix/<slug>  (branch BEFORE editing; never on main)
classify (§1)   → careful or fast
/tdd            → careful: failing tests committed first, then locked
build           → smallest vertical slice that runs end to end
stuck > 20 min  → /diagnose — reproduce and dump real values before theorizing
review (§4)     → careful only: fresh context, one verdict
merge           → squash into main locally; push for backup
write-back      → if you had to correct Claude, add one dated rule line to CLAUDE.md
```

No PRs required, no CI required for the prototype. A GitHub remote is optional; push only so the work is backed up and visible from other devices.

---

## 3. Test integrity

- Careful-tier tests are committed **before** the implementation and not edited while implementing it. If a test is wrong, fix it in its own commit with a one-line reason.
- Golden files (expected outputs, eval cases) are never regenerated to match new output. "Update the expected output so it passes" is how silent bugs ship.
- An eval or test run that asserts nothing, or runs zero cases, is worse than none. Every eval prints and asserts its case count.

---

## 4. Fresh-context review (the one rule that survives at any scale)

The session that wrote the code doesn't review it; it reads its own intent into the diff. For Careful changes, review in a new session or with `/code-review`. You get one consolidated verdict, not a stream of comments. Behavior-changing findings come to you. Lint, naming, and dead code the session fixes itself.

End of each day: one fresh-context review over the day's whole diff (`git diff main@{1.day.ago}`), even for Fast changes.

---

## 5. LLM-touching code

- **Assert at the wire, not the wrapper.** For every model call, confirm once against the actual request and response (log the payload, check the parameters you think you're sending). Wrapper-level tests have hidden silent transmission bugs for months.
- **Tiny eval set from day 1:** 10–20 synthetic cases per AI feature, kept as a locked fixture (§3), runnable with one command, and printing pass count over case count. Not a gate, but it's what tells you a prompt change made things worse.
- Prompts live in files, not inline strings, so a diff shows when one changed.

---

## 6. Data and environment

- **Synthetic data only.** No real patient, member, or claim data in prompts, fixtures, logs, commits, or screenshots. Synthea exports claim/EOB-shaped data; hand-built X12/CSV samples are fine too.
- Per-project `.venv` via `uv`. Never sync or install into a shared environment.
- Secrets in a gitignored `.env`; `.env.example` holds key names only.
- Anything that mutates the filesystem or environment, including our own scripts, gets tried on a throwaway target first.
- No new skill, plugin, MCP server, or package mid-task because an agent suggested it. Finish, then vet it cold.

---

## 7. Three-day shape

| Day | Goal | Exit check |
|---|---|---|
| **0 (≈2h)** | Pick ONE RCM problem (e.g. denial triage, claim scrubbing, appeal drafting). Use `/grill-me` or `/office-hours` to pin the user, input, output, and what "good" looks like. Pick the synthetic data source. | One-paragraph problem statement in README + 10 eval cases written by hand |
| **1** | Thinnest end-to-end slice: load synthetic data → one AI step → visible output (CLI or a single page). | Runs from a clean clone with one command; eval prints N/N cases ran |
| **2** | Make the Careful path trustworthy: money/rule logic tested, prompt iterated against the eval set, wire-level check done. | Eval score recorded in the commit message; no hand-waved number in README |
| **3** | Demo surface + honest write-up: what works, what's faked, what you'd need for real data. Final fresh-context review. | Someone else could run it and know its limits |

Scope cuts happen at the day boundary, not mid-slice. If Day 1 isn't end-to-end by evening, Day 2 finishes it and the feature list shrinks.

---

## 8. Working from the phone

Phone sessions are for **steering and reviewing**: answer questions, approve Fast-tier work, read a review verdict, queue the next slice. Careful-tier implementation and merges into `main` happen at the desk, where you can read the full diff.

---

## 9. Deliberately dropped (from the larger multi-agent workflow)

| Dropped | Why it's not worth it here |
|---|---|
| Parallel lanes, worktrees, lane/WIP scripts | One person, three days; the bottleneck is review attention, which a second lane would split |
| PR-per-issue, CI gate, SHA-bound attestation, merge-actor rules | They govern merges at scale. Solo local squash-merge plus a fresh-context review gets you the safety at near-zero cost |
| Full grill → design doc → ADR chain | Replaced by the 5-line intent note in the commit; the README holds the one problem statement |
| Issue tracker, triage labels, sprint start/end ceremonies | The 3-day table (§7) is the plan |
| Catch-rate / cycle-time dashboards | Too little data in 3 days. If the prototype graduates, start logging what review catches from that day forward |

**Kept, because each of these caught real bugs before:** branch before editing, tests-first + locked goldens, fresh-context review, wire-level confirmation at boundaries, eval-must-assert-it-ran, synthetic-only data, sandbox-first, and the dated CLAUDE.md write-back.
