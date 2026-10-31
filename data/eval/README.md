# Eval case set

20 synthetic denied CGM claims (LCD L33822) with gold labels, one JSON file per case in `cases/`. Each file uses the same case schema the CLI reads (`claim`, `note`, `evidence`), plus a `gold` block. All data is synthetic: no real patient, member, or claim data.

- **Drafted and labeled:** 2026-10-30 by Claude, on best judgment at the user's request, against the L33822 v70 text and the ICD-10-CM FY2027 tabular. Each case's `gold.label_status` is `claude-labeled`.
- **Human review:** pending. A reviewer who agrees with a label sets it to `confirmed`; one who disagrees corrects it under the locking rule below.
- **Count:** 20 (9 overturnable, 5 uphold, 6 needs_human; 2 specificity-mismatch and 2 Excludes1-conflict findings).

## Label rules

- **overturnable:** a covered diagnosis is billed and documented, and the note documents every initial criterion (1)–(5) for an in-scope item (E2102, E2103, initial coverage, CARC 50).
- **uphold:** the documentation shows that a coverage condition is **not** met (for example: not insulin-treated and no problematic hypoglycemia, a diagnosis that isn't covered, or no qualifying visit).
- **needs_human:** the claim is out of scope (supply allowance, continued coverage, a non-medical-necessity CARC), the documentation is missing or ambiguous, or the claim has a coding error to fix first (an Excludes1 conflict).
- The case's `evidence` block is what a perfect extractor would return. Every span is verbatim text from the note. `criteria_not_met` holds spans showing a criterion affirmatively fails; a criterion the note doesn't mention appears in neither list.
- `gold.path` (overturnable only) names the diagnosis and how each criterion is met.
- `gold.findings` records specificity mismatches (billed code covered but less specific than documented) and Excludes1 conflicts.
- `gold.known_system_gap` explains a case the current system gets wrong for a known, tracked reason. The gold label stays the policy-correct answer; it is not adjusted to match the system.

## Metrics

- **accuracy:** the arm's verdict equals the gold verdict.
- **false overturnable:** the arm says overturnable and gold doesn't. This is the costly error: an appeal sent on a claim that shouldn't be appealed.
- **false uphold:** the arm says uphold and gold doesn't. The other costly error: a claim written off that should have been appealed or reviewed.
- **abstention rate:** the share of cases the arm returns as needs_human.
- **abstention precision:** of those hand-offs, the share where gold is also needs_human. A hand-off on a gold-uphold case counts as an unnecessary hand-off: safe, but it costs reviewer time.
- **no output:** a model response that is refused, truncated or not JSON, or, for the LLM-only baseline, not a verdict. The arm hands the claim off (needs_human), so it scores like an abstention. The run record counts these per arm by stop reason, and eval warns about them on stderr.
- **disagreements:** with more than one arm, each case where the arms' outcomes differ, with gold and each arm's outcome and reasons.
- **errors:** cases where the arm raised. They count as wrong, never as abstentions, and a run with any errors exits 3, because its metrics aren't comparable to a clean run.

## Locking rule

This set is a locked golden fixture from its first commit, including while labels are `claude-labeled`. Never edit a case or label to match system output. Any change, including a corrected label, goes in its own commit whose message says which case changed and why. The eval harness asserts the case count against `set.json` (`expected_cases`), so adding or removing a case means updating that number in the same commit.
