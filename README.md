# rcm-ai

Exploratory prototype: graph-grounded triage of Medicare medical-necessity denials. Synthetic data only.

## Problem

A denial analyst working a medical-necessity denial (CARC 50) must decide whether the documented diagnosis supports the billed item under the governing coverage policy, which means cross-referencing the note, the claim's ICD-10-CM and HCPCS codes, the ICD-10-CM hierarchy and Excludes1 rules, and the LCD's billing article. An LLM can draft an appeal, but it decides coverage from memory and cites policy it can't trace. This prototype lets a small terminology-and-policy graph make the coverage decision and return a justification path where every hop points at a row in a public source file. The LLM extracts evidence from the note and drafts the appeal, and a deterministic checker fails any letter that cites a code or policy ID the justification path doesn't establish. An eval set of 20 labeled synthetic cases scores the graph on supplied evidence and on LLM-extracted evidence against an LLM-only baseline, on verdicts and on the citations in their appeals, so the value of the knowledge layer is measured, not asserted. The labels are Claude's best judgment and await human review (#15).

## Scope

- **Policy:** Glucose Monitors, DME MAC LCD L33822 (v70, revision effective 2024-10-01) and its policy article A52464 ("Glucose Monitor - Policy Article", v65, effective 2025-02-18). Both IDs are confirmed in the CMS exports. Picked because the items are HCPCS Level II (31 codes, A4233–A9999 and E0607–E2104, none CPT-shaped), and diabetes (E08–E13) exercises both hierarchy closure and real Excludes1 pairs (E10 vs E11).
- **Codes:** ICD-10-CM and HCPCS Level II only. No CPT codes or descriptors are committed (AMA-licensed); `rcm_ai.codes.is_cpt` guards fixtures.

## Sources

