# rcm-ai

Rules for any agent or person working in this repo. What the project is and how to run it: [README.md](README.md). Decisions: [docs/adr/](docs/adr/README.md).

## Workflow

These rules hold for every session that works in this repo: Claude Code on J's Mac, a Claude Duet thread in the cloud, or a subagent either one starts. They restate J's cross-project rules, which live in `~/.claude` on his Mac, where cloud sessions can't read them. The fuller method, with the reasons behind it, is [researchflow's DAILY_DEV_WORKFLOW.md](https://github.com/jagnyesh/researchflow/blob/main/docs/DAILY_DEV_WORKFLOW.md). Where this section and something J says in the conversation disagree, ask J.

### 1. Classify before you plan

A change's risk tier comes from the paths it touches, and the highest tier among them wins. The tier sets how much planning the change gets and which model runs it. This repo's paths for each tier are under "In this repo" below.

| Tier | Plan | Model, effort |
|---|---|---|
| Low | The issue text is the plan. | Sonnet, low for a one-file docs change, otherwise medium |
| Medium | A 5 to 10 line intent block in the PR body: what, why, constraints, non-goals. | Sonnet, medium |
| High | Grill, then a design note, then a decision record, before any code. | Opus, high |

Fable runs only when J names it, with one standing exception: on High-tier PRs the second, independent reviewer runs on Fable.

### 2. Grill before building

For High-tier work, and for any open question a Medium change rests on:

- Ask J one question at a time, and wait for the answer before asking the next.
- Start with the question whose answer changes the most of what follows.
- Record each decision in this repo's decision log before building on it: what was decided, why, and what it rules out.
- On High-tier questions, don't fill a gap with a "reasonable assumption". Ask.

### 3. TDD: red, green, refactor

- Write the test first and commit it before the implementation. Run it and watch it fail for the reason you expect.
- Write the least code that makes it pass, then refactor with the tests green.
- From that commit until the task ends, tests and expected outputs are locked. Never edit a test, golden file or eval label to match the code. A correction is its own commit that says why.
- At an interface boundary (an API, an SDK, a wrapper, a cache), green unit tests are not enough. Confirm the behavior with real values on the wire.

### 4. Branches and PRs

- One issue, one branch, one PR. Branch before editing: `feat/<issue>-<slug>` or `fix/<issue>-<slug>`.
- Conventional commit messages. Rebase onto the latest `main` before review.
- Open every PR as a draft, with a Testing evidence section: the tier and the paths behind it, the commands run with their real output, what was exercised by hand, and what isn't covered and why.

### 5. Review in a fresh context

- The session that wrote a change never reviews it. A fresh session or subagent reviews the diff and posts one consolidated verdict that names the commit SHA it reviewed.
- High tier also gets a second, independent reviewer on Fable.
- The author fixes mechanical findings (lint, naming, dead code) itself. Findings that would change behavior go to J.

### 6. Parallel sessions

- Each parallel session (a lane) gets its own branch and worktree, and a brief that carries the why, not a list of steps. A lane sees only its brief and this file.
- Two lanes that touch the same file run one after the other, never side by side. So do two lanes that change the same behavior from different files, such as a code change and a test that asserts the old behavior. `CLAUDE.md` and the decision log count as shared files.
- While `main` is red, the only lane that opens is the one that fixes it.

### 7. Merge, deploy and settings

- No session merges a PR, deploys, or changes repository, hosting or secret settings without J's yes, in his own words, for that PR or change. This repo has no standing self-merge rule.
- Anything that changes files or environments outside the repo proves itself in a throwaway target first. So does a new skill or tool: read all of it, pin a version, try it in a scratch repo, and never install one mid-task because an agent suggested it.
- Keys come from the environment. Never commit one, and never copy one from another repo.

### 8. Write corrections back

When J corrects a session, add one dated line to this file that states the rule, with the case as its precedent, so the next session doesn't need the same correction.

### 9. Claims, numbers and size

- Label every claim in a report, PR body or review verdict as measured (you ran it and saw the output), inferred (it follows from what you read) or a guess. An unlabeled claim reads as measured.
- Before you trust, report or act on a measured number (a speedup, token count, eval score or cost), name what limits it and rule out that it measured something other than the work you meant. Precedent: in researchflow, a cost aggregator double-charged because `input_tokens` already included `cache_read`.
- Subtract before you add. Remove dead code and unused paths first, make the smallest change that does the job, and prefer deleting to adding a new abstraction.

### In this repo

- **High:** the verdict and evidence path (`src/rcm_ai/adjudicate.py`, `graph.py`, `extract.py`, `provenance.py`, `appeal.py` and its citation check); prompts (`src/rcm_ai/prompts/**`); the eval set, its labels and the gate (`data/eval/**`, `src/rcm_ai/gate.py`); source fixtures (`data/fixtures/**`); live mode, its key, rate limits and cost cap (`src/rcm_ai/live.py`, `src/rcm_ai/web.py`, ADR 0006 and 0008); anything that touches the synthetic-data-only rule (ADR 0007).
- **Medium:** the web demo page (`public/**`), CLI plumbing, `evaluate.py`, `stats.py`, `demo_export.py`, CI workflows.
- **Low:** `docs/**`, `README.md`, tests that aren't eval cases or fixtures, refactors with no behavior change outside the High paths.
- **Decision log:** one numbered file per decision in `docs/adr/`.
- **Tests:** `uv run pytest -q`, which needs no key. CI also runs `uv run rcm-ai export-demo --check` and the graph-arm eval through `uv run rcm-ai gate`.
- **Model calls cost money.** Run a command that calls a model (the README's command table lists them) only when J asks for that run.
- **Live mode (J, 2026-11-20):** when J says turn it off, turn it off right away.
