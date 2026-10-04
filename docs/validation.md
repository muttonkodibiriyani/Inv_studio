# Verification record

This is an implementation test record, not a measured claim about accuracy across supplier invoices.

## Verified locally

- Backend tests cover exact identity and scope, SKU/GTIN conflicts, receipt balances, returns, repeated lines, duplicate invoices, currency rounding, review revisions, atomic batch export, malformed reference inputs and secret encryption.
- Preflight regressions prove invalid uploads do not consume confirmation, missing confirmation blocks processing, changed rules invalidate a plan, and a mid-extraction rule change cannot enrich or approve the result under unconfirmed rules.
- Mocked provider tests cover OpenAI, Anthropic, local Claude and ChatGPT adapter request/response contracts, structured output and failure paths. No paid inference or real subscription sign-in is implied by these tests.
- Cloud identity tests require a verified, allowlisted email and check token revocation. Anonymous access to invoices, references and downloads is rejected. PostgreSQL/GCS contract tests cover transaction locking, rollback, ciphertext credentials, raw export SQL and blob recovery; the real-database test runs when `INV_STUDIO_TEST_DATABASE_URL` is supplied.
- Real Chromium tests cover desktop/mobile layout, no processing before confirmation, cancel behavior, demo/retry/upload, review/export holds, two-invoice batch download, exact 13/3/6 column shapes, transaction joins and no provider-key browser storage.
- Actual invoice2data native PDF, PaddleOCR synthetic scan and Docling native PDF runs were exercised. Paddle required CPU MKLDNN disabled in this environment; Docling required a line-separator template rule to recover both table lines. These fixes are included.

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

Cloud deployment must separately verify the live sign-in gate, protected API, authenticated file downloads, synthetic end-to-end workflow, and persistence after instance replacement. Capture deployment identifiers and test results in the release receipt. A local or mocked test does not establish that the hosted service works.
