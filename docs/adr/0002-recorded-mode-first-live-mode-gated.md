# 0002. Recorded mode by default; live mode behind an access code

- Status: Proposed
- Date: 2026-11-19
- Deciders: the maintainer

## Context

The demo's audience is the hiring manager for R1 RCM's R37 clinical informaticist role. That team's work centers on whether data is ingested, complete, fresh and correctly represented. So the demo should show two things:
- the graph's decision and the source row behind every step;
- what the model is trusted with (reading the note, drafting the letter) and what it isn't (deciding coverage).

A live model call is slower (tens of seconds per call at effort high), costs money on the maintainer's key, and can fail on the day. The README's own results also come from recorded runs, not from a live call.

## Decision

The demo has two modes.

**Recorded mode (the default, no model calls, open to anyone).** The visitor picks one of the 20 eval cases or `case-001`. The page shows:
- the claim and the note;
- the evidence;
- the graph's verdict and each step of its justification path, with the fixture file, line, version and the text to look for;
- for a hand-off, the reason it goes to a human;
- for the three demo cases, the appeal letter recorded on 2026-11-01 and its citation check;
- each arm's verdict on that case side by side, from the 2026-11-01 run (`data/demo/run-2026-11-01.json`). ev-18 is where llm-only made its false uphold, and ev-09, ev-11, ev-12 and ev-13 are where extraction sent the case to a human.

A "Data sources" panel shows what the graph loaded: each fixture's source, version, export date and sha256 pin, and the parsed, rejected and loaded counts from `rcm-ai ingest`.

**Live mode (behind an access code the maintainer gives out).** For a bundled case, optionally with the note edited, the page runs the real pipeline:
- the model extracts evidence;
- the support check runs;
- the graph decides;
- for an overturnable verdict, the model drafts the appeal and the checker scores it.

Each step appears as it finishes.

## Options considered

1. **Recorded by default, live behind a code (chosen).** Recorded mode is instant, free and can't fail on the day. Live mode proves the system is real when someone asks to see it.
2. **Live only.** Most convincing, but every visit costs money, a slow or failed call is what the visitor remembers, and anyone could run up the bill.
3. **Recorded only.** Free and safe, but nobody can see the model read a note.
4. **Live mode open to anyone, with rate limits only.** No code to hand out, but per-IP limits don't stop a determined visitor, and Hobby has no spend controls.

## Consequences

- The page works in an interview even if the Anthropic API is slow or down.
- the maintainer shares the access code with the hiring manager directly, by email or in the meeting.
- Live results are labeled as a single run: model output isn't deterministic, and the README's results are agreement with unreviewed labels (#15). The page carries that warning next to every accuracy number.
