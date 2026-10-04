# Verification record

This is an implementation test record, not a measured claim about accuracy across supplier invoices.

## Automated and local verification

The integrated backend suite currently passes **101 tests**, including the real local PostgreSQL contract, and Ruff is clean. The container's invoice2data, PaddleOCR and Docling warm checks each recovered both synthetic lines. A transient Docker writer lock interrupted packaging and was retried; it was not an application/readiness failure. **PENDING RELEASE VALIDATION:** record the final browser result, published image, private catalog import, authenticated hosted-extension smoke, deployed revision and date after those runs finish. Earlier hosted baseline results do not establish the current source candidate.

- Backend tests cover exact identity and scope, SKU/GTIN conflicts, receipt balances, returns, repeated lines, duplicate invoices, currency rounding, review revisions, atomic batch export, malformed reference inputs and secret encryption.
- Preflight regressions prove invalid uploads do not consume confirmation, missing confirmation blocks processing, changed rules invalidate a plan, and a mid-extraction rule change cannot enrich or approve the result under unconfirmed rules.
- Mocked provider tests cover OpenAI, Anthropic, local Claude and ChatGPT adapter request/response contracts, structured output and failure paths. No paid inference or real subscription sign-in is implied by these tests.
- Cloud identity tests require a verified, allowlisted email and check token revocation. Anonymous access to invoices, references and downloads is rejected. PostgreSQL/GCS contract tests cover transaction locking, rollback, ciphertext credentials, raw export SQL and blob recovery; the real-database test runs when `INV_STUDIO_TEST_DATABASE_URL` is supplied.
- Real Chromium tests cover desktop/mobile layout, no processing before confirmation, cancel behavior, demo/retry/upload, review/export holds, two-invoice batch download, exact 13/3/6 column shapes, transaction joins and no provider-key browser storage.
- Actual invoice2data native PDF, PaddleOCR synthetic scan and Docling native PDF runs were exercised. Paddle required CPU MKLDNN disabled in this environment; Docling required a line-separator template rule to recover both table lines. These fixes are included.
- Extension regressions must prove that the readable-PDF fast path skips image OCR when no supplier template matches, stage changes remain visible, and any permitted AI branch still returns to schema/reference/review gates.
- Manual-draft regressions must prove exact 13/3/6 sheets, leading-zero text, no formulas, explicit acknowledgement, `DRAFT_UNVALIDATED` disclosure, download during processing, and no change to job/revision/invoice/reference approval/export ledger/receipt allocation.
- Lookup regressions must exercise the 114,940-item / 297,199-PO source summary, identifier and product-name search, pagination/filtering, conflict display and explicit confirmation while proving that lookup records are not approved matching data.
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

## Business and deployment acceptance still required

Approve source mappings and unresolved business fields before real invoices can be called ready. Build an adjudicated evaluation set across supplier formats, languages, scan quality, partial receipts, returns and ambiguous identifiers. Verify the generated workbook in the actual receiving process.

## Previously verified hosted baseline

The Firebase/GCP endpoint was exercised with a temporary verified, allowlisted test identity:

- Anonymous protected API requests are rejected; authenticated session identity is checked by the server.
- Actual invoice2data PDF, PaddleOCR scan and Docling PDF jobs each recovered both synthetic invoice lines.
- Two separate reviewed invoices exported one workbook with exactly Header/Tax_Breakdown/Details, 13/3/6 columns, transaction IDs 1 and 2 on all sheets, and no formulas.
- After replacing the Cloud Run revision, all five jobs remained present. The workbook and every original document retained their exact SHA-256 hashes.
- Actual desktop/mobile browser checks cover Firebase password sign-in, authenticated source preview, Excel download and sign-out.

This proves the hosted synthetic workflow, not accuracy or export eligibility for the customer's unapproved business extracts. Database backup restoration, representative invoice evaluation and live paid AI inference remain separate acceptance work.

The baseline above predates the current manual-draft, lookup, readable-PDF stage UX and subscription-transfer source changes. Do not extend those hosted claims until a new revision is deployed and the authenticated checks are recorded. No live ChatGPT or Claude subscription inference has been tested or certified here.

## Release owner fill-in

- Revision: `PENDING`
- Backend: `101 passed`
- PostgreSQL: `real local contract passed`
- Ruff: `clean`
- Browser: `PENDING`
- Container/readers: `all three two-line warm checks passed; final image publication PENDING`
- Private catalog import: `PENDING`
- Hosted authenticated extension smoke: `PENDING`
