# Verification record

This is an implementation test record, not a measured claim about accuracy across supplier invoices.

## Automated and local verification

The repository suite currently passes **148 tests with no skips**, including the real PostgreSQL contract. Ruff is clean. The full Chromium workflow passes, including desktop/mobile layout, stable source preview, explicit save-before-batch behavior, combined review-only Excel, the separate atomic approved export, reference candidates and managed Vertex selection. All three Docker reader warmups passed in 63 seconds and recovered both synthetic lines. Access hotfix revision `00005-zdk` is deployed; hosted login, 31 retained jobs and sign-out passed after the only uncommitted reference import was cancelled. The complete current feature bundle is not yet claimed live. The hosted lookup currently reports zero rows, so catalog availability and recovery remain pending.

- Backend tests cover exact identity and scope, SKU/GTIN conflicts, receipt balances, returns, repeated lines, duplicate invoices, currency rounding, review revisions, atomic batch export, malformed reference inputs and secret encryption.
- Preflight regressions prove invalid uploads do not consume confirmation, missing confirmation blocks processing, changed rules invalidate a plan, and a mid-extraction rule change cannot enrich or approve the result under unconfirmed rules.
- Mocked provider tests cover managed Vertex AI, OpenAI, Anthropic, local Claude and ChatGPT request/response contracts, structured output and failure paths. Vertex uses Application Default Credentials, a fixed Google endpoint, bounded inline document/text input, a strict response schema and safe errors.
- Cloud identity tests require a verified, allowlisted email and check token revocation. Anonymous access to invoices, references and downloads is rejected. PostgreSQL/GCS contract tests cover transaction locking, rollback, ciphertext credentials, raw export SQL and blob recovery; the real-database test runs when `INV_STUDIO_TEST_DATABASE_URL` is supplied.
- Real Chromium tests cover desktop/mobile layout, no processing before confirmation, cancel behavior, multi-file upload, review/export holds, visible unsaved edits, combined review-only download without job/ledger mutation, approved two-invoice batch download, exact 13/3/6 shapes, transaction joins and no provider-key browser storage.
- Printed line net and tax values are preserved separately from printed unit price. Regressions prove review saves retain them and neither extraction nor quality checks reprice a line to force arithmetic.
- Native-layout regressions require a strong invoice heading, reject purchase orders, extract explicit labeled facts and spatial table rows, preserve leading zeros, leave conflicts/ambiguous dates unset and never infer internal business codes, currency or header totals from line arithmetic.
- Extraction outcome regressions record `text_read`, `fields_extracted` or `extraction_failed`; the legacy `extracted` event is displayed only as `Reader run finished (legacy event)`.
- Manual-draft regressions must prove exact 13/3/6 sheets, leading-zero text, no formulas, explicit acknowledgement, `DRAFT_UNVALIDATED` disclosure, download during processing, and no change to job/revision/invoice/reference approval/export ledger/receipt allocation.
- Lookup regressions exercise the 114,940-item / 297,199-PO/GRN source profile (412,139 rows), identifier and product-name search, pagination/filtering, conflict display and explicit confirmation while proving that lookup records are not approved matching data. The hosted import is paused/cancelling and live availability is not confirmed.
- Connection regressions must cover Claude setup-token encryption/removal and cloud authentication, plus the ChatGPT move boundary: local export only, credential detachment after successful bundle construction, hosted import only, and no secret returned through state or errors.

