# Verification record

This is an implementation test record, not a measured claim about accuracy across supplier invoices.

## Automated and local verification

The repository suite currently passes **214 tests with no skips**, including the real PostgreSQL contract. Ruff is clean. Both Chromium workflows and all four GitHub checks pass, and all three packaged reader warm-ups recovered two synthetic lines. Final revision `inv-studio-api-00009-vxw` has 100% traffic and uses the tested core image plus the cancel/footer fixes. The owner-only allowlist, email-verified owner-account flag, anonymous/direct-service denial and temporary-identity deletion checks passed. The private evidence catalog completed atomic cutover with 412,139 live rows, exact source-kind counts and a sanitized archive receipt beginning `d06678`; authenticated API and UI lookup QA passed.

- Backend tests cover exact identity and scope, SKU/GTIN conflicts, receipt balances, returns, repeated lines, duplicate invoices, currency rounding, review revisions, atomic batch export, malformed reference inputs and secret encryption.
- Preflight regressions prove invalid uploads do not consume confirmation, missing confirmation blocks processing, changed rules invalidate a plan, and a mid-extraction rule change cannot enrich or approve the result under unconfirmed rules.
- Mocked provider tests cover managed Vertex AI, OpenAI, Anthropic, local Claude and ChatGPT request/response contracts, structured output and failure paths. Vertex uses Application Default Credentials, a fixed Google endpoint, bounded inline document/text input, a strict response schema and safe errors.
- Cloud identity tests require a verified, allowlisted email and check token revocation. Anonymous access to invoices, references and downloads is rejected. PostgreSQL/GCS contract tests cover transaction locking, rollback, ciphertext credentials, raw export SQL and blob recovery; the real-database test runs when `INV_STUDIO_TEST_DATABASE_URL` is supplied.
- Real Chromium tests cover desktop/mobile layout, no processing before confirmation, cancel behavior, multi-file upload, review/export holds, visible unsaved edits, combined review-only download without job/ledger mutation, approved two-invoice batch download, exact 13/3/6 shapes, transaction joins and no provider-key browser storage.
- Printed line net and tax values are preserved separately from printed unit price. Regressions prove review saves retain them and neither extraction nor quality checks reprice a line to force arithmetic.
- Printed buyer name is preserved separately from the internal buyer/company code. Regressions do not allow a source name to become an internal code without approved reference evidence.
- Native-layout regressions require a strong invoice heading, reject purchase orders, extract explicit labeled facts and spatial table rows, preserve leading zeros, leave conflicts/ambiguous dates unset and never infer internal business codes, currency or header totals from line arithmetic.
- Extraction outcome regressions record `text_read`, `fields_extracted` or `extraction_failed`; the legacy `extracted` event is displayed only as `Reader run finished (legacy event)`. Trace output distinguishes a failed engine from text-only output with zero structured items. Field completeness is labelled as read coverage, not accuracy. When invoice2data receives a scanned image through Paddle OCR, the trace names Paddle as the input reader.
- Manual-draft regressions must prove exact 13/3/6 sheets, leading-zero text, no formulas, explicit acknowledgement, `DRAFT_UNVALIDATED` disclosure, download during processing, and no change to job/revision/invoice/reference approval/export ledger/receipt allocation.
- Lookup regressions exercise the 114,940-item / 297,199-PO/GRN source profile (412,139 rows), identifier and fuzzy product-name search, price/quantity/unit clues, pagination/filtering, conflict display and explicit human confirmation while proving that lookup records are not approved matching data. The guarded hosted import is not available until post-import verification passes.
- Connection regressions distinguish an encrypted saved key from verified access, preserve a connected user's selected model, and offer an available connected model only when no connected model is selected. They also cover Claude setup-token encryption/removal and cloud authentication, plus the ChatGPT move boundary: local export only, credential detachment after successful bundle construction, hosted import only, and no secret returned through state or errors.
- Deletion regressions require explicit confirmation and current revisions, reject active jobs and partial exported batches, remove live jobs, source objects, detailed audit evidence and workbook copies, and retain only a minimal deletion audit plus the invoice-key/PO-line allocation tombstone required for duplicate and receipt controls. Provider backups, logs, versions and soft deletion remain outside the live-store operation.

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
| PaddleOCR + layout, AI off | 6/6 lines; 60/60 adjudicated header, identifier and numeric facts exact across three scans. On a separate challenging two-page scan, an 80.755 s local baseline recovered 23/23 rows and 90/99 facts (90.9%). Its bounded 86.542 s higher-resolution alternate met every selection gate and improved the whole result to 96/99 (97.0%): 6/7 headers, 21/23 SKUs and 23/23 quantities, prices and printed net amounts; the absent PO stayed null. Total local reader time was about 168 s and peak cgroup memory was 2.31 GiB. | Descriptions remain unproven because raw OCR can fuse/space words. Counting all six descriptions in the adjudicated three-scan set as incorrect gives a conservative 60/66 = 90.91% floor. Revision `00008` makes the alternate eligible only for PDFs of at most two pages when baseline quantity/price is missing and baseline time is at most 150 seconds. It gives Paddle up to 360 seconds total and passes the exact remaining budget to the alternate worker; Docling stays at 240 seconds. The alternate replaces the whole result only when row count and invoice number/date/PO/currency/net/tax do not regress, missing quantity/price improves and printed net reconciles. The three earlier scans did not trigger it and retained their baselines. Heavy local OCR is serialized; an early alternate failure keeps the baseline. |
| Docling, AI off | 5/5 lines in 34 s; 1/1 line on a one-page document; 17/17 lines on a two-page document in 92 s; three earlier scans now yield 6/6 rows and 18/18 header fields present after adapter fixes | Native-table conversion and those bounded scan structures work; field presence is not source-gold accuracy |
| Docling failure boundary | Ten-page document exceeded 528 s and was terminated. Exposing RapidOCR's measured cells improved one challenging two-page scan from 26 assembled blocks to 278 OCR cells and 10/23 parsed lines in 126.37 s; source-position comparison found 0 exact SKUs, 1 exact quantity and 2 exact prices | The expected 95% and broad scan support are unresolved; 10/23 is only line coverage and the field comparison is poor, so Docling remains a selectable, review-required path |
| Managed Vertex | 15 documents, 570 lines, all source-tested fields exact after retry, from one layout family | Useful within-sample evidence only; it does not establish held-out supplier, scan, language or page-length accuracy |

