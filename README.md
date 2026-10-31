# rcm-ai

Exploratory prototype: graph-grounded triage of Medicare medical-necessity denials. Synthetic data only.

## Problem

A denial analyst working a medical-necessity denial (CARC 50, CARC 11) must decide whether the documented diagnosis supports the billed item under the governing coverage policy, which means cross-referencing the note, the claim's ICD-10-CM and HCPCS codes, the ICD-10-CM hierarchy and Excludes1 rules, and the LCD's billing article. An LLM can draft an appeal, but it decides coverage from memory and cites policy it can't trace. This prototype lets a small terminology-and-policy graph make the coverage decision and return a justification path where every hop points at a row in a public source file. The LLM only extracts evidence from the note and drafts the letter. A hand-labeled eval compares that against an LLM-only baseline, so the value of the knowledge layer is measured, not asserted.

## Scope

- **Policy:** Glucose Monitors, DME MAC LCD L33822 (v70, revision effective 2024-10-01) and its policy article A52464 ("Glucose Monitor - Policy Article", v65, effective 2025-02-18). Both IDs are confirmed in the CMS exports. Picked because the items are HCPCS Level II (31 codes, A4233–A9999 and E0607–E2104, none CPT-shaped), and diabetes (E08–E13) exercises both hierarchy closure and real Excludes1 pairs (E10 vs E11).
- **Codes:** ICD-10-CM and HCPCS Level II only. No CPT codes or descriptors are committed (AMA-licensed); `rcm_ai.codes.is_cpt` guards fixtures.

## Sources

| Source | Version | Where | License | Status |
|---|---|---|---|---|
| ICD-10-CM tabular XML | FY2027, effective 2026-10-01 | `icd10cm-tabular_-2027.xml` inside `icd10cm-table-and-index-2027.zip`, [CDC NCHS FTP](https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/2027/) | Public domain | Retrieved 2026-10-29; E08–E13 and O24 slices committed |
| Medicare Coverage Database, Current Article Data | Export of 2026-10-26 | `article_x_icd10_covered.csv` inside `current_article.zip`, [CMS MCD downloads](https://www.cms.gov/medicare-coverage-database/downloads/downloads.aspx) | Download requires accepting AMA/ADA/AHA license agreements; the committed slice holds ICD-10-CM only | Retrieved 2026-10-29; A52464 v65 slice committed (461 codes) |
| Medicare Coverage Database, Current LCD Data | Export of 2026-10-26 | `lcd_x_hcpc_code.csv` inside `current_lcd.zip`, same page | Same license gate; the committed slice holds HCPCS Level II only | Retrieved 2026-10-29; L33822 v70 slice committed (31 codes) |

Full downloads live in `data/raw/` (gitignored). Committed fixture slices live in `data/fixtures/`, each pinned by sha256 and source location in `data/fixtures/MANIFEST.json`; `verify_fixtures` fails on an edited or unlisted fixture. Each manifest entry names its source `table`, and the graph loads every fixture by table and every criteria file in `data/curated/`, so another LCD is added with data files alone. `uv run rcm-ai ingest` prints parsed, rejected, loaded and in-graph counts per source. A row that fails validation is rejected with its line and reason (triage and eval warn when any are), a duplicate key fails ingest, and so does a curated quote that isn't verbatim in its LCD text. Ingest also refuses inputs the graph can't represent faithfully: two versions of one LCD or article (graph nodes carry no version), an article whose covered codes span more than one group (groups can require a code pair), a criterion id reused across criteria files, and a code defined twice. The E08–E13 slice has no chapter 4 wrapper, so chapter 4's Excludes1 note (P70–P74) isn't loaded; the E10 and E11 category notes already exclude P70.2.

**Source format notes (FY2027 tabular XML):** Excludes1 targets are free text with a trailing code reference in one of three forms: `(E10.-)` subtree, `(P70.2)` exact, `(E11.0-E11.8)` range (range ends may carry a wildcard, as in `(I63.3-I63.5-)`). A note may also list several references, as in `(J09.X3, J10.2, J11.2)`; each becomes its own rule. Section- and chapter-level notes apply to every code below them, with the section (`E08-E13`) or chapter (`chapter-15`) as their owner. All 43 Excludes1 notes in E08–E13 sit under a `diag` and parse; the O24 slice carries chapter 15's note, `supervision of normal pregnancy (Z34.-)`. Across the full file there are 5,411 notes: 5,259 under a `diag`, 150 under a `section`, 2 under a `chapter`. Of these, 205 (194 `diag`, 11 `section`) still don't parse: lead-in notes with no code and one malformed source reference, `F98.2.-F98.3`. `parse_tabular` raises on them rather than dropping them; neither committed slice has one.

**Cross-source findings that shape the graph:**

- A52464 lists 461 covered codes, all leaf codes: 431 in E08–E13 and 30 in O24 (pre-existing diabetes in pregnancy; gestational O24.4 is not covered).
- 260 of the 431 E08–E13 covered codes (60%) are 7-character codes such as E08.3211, built from the tabular's `sevenChrDef` (eye-complication laterality) rather than listed as `diag` elements. The parser doesn't expand these yet, so they aren't concepts in the graph.
- For DME, the HCPCS codes sit in the LCD (`lcd_x_hcpc_code`) and the ICD-10 codes in the article (`article_x_icd10_covered`). Joining them takes the LCD-to-article link, which isn't modeled yet. The LCD's group 1 also says a listed code "does not necessarily indicate coverage".
- The article's only covered group says a listed ICD-10 code "is not sufficient by itself to assure coverage" and points to the LCD's medical-necessity criteria. A diagnosis match alone can't yield an overturnable verdict. Those criteria (insulin treatment, problematic hypoglycemia) live as HTML in `lcd.csv`'s `indication` column (about 15k characters for L33822), which isn't committed yet.

## Usage

From the repo root:

```bash
uv run rcm-ai triage data/synthetic/case-001.json
```

It prints the verdict: `overturnable` (with each step of the justification path: the fixture file and line, the source version, and the text to look for there), `uphold` (with the coverage condition the note shows failing, or an uncovered diagnosis), or `needs_human` (with the reason: out of scope, coding conflict, undocumented or conflicting evidence, or a gap in the loaded data). Uphold always rests on something the note says; a criterion the note doesn't mention is a hand-off, not an uphold. Cases must be marked `"synthetic": true`. A malformed or unreadable case is rejected on stderr with exit code 2. `--data` points at a different data directory (default `data`, relative to the current directory).

To score the system against the locked eval set (`data/eval`):

```bash
uv run rcm-ai eval
```

It asserts the case count from `data/eval/set.json` and fails with exit code 2 on an empty or partial set. It prints per-arm accuracy, false-overturnable cases, abstention rate and abstention precision, and lists each miss as a known gap or UNEXPLAINED. Per-case results, with the date, git SHA and model id, go to `eval-results/` (gitignored).

LCD coverage criteria live in `data/curated/L33822-cgm-criteria.json`, hand-curated with verbatim quotes from the LCD text. In scope: initial coverage of the CGM device (E2102, E2103) for medical-necessity denials (CARC 50). Supply allowances, continued coverage, and other denial reasons return `needs_human`. Evidence spans must appear verbatim in the case note, and evidence naming an unknown criterion is rejected.

For criteria hops, the line number is where the LCD record starts (the record spans about 130 lines). The `look for` text is the HTML-decoded indication text, so search the decoded text: the raw CSV stores `<` as `&lt;`.

## Development

```bash
uv sync
uv run pytest -xvs
```

Workflow: `docs/DAILY_DEV_WORKFLOW.md`.