## Commands

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check app tests scripts
npm ci
npm run build:auth
npm run test:browser
```

Use an isolated data directory for browser tests: they deliberately import synthetic references and create invoices/exports. The Docker build warms all local readers on synthetic documents and fails if the expected invoice lines are not recovered.

## Observed extraction checks

No invoice values are published. The figures below are aggregate private engineering evidence, with their denominators and limits kept visible.

| Path | Observed private result | Practical conclusion |
|---|---|---|
| Native PDF tables, AI off | 15/15 PDFs, 570/570 rows and 3,990/3,990 source-tested line fields exact; 24.6 s total on the first batch and 12.9 s total warm | Strong evidence for this one native layout family through the invoice2data/native adapter; not a supplier-wide result |
| PaddleOCR + layout, AI off | 6/6 lines; 60/60 adjudicated header, identifier and numeric facts exact across three scans | Descriptions remain unproven because raw OCR visibly fuses/spaces words. Counting all six descriptions as incorrect gives a conservative 60/66 = 90.91% floor |
| Docling, AI off | 5/5 lines in 34 s; 1/1 line on a one-page document; 17/17 lines on a two-page document in 92 s | Native-table conversion works on these bounded examples |
| Docling failure boundary | Ten-page document exceeded 528 s and was terminated; three scans took 103/155/170 s and returned 0 reliable lines with only 3/2/2 headers | The expected 95% and broad scan support are unresolved; Docling must remain a selectable, review-required path rather than a universal fallback |
| Managed Vertex | 15 documents, 570 lines, all source-tested fields exact after retry, from one layout family | Useful within-sample evidence only; it does not establish held-out supplier, scan, language or page-length accuracy |

The hosted candidate currently contains 14 of those evaluated documents with 377 lines plus four other real jobs. The 193-line ten-page result is not yet live. Do not combine local and hosted counts into a deployment claim.

The 15 successful managed Vertex calls used 36,450 input tokens and 91,892 output tokens including reasoning. At Google's current global introductory Gemini 3.7 Flash rate through 31 December 2026—USD 0.75 per million input tokens and USD 3.75 per million output tokens—the calculated usage is **USD 0.3719325**, or **USD 24.7955 per 1,000 documents** at the same token mix. This is not an actual bill and excludes retries, Cloud Run, Cloud SQL, storage and network charges. Pricing source: [Google Agent Platform generative AI pricing](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing).

## Frozen held-out 90% evaluation plan

The 90% figures below are proposed acceptance targets, not measured results.

1. Select a held-out set stratified by supplier, native text versus scan, language, page-count band and document quality. Freeze file hashes and the provider/model/revision before any tuning.
2. One reviewer transcribes every expected header and line cell; a second checks the transcription against the source; an adjudicator resolves differences. Represent expected missing values explicitly as `null`.
3. Run every invoice once under the controlled plan. Keep errors, timeouts, rejected schemas and empty output in the denominator; score their affected cells and zero-correction outcome as incorrect.
4. Report **header-field exact accuracy** over every gold header cell, including expected nulls. Apply only contract-defined normalization for dates, Decimals and whitespace.
5. Align output lines with gold lines by adjudicated source order/evidence. Report line recall and **line-cell exact accuracy** over item identifiers, description, quantity, unit, price and evidence page. Extra, missing and misaligned lines are errors.
6. Report **zero-correction invoice rate**: invoices requiring no extracted header or line edit divided by all attempted invoices.
7. Separately report schema validity, completeness, arithmetic reconciliation, validation holds, latency, tokens, cost and correction reason. None of those substitutes for accuracy.

Proposed gate: at least **90% header-field exact accuracy, 90% line-cell exact accuracy and 90% zero-correction invoices**, with zero false-ready approved invoices. Report confidence intervals and each agreed supplier/language/scan stratum; the business owner must set a minimum per stratum so a large easy cohort cannot hide a weak one. A controlled reviewer signs the comparison, and AP/data owners decide whether failures require a template/rule change or continued manual handling.

## Business and deployment acceptance still required

Approve supplier/site mappings, receipt-acceptance meaning, prior-invoicing baseline and other unresolved business fields before real invoices can be called ready. Complete the frozen evaluation above and verify the generated workbook in the actual receiving process.

## Hosted verification boundary

The Firebase/GCP endpoint was exercised with a temporary verified, allowlisted test identity:

- Anonymous protected API requests are rejected; authenticated session identity is checked by the server.
- Actual invoice2data PDF, PaddleOCR scan and Docling PDF jobs each recovered both synthetic invoice lines.
- Two separate reviewed invoices exported one workbook with exactly Header/Tax_Breakdown/Details, 13/3/6 columns, transaction IDs 1 and 2 on all sheets, and no formulas.
- After replacing the Cloud Run revision, all five jobs remained present. The workbook and every original document retained their exact SHA-256 hashes.
- Actual desktop/mobile browser checks cover Firebase password sign-in, authenticated source preview, Excel download and sign-out.

This proves the earlier hosted synthetic workflow, not accuracy or export eligibility for unapproved business extracts. Database backup restoration and representative held-out evaluation remain separate acceptance work. The narrow private measurements above must retain their supplier/layout denominators.

Access hotfix `00005-zdk` passed login, retained-job count and sign-out. The current feature bundle still needs deployment and authenticated extraction, combined review-only batch, approved export and persistence checks. No live ChatGPT or Claude subscription inference has been tested or certified here.

## Release owner fill-in

- Revision: `access hotfix 00005-zdk deployed; current feature bundle deployment/verification PENDING`
- Repository tests: `148 passed; 0 skipped`
- PostgreSQL: `included in the no-skip repository run`
- Ruff: `clean`
- Browser: `full Chromium workflow passed locally`
- Container/readers: `all three Docker warmups passed in 63 s with two synthetic lines each; final feature image publication PENDING`
- Native, AI off: `15/15 PDFs; 570/570 lines; 3,990/3,990 tested line fields exact for one layout family`
- Paddle, AI off: `6/6 lines; conservative 60/66 = 90.91% floor with all descriptions treated unproven`
- Docling, AI off: `bounded native examples work; ten-page timeout and three scan failures unresolved`
- Managed Vertex: `15 documents/570 lines, all source-tested fields exact for one layout family; calculated usage USD 0.3719325`
- Private catalog: `412,139-row source profile; hosted import paused/cancelling; live lookup 0 rows`
- Hosted authenticated extension smoke: `access flow passed; full current feature smoke PENDING`
