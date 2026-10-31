# rcm-ai

Exploratory prototype: graph-grounded triage of Medicare medical-necessity denials. Synthetic data only.

## Problem

A denial analyst working a medical-necessity denial (CARC 50) must decide whether the documented diagnosis supports the billed item under the governing coverage policy, which means cross-referencing the note, the claim's ICD-10-CM and HCPCS codes, the ICD-10-CM hierarchy and Excludes1 rules, and the LCD's billing article. An LLM can draft an appeal, but it decides coverage from memory and cites policy it can't trace. This prototype lets a small terminology-and-policy graph make the coverage decision and return a justification path where every hop points at a row in a public source file. The LLM only extracts evidence from the note. An eval set of 20 labeled synthetic cases scores the graph on supplied evidence and on LLM-extracted evidence, so the value of the knowledge layer is measured, not asserted. Not built yet: the LLM-only baseline arm (#10) and appeal-letter drafting (#11). The labels are Claude's best judgment and await human review (#15).

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

It prints the verdict: `overturnable` (with each step of the justification path: the fixture file and line, the source version, and the text to look for there), `uphold` (with the coverage condition the note shows failing, or an uncovered diagnosis), or `needs_human` (with the reason: out of scope, coding conflict, undocumented or conflicting evidence, or a gap in the loaded data). Uphold always rests on something the note says; a criterion the note doesn't mention is a hand-off, not an uphold. Cases must be marked `"synthetic": true`. A malformed or unreadable case, or one with no `evidence` block run without `--extract`, is rejected on stderr with exit code 2. `--data` points at a different data directory (default `data`, relative to the current directory).

To score the system against the locked eval set (`data/eval`):

```bash
uv run rcm-ai eval
```

It asserts the case count from `data/eval/set.json` and fails with exit code 2 on an empty or partial set. It prints per-arm accuracy, false-overturnable and false-uphold cases, abstention rate and abstention precision, and lists each miss as a known gap or UNEXPLAINED; any arm error makes the run exit 3. Per-case results, with the date, git SHA and model id, go to `eval-results/` (gitignored). `--arms` picks the arms: `graph` (the default, using each case's supplied evidence) and `extracted` (see below).

LCD coverage criteria live in `data/curated/L33822-cgm-criteria.json`, hand-curated with verbatim quotes from the LCD text. In scope: initial coverage of the CGM device (E2102, E2103) for medical-necessity denials (CARC 50). Supply allowances, continued coverage, and other denial reasons return `needs_human`. Evidence spans must appear verbatim in the case note, and evidence naming an unknown criterion is rejected.

### LLM evidence extraction

`uv run rcm-ai triage <case> --extract` and `uv run rcm-ai eval --arms graph,extracted` have `claude-opus-5-5` read the note and return diagnoses with ICD-10-CM codes, plus the criteria the note shows met or not met, each with a span copied from the note. The prompt and the JSON output schema are files in `src/rcm_ai/prompts/`. A claim that is out of scope from the claim alone (denial reason, continued coverage, an item no modeled criteria apply to) is handed off before any model call. Before anything is adjudicated, the extraction is checked:
- every span must appear verbatim in the note and be at least 10 characters;
- every code must exist in the loaded ICD-10-CM terminology. The rejection says whether a code is invalid or a real code outside the loaded slices;
- every criterion must be one offered for the claim's LCD;
- a criterion with alternatives may be marked not met only through each of its alternatives.

One failure rejects the whole extraction and the claim goes to `needs_human`, and so does a refusal or a truncated response. There is no fallback model. On extracted evidence, an uncovered billed diagnosis is upheld only when the note rules out the covered condition, because the model may have missed a covered diagnosis the note documents.

**Known limit:** validation proves each span is in the note, not that it supports what it's attached to. A negated phrase ("on insulin" inside "not on insulin"), one span reused for another criterion, or a code whose span describes a different condition all pass. Checking that a span entails its claim, with an NLI model or a second model pass, is not built.

These commands call the Anthropic API and need credentials (for example `ANTHROPIC_API_KEY`). A one-call check fails fast with exit code 2 without them. The API rejects `temperature`, so runs are not deterministic. Each run record reads the model, effort and the prompt and schema hashes from the request bodies actually sent, plus per case the model that answered and the request id. Each extraction log keeps the exact request bytes and is written as soon as the call returns.

First runs, 2026-10-31, 20 cases (labels await review in #15):
- **Graph arm:** 20/20.
- **Extracted arm at `bb8cb0f`:** 16/20, 0 false overturnable, 0 false uphold, 17 model calls. All four misses are hand-offs:
  - three for a correct secondary code outside the loaded ICD-10-CM slices (Z90.411, N18.31, Z98.84);
  - one, ev-11, where the model didn't take "on a sulfonylurea" to rule out insulin treatment, which the gold label does.
- **Earlier run** with the previous prompt and validation: 15/20. The one-case difference is run-to-run variance: ev-04 failed there only because the model added Z79.84, which it didn't do this time.

For criteria hops, the line number is where the LCD record starts (the record spans about 130 lines). The `look for` text is the HTML-decoded indication text, so search the decoded text: the raw CSV stores `<` as `&lt;`.

## Development

```bash
uv sync
uv run pytest -xvs
```

Workflow: `docs/DAILY_DEV_WORKFLOW.md`.
