# 0007. Synthetic data only: bundled cases, no free-form claims, nothing stored

- Status: Proposed
- Date: 2026-11-19
- Deciders: the maintainer

## Context

rcm-ai has no de-identification, access control or audit trail, and its logs keep each note's full text. The README says that is safe only because every note is synthetic. Every bundled case carries `"synthetic": true`, and `load_case` rejects one that doesn't. A public page with a text box invites someone to paste a real clinical note, which would be PHI sent to a third-party API under the maintainer's name.

## Decision

- **Bundled cases only.** Live mode runs on one of the bundled synthetic cases. The claim (HCPCS, ICD-10-CM codes, CARC, coverage) can't be edited.
- **The note can be edited, within limits.** Editing the note is what shows extraction and its checks working, for example by negating "on insulin". The edited note:
  - is capped in length, for example 1,500 characters (the bundled notes are a few sentences);
  - starts with the marker "SYNTHETIC NOTE." as every bundled note does;
  - sits under a plain warning on the page: synthetic text only, no real patient information.
- **Nothing is stored.** The function writes no logs of notes, extractions or letters. Vercel's runtime logs get the case id, the step, the duration and the call count, never the note's text.
- **What goes to Anthropic.** The note, the claim and the criteria, as in the eval. The page says this goes to the Anthropic API.

## Options considered

1. **Bundled cases with an editable note (chosen).** Shows extraction working on changed text while keeping the claim and the case synthetic.
2. **Bundled cases only, no editing.** Safest, but the live result then only repeats the recorded one with model noise.
3. **A free-form claim and note.** The most flexible, but it invites real data, and the graph only covers LCD L33822, so most real claims would just hand off.
4. **Run a PHI detector on input.** It adds a dependency and false confidence. A detector that misses something is worse than not inviting real notes at all.

## Consequences

- The marker and the length cap don't detect PHI. They set expectations and keep inputs small; the warning is the real control.
- This matches the repo's own scope: one policy, synthetic data, no production controls.
