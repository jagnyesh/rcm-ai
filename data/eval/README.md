# Eval case set

20 synthetic denied CGM claims (LCD L33822) with gold labels, one JSON file per case in `cases/`. Each file uses the same case schema the CLI reads (`claim`, `note`, `evidence`), plus a `gold` block. All data is synthetic: no real patient, member, or claim data.

- **Drafted and labeled:** 2026-10-30 by Claude, on best judgment at the user's request, against the L33822 v70 text and the ICD-10-CM FY2027 tabular. Each case's `gold.label_status` is `claude-labeled`.
- **Human review:** pending. A reviewer who agrees with a label sets it to `confirmed`; one who disagrees corrects it under the locking rule below.
- **Count:** 20 (9 overturnable, 5 uphold, 6 needs_human; 2 specificity-mismatch and 2 Excludes1-conflict findings).

## Label rules

- **overturnable:** a covered diagnosis is billed and documented, and the note documents every initial criterion (1)–(5) for an in-scope item (E2102, E2103, initial coverage, CARC 50).
- **uphold:** the documentation shows that a coverage condition is **not** met (for example: not insulin-treated and no problematic hypoglycemia, a diagnosis that isn't covered, or no qualifying visit).
- **needs_human:** the claim is out of scope (supply allowance, continued coverage, a non-medical-necessity CARC), the documentation is missing or ambiguous, or the claim has a coding error to fix first (an Excludes1 conflict).
- `gold.evidence` (the case's `evidence` block) is what a perfect extractor would return. Every span is verbatim text from the note.
- `gold.path` (overturnable only) names the diagnosis and how each criterion is met.
- `gold.findings` records specificity mismatches (billed code covered but less specific than documented) and Excludes1 conflicts.
- `gold.known_system_gap` explains a case the current system gets wrong for a known, tracked reason. The gold label stays the policy-correct answer; it is not adjusted to match the system.

## Locking rule

This set is a locked golden fixture from its first commit, including while labels are `claude-labeled`. Never edit a case or label to match system output. Any change, including a corrected label, goes in its own commit whose message says which case changed and why. The eval harness (#8) asserts the case count, so adding or removing a case is a deliberate, visible change.