Revision `00007` read the challenging two-page scan baseline in 134.082 seconds and correctly retained its 90/99 result because that revision used the 100-second eligibility gate and 240-second Paddle budget. Revision `00008` is live with the Paddle-only budget and eligibility changes described above. Its hosted Paddle run completed the adaptive path in 242.98 seconds and returned 23/23 rows and 96/99 checked facts (97.0%). Separately, the hosted native path recovered 193/193 rows, 6/6 checked headers and 1,351/1,351 checked line facts in 10.21 seconds. A hosted managed-AI check on the challenging scan returned 23 rows and 97/99 checked facts in 16.18 seconds, preserved the printed buyer name and kept the internal buyer code null. These are distinct path- and layout-specific checks; do not combine them into a broad deployment accuracy claim.

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
- Two real processed invoices produced one review workbook with exactly Header/Tax_Breakdown/Details, 13/3/6 columns and transaction IDs 1 and 2. This review-only workbook did not approve or reserve receipts.
- After replacing the Cloud Run revision, all five jobs remained present. The workbook and every original document retained their exact SHA-256 hashes.
- Actual desktop/mobile browser checks cover Firebase password sign-in, authenticated source preview, Excel download and sign-out.

This proves the earlier hosted synthetic workflow, not accuracy or export eligibility for unapproved business extracts. Database backup restoration and representative held-out evaluation remain separate acceptance work. The narrow private measurements above must retain their supplier/layout denominators.

Revision `00008` passed adaptive Paddle, authenticated API and References UI lookup, deletion/model-control and cancel-dialog checks. Final revision `inv-studio-api-00009-vxw` uses the same tested core image plus the cancel/footer fixes, has 100% traffic, and passed the owner-only allowlist and unauthenticated-denial cleanup checks. No live ChatGPT or Claude subscription inference has been tested or certified here.

## Release owner fill-in

- Revision: `inv-studio-api-00009-vxw at 100% traffic; same tested core image plus cancel/footer fixes; owner-only allowlist, verified-account flag, smoke-identity deletion and unauthenticated denial PASSED`
- Repository tests: `214 passed; 0 skipped; Ruff, both Chromium workflows and four GitHub checks passed`
- PostgreSQL: `included in the no-skip repository run`
- Ruff: `clean`
- Browser: `full Chromium workflow passed locally`
- Container/readers: `all three packaged reader warm-ups recovered two synthetic lines; final image sha256 063f72cc… live on 00009-vxw`
- Native, AI off: `15/15 PDFs; 570/570 lines; 3,990/3,990 tested line fields exact for one layout family`
- Paddle, AI off: `6/6 adjudicated lines; conservative 60/66 = 90.91% floor with all descriptions treated unproven; bounded adaptive two-page result 23/23 rows, 6/7 headers and 96/99 checked facts (97.0%), about 168 s combined, 2.31 GiB peak`
- Docling, AI off: `bounded native examples work; challenging two-page scan 10/23 line coverage after measured-cell recovery; ten-page timeout unresolved`
- Managed Vertex: `15 documents/570 lines, all source-tested fields exact for one layout family; calculated usage USD 0.3719325`
- Private catalog: `atomic cutover succeeded; live 412,139 = 114,940 item + 297,199 PO/GRN; archive receipt d06678…; 94 healthy guard probes; state 1.917 s; disk 5.244/10.464 GB; API lookup QA exact-name rank one 2.831 s, typo rank one 1.182 s, PO 0.462 s, impossible strict filter empty; UI exact/typo rank one 1.222/1.293 s, three exact PO results 0.446 s, warnings retained, zero mutations/page errors`
- Hosted authenticated extension smoke: `native 193-row, Paddle 96/99 in 242.98 s, managed-AI 97/99 in 16.18 s, two-invoice review workbook, lookup, deletion/model and cancel-dialog checks passed; 00009-vxw owner-only allowlist and unauthenticated-denial cleanup PASSED`