| Source | Version | Where | License | Status |
|---|---|---|---|---|
| ICD-10-CM tabular XML | FY2027, effective 2026-10-01 | `icd10cm-tabular_-2027.xml` inside `icd10cm-table-and-index-2027.zip`, [CDC NCHS FTP](https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/2027/) | Public domain | Retrieved 2026-10-29; E08–E13, E15–E16, O24 and R73 slices committed |
| Medicare Coverage Database, Current Article Data | Export of 2026-10-26 | `article_x_icd10_covered.csv` inside `current_article.zip`, [CMS MCD downloads](https://www.cms.gov/medicare-coverage-database/downloads/downloads.aspx) | Download requires accepting AMA/ADA/AHA license agreements; the committed slice holds ICD-10-CM only | Retrieved 2026-10-29; A52464 v65 slice committed (461 codes) |
| Medicare Coverage Database, Current LCD Data | Export of 2026-10-26 | `lcd_x_hcpc_code.csv` inside `current_lcd.zip`, same page | Same license gate; the committed slices hold HCPCS Level II codes and the LCD's own text, no CPT | Retrieved 2026-10-29; L33822 v70 slices committed: `lcd_x_hcpc_code` (31 codes), the `lcd.csv` record, `lcd_related_documents` (3 links) |

Full downloads live in `data/raw/` (gitignored). Committed fixture slices live in `data/fixtures/`, each pinned by sha256 and source location in `data/fixtures/MANIFEST.json`; `verify_fixtures` fails on an edited or unlisted fixture. Each manifest entry names its source `table`, and the graph loads every fixture by table and every criteria file in `data/curated/`, so another LCD is added with data files alone. `uv run rcm-ai ingest` prints parsed, rejected, loaded and in-graph counts per source. A row that fails validation is rejected with its line and reason (triage and eval warn when any are; a source with every row rejected fails ingest, and while any covered-list row is rejected a "not covered" diagnosis hands off instead of upholding), a duplicate key fails ingest, and so does a curated quote that isn't verbatim in its LCD text. Ingest also refuses inputs the graph can't represent faithfully: two versions of one LCD or article (graph nodes carry no version), an article whose covered codes span more than one group (groups can require a code pair), a criterion id reused across criteria files, and a code defined twice. The E08–E13 slice has no chapter 4 wrapper, so chapter 4's Excludes1 note (P70–P74) isn't loaded; the E10 and E11 category notes already exclude P70.2.

**Source format notes (FY2027 tabular XML):** Excludes1 targets are free text with a trailing code reference in one of three forms: `(E10.-)` subtree, `(P70.2)` exact, `(E11.0-E11.8)` range (range ends may carry a wildcard, as in `(I63.3-I63.5-)`). A note may also list several references, as in `(J09.X3, J10.2, J11.2)`; each becomes its own rule. Section- and chapter-level notes apply to every code below them, with the section (`section-E08-E13`, prefixed because some sections share their id with their only category, as `E89` does) or chapter (`chapter-15`) as their owner. All 43 Excludes1 notes in E08–E13 sit under a `diag` and parse; the O24 slice carries chapter 15's note, `supervision of normal pregnancy (Z34.-)`. Across the full file there are 5,411 notes: 5,259 under a `diag`, 150 under a `section`, 2 under a `chapter`. Of these, 205 (194 `diag`, 11 `section`) still don't parse: lead-in notes with no code and one malformed source reference, `F98.2.-F98.3`. `parse_tabular` raises on them rather than dropping them; neither committed slice has one.

**Cross-source findings that shape the graph:**

- A52464 lists 461 covered codes, all leaf codes: 431 in E08–E13 and 30 in O24 (pre-existing diabetes in pregnancy; gestational O24.4 is not covered).
- 260 of the 431 E08–E13 covered codes (60%) are 7-character codes such as E08.3211, built from the tabular's `sevenChrDef` (eye-complication laterality) rather than listed as `diag` elements. The parser expands them into concepts, each sourced to the `sevenChrDef` extension line that defines its 7th character.
- For DME, the HCPCS codes sit in the LCD (`lcd_x_hcpc_code`) and the ICD-10 codes in the article (`article_x_icd10_covered`). Joining them takes the LCD-to-article link in `lcd_related_documents`. That link cites article v49 while the covered list loaded is v65; the justification path shows both. The LCD's group 1 also says a listed code "does not necessarily indicate coverage".
- The article's only covered group says a listed ICD-10 code "is not sufficient by itself to assure coverage" and points to the LCD's medical-necessity criteria. A diagnosis match alone can't yield an overturnable verdict. Those criteria (insulin treatment, problematic hypoglycemia) live as HTML in `lcd.csv`'s `indication` column (about 15k characters for L33822). That record is committed, and `data/curated/` holds the criteria as verbatim quotes from it.

## Usage

From the repo root:

```bash
uv run rcm-ai triage data/synthetic/case-001.json
```

It prints the verdict: `overturnable` (with each step of the justification path: the fixture file and line, the source version, and the text to look for there), `uphold` (with the coverage condition the note shows failing, or an uncovered diagnosis), or `needs_human` (with the reason: out of scope, coding conflict, undocumented or conflicting evidence, or a gap in the loaded data). Uphold always rests on something the note says; a criterion the note doesn't mention is a hand-off, not an uphold. Cases must be marked `"synthetic": true`. A malformed or unreadable case, or one with no `evidence` block run without `--extract`, is rejected on stderr with exit code 2. `--data` points at a different data directory (default `data`, relative to the current directory). `--draft` also drafts an appeal for an overturnable verdict and prints it with its citation check (see below).

To score the system against the locked eval set (`data/eval`):

```bash
uv run rcm-ai eval
```

It asserts the case count from `data/eval/set.json` and fails with exit code 2 on an empty or partial set. It prints per-arm accuracy, false-overturnable and false-uphold cases, abstention rate and abstention precision, and lists each miss, marking a graph or extracted miss as a known gap or UNEXPLAINED; any arm error makes the run exit 3. Per-case results, with the date, git SHA, model id and the reasons behind each verdict, go to `eval-results/` (gitignored). `--arms` picks the arms: `graph` (the default, using each case's supplied evidence), `extracted` and `llm-only` (see below). With more than one arm, eval also prints each case where the arms' outcomes differ, with the reasons of each arm that missed gold, and records every such case, with all arms' reasons, under `disagreements`. `--draft` adds appeal drafting and the unsupported-citation rate per arm (see below).

LCD coverage criteria live in `data/curated/L33822-cgm-criteria.json`, hand-curated with verbatim quotes from the LCD text. In scope: initial coverage of the CGM device (E2102, E2103) for medical-necessity denials (CARC 50). Supply allowances, continued coverage, and other denial reasons return `needs_human`. Evidence spans must appear verbatim in the case note, and evidence naming an unknown criterion is rejected.

### LLM evidence extraction

`uv run rcm-ai triage <case> --extract` and `uv run rcm-ai eval --arms graph,extracted` have `claude-opus-5-5` read the note and return diagnoses with ICD-10-CM codes, plus the criteria the note shows met or not met, each with a span copied from the note. The prompt and the JSON output schema are files in `src/rcm_ai/prompts/`. Some claims are decided by the claim alone and are handed off before any model call: a claim out of scope (denial reason, continued coverage, an item no modeled criteria apply to), or one whose billed codes conflict under Excludes1. Before anything is adjudicated, the extraction is checked:
- every span must appear verbatim in the note and be at least 10 characters;
- every code must exist in the loaded ICD-10-CM terminology. The rejection says whether a code is invalid or outside the loaded slices, where the graph can't check it;
- every criterion must be one offered for the claim's LCD;
- a criterion decided from the diagnosis code may not be listed as met;
- a criterion with alternatives may be marked not met only through each of its alternatives.

One failure rejects the whole extraction and the claim goes to `needs_human`, and so does a refusal or a truncated response. There is no fallback model. On extracted evidence, an uncovered billed diagnosis is upheld only when the note rules out the covered condition, because the model may have missed a covered diagnosis the note documents.

**Known limit:** validation proves each span is in the note, not that it supports what it's attached to. A negated phrase ("on insulin" inside "not on insulin"), one span reused for another criterion, or a code whose span describes a different condition all pass. Checking that a span entails its claim, with an NLI model or a second model pass, is not built.

These commands call the Anthropic API and need credentials (for example `ANTHROPIC_API_KEY`). A one-call check fails fast with exit code 2 without them. The API rejects `temperature`, so runs are not deterministic. Each run record reads the model, effort and the prompt and schema hashes from the request bodies actually sent. It counts calls made and responses returned, and per case records whether the model was called, the model that answered and the request id. Each extraction log keeps the exact request bytes and is written as soon as the call returns.

First runs, 2026-10-31, 20 cases (labels await review in #15):
- **Graph arm:** 20/20.
- **Extracted arm at `bb8cb0f`:** 16/20, 0 false overturnable, 0 false uphold, 17 model calls. The run record is marked dirty only by the untracked `docs/diagrams/`. All four misses are hand-offs:
  - three for a real secondary code outside the loaded ICD-10-CM slices (Z90.411, N18.31, Z98.84, each in the full FY2027 tabular);
  - one, ev-11, where the model didn't take "on a sulfonylurea" to rule out insulin treatment, which the gold label does.
- **Same outputs replayed at `decc246`:** the same 16/20, with 15 calls. ev-19 and ev-20 bill conflicting codes and now hand off without a model call. The prompt is unchanged since `bb8cb0f`.
- **Earlier run** with the previous prompt and validation: 15/20. The one-case difference is run-to-run variance: ev-04 failed there only because the model added Z79.84, which it didn't do this time.

### LLM-only baseline

`uv run rcm-ai eval --arms graph,extracted,llm-only` adds the comparison arm. `claude-opus-5-5` reads the claim, the note and the curated criteria of the LCD that lists the billed item (the text the extractor gets), and returns the verdict itself. It gets nothing the graph decides: not the covered-diagnosis list, the linked article, the ICD-10-CM terminology or Excludes1 rules. Its prompt, `src/rcm_ai/prompts/llm_only_verdict.md`, defines the verdicts with the three label rules from `data/eval/README.md` word for word, scope included. Model, effort and `max_tokens` are the extractor's, and its output schema is the extractor's plus `verdict` and `reasons`. Every case gets a model call.
- Its verdict is taken as given. Its reasons become the verdict's reasons, or, for overturnable, path hops with no source: the model's assertion, not a row in a source file.
- Its evidence fields are logged but not checked or scored. Comparing how well each arm's claims are grounded is #11.
- A refusal, a truncated response, or output that isn't a verdict with reasons hands the claim off (`no_verdict`), as a rejected extraction does in the extracted arm. Each model-calling arm's record counts stop reasons and the responses it couldn't use, and eval warns about the latter.
- It keeps its own run record, and its logs go to `eval-<stamp>-llm-only/`.

Runs on 2026-10-31, each marked dirty only by the untracked `docs/diagrams/`:

| Arm | Run | Accuracy | False overturnable | False uphold | Hand-offs (correct) |
|---|---|---|---|---|---|
| graph | both | 20/20 | 0 | 0 | 6 (6) |
| extracted | `4bc67f4` | 16/20 | 0 | 0 | 10 (6) |
| llm-only, first prompt | `4bc67f4` | 19/20 | 0 | 0 | 7 (6) |
| llm-only, label rules verbatim | `1b47e29` | 19/20 | 0 | 1 | 5 (5) |

- **First prompt:** its verdict definitions paraphrased the label rules. The review found that the paraphrase added scope wording and widened "coding error" beyond Excludes1. Its one miss, ev-09, was a hand-off: it wanted the billed E11.9 recoded to E11.22 before an appeal, while gold says overturnable with a specificity finding. The extracted arm missed ev-09 (N18.31 outside the loaded slices), ev-11, ev-12 (`covered_diagnosis_not_ruled_out`) and ev-13 (Z98.84), all as hand-offs. Compared with its earlier run, ev-05 passed and ev-12 missed, at the same prompt.
- **Label rules verbatim:** ev-09 is now correct, but ev-18 is a false uphold, the costly error. The note says insulin was discussed and doesn't mention hypoglycemia. The model took that silence as criterion 4 failing, where gold says to request records. The graph decides ev-18 by a rule in its code: a criterion the note doesn't mention is undocumented, and the claim goes to a human. The verbatim rules state that only loosely ("the documentation is missing" means needs_human), and their uphold example, "not insulin-treated and no problematic hypoglycemia", also reads onto ev-18. The first prompt stated the graph's rule outright, so this prompt lost a rule the graph keeps. Settling the rules' wording belongs with the label review (#15); the prompt wasn't run a third time.
- **Reading these numbers:** the baseline matches or beats the extracted arm on accuracy in both runs. That is the outcome the PRD says would count against the premise. Weigh it against four things:
  - the cases and gold labels were drafted by the same model family the baseline runs on;
  - the notes are short and test one issue each;
  - each arm ran once on 20 cases;
  - the eval scores the verdict, not its grounding: the baseline's overturnable justifications cite nothing a reviewer can check.

### Appeal drafting and citation checking

`uv run rcm-ai triage <case> --draft` drafts an appeal for an overturnable verdict, prints it, and checks its citations against the verdict's path. `uv run rcm-ai eval --arms graph,extracted,llm-only --draft` does the same for every overturnable verdict of every arm. Drafting calls the model, so it's opt-in; without `--draft`, `triage` and `eval` make no drafting call.
- **Drafter:** `claude-opus-5-5` with the extractor's settings and `src/rcm_ai/prompts/draft_appeal.md`. It gets the claim, the path's statements and the note's quotes (diagnoses unlabeled by code, then each criterion met), and returns one `letter` field. Every arm uses the same drafter with its own verdict and evidence: the graph arm the case's evidence, the extracted arm its validated extraction, the baseline its own reasons and unchecked evidence fields. The steps carry no source file names, so every arm's input reads alike.
- **Checker:** deterministic. It reads every ICD-10-CM code (with or without its dot), HCPCS code (with or without modifiers), LCD and article ID (as `L33822` or "LCD 33822"), NCD ID, and CPT code labeled "CPT" in the letter, and fails the letter on any its reference path doesn't establish. A path establishes what its hop statements name, not their source anchors or asides.
  - In `triage`, the reference is the verdict's own path.
  - In `eval`, every arm's letter is checked against the graph's path on the case's gold evidence. When the graph wouldn't overturn the claim, that path is empty and every citation is unsupported.
  - **Limits:** a bare five-digit number isn't read as a CPT code, so an unlabeled CPT code passes; lowercase codes aren't read; and code-shaped words like "vitamin B12" or the remark code M25 are read as codes and fail the letter.
- **Metrics per arm:** letters drafted, letters that fail the check, drafts without a letter, drafting errors, and the unsupported-citation rate (unsupported citations over all citations, each code counted once per letter). A failed draft doesn't change the verdict's score; a drafting error makes the run exit 3.

Run on 2026-10-31 at `9328cbc` (marked dirty only by the untracked `docs/diagrams/`), all three arms, 60 model calls:

| Arm | Accuracy | Letters | Letters failing the check | Unsupported citations |
|---|---|---|---|---|
| graph | 20/20 | 9 | 0 | 0/36 |
| extracted | 15/20 | 7 | 0 | 0/28 |
| llm-only | 19/20 (false uphold: ev-18) | 9 | 4 | 7/26 (27%) |

- Every unsupported citation is a real ICD-10-CM code taken from the baseline's own reasons. None is fabricated, but the graph's path establishes none of them:
  - Z79.4 on ev-01, ev-05 and ev-08;
  - Z90.411 on ev-05;
  - E11.65 on ev-08 and E11.22 with N18.31 on ev-09. These are the more specific diagnoses the notes document, not the billed E11.9, so those letters argue a diagnosis that isn't on the claim.
- The grounded letters cite the LCD and the article every time. Only one of the baseline's nine cites any policy ID.
- The extracted arm missed ev-06 this run (it coded Z79.4, outside the loaded slices) and passed ev-05: run-to-run variance at the same prompt.
- After this run, the checker learned more written forms, and the drafter's steps lost their source file names. Re-scoring the saved letters with the final checker gives the same counts. The letters themselves were drafted from steps that still named source files.
- Like the verdict results, these come from one run on 20 cases drafted and labeled by the same model family the baseline runs on.

For criteria hops, the line number is where the LCD record starts (the record spans about 130 lines). The `look for` text is the HTML-decoded indication text, so search the decoded text: the raw CSV stores `<` as `&lt;`.

## Development

```bash
uv sync
uv run pytest -xvs
```

Workflow: `docs/DAILY_DEV_WORKFLOW.md`.
