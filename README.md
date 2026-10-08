# Inv Studio

Read supplier invoices, check them against your reference data, resolve exceptions and download the three-sheet invoice workbook. Runs locally or in a restricted Firebase/GCP pilot. The repository contains synthetic examples only.

**Hosted portal:** https://inv-studio-740495548022.web.app — final revision `inv-studio-api-00009-vxw` has 100% traffic. It uses the tested core image plus the cancel/footer fixes. The allowlist is owner-only, the owner account remained email-verified, anonymous and direct-service API calls returned 401, and the temporary smoke identity was deleted. The private evidence catalog completed atomic cutover and authenticated API/UI lookup QA. Validated export still requires separately approved business references.

## Start

Python 3.12 is the tested runtime. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
uv venv
uv pip install -e '.[test]'
./scripts/run.sh
```

Open **http://127.0.0.1:8765**. Choose **Try the demo**, confirm the proposed processing rules, inspect the document and confirm your review. The demo explicitly loads synthetic reference data. Avoid loading it over your business references.

The base install reads digital documents using invoice2data. To add the two local OCR engines on Linux CPU:

```bash
./scripts/install-ocr.sh
```

The first image-OCR run downloads public model weights and takes longer. For a readable PDF with no matching supplier template, the automatic path keeps the native text, applies conservative invoice-layout rules and skips the expensive image-OCR pass; it can continue to the selected AI provider when the confirmed plan permits fallback. The job view distinguishes text read from invoice fields extracted instead of presenting either as a generic success. No invoice is sent to an AI provider unless you enable fallback or select AI and confirm the processing plan. Credentials are encrypted on the server and never stored in browser storage.

## What works

- Multiple files, each containing one invoice: PDF, PNG, JPEG, WebP, BMP, TIFF, DOCX, XLSX, CSV, TXT and structured invoice JSON. A readable PDF that binds several invoices back to back is split by page into one job per invoice number at upload; when the pages cannot be split cleanly the job is refused with every invoice number named.
- Confirm the engine, fallback provider/model, reference version and tolerances before processing. Cancel makes no processing request.
- Automatic reading through supplier templates, conservative native-layout rules, PaddleOCR and Docling, with optional selected AI fallback for unread or incomplete invoices. When the invoice2data path receives a scan, its trace names Paddle as the image-text input instead of implying invoice2data read the pixels itself. Native rules require an invoice heading, reject purchase orders, leave conflicting labels unset and never invent internal codes or totals. You can select a particular reader or AI explicitly. The trace distinguishes an engine failure from text-only output with zero structured items, and labels field completeness as read coverage rather than accuracy.
- Managed Google Cloud AI through Vertex AI and the workspace service identity, plus OpenAI and Anthropic API connections. A saved API key means only that an encrypted value exists; retrieving the provider's model list or completing a probe verifies access. When a connected provider has no selected connected model, the UI can offer its available model automatically, but it does not replace a user's still-connected selection. A Claude setup token can connect the restricted Claude CLI on the server where that CLI is installed. ChatGPT uses the application's own local OAuth registration; an explicit export/import moves its encrypted credentials from the local instance to the authenticated hosted owner workspace and disconnects the local copy. Provider/account eligibility, model access, project charges and usage limits apply.
- Canonical reference import and strict matching remain the approval path. The private evidence catalog completed atomic cutover with **412,139 live rows**—114,940 item rows and 297,199 PO/GRN rows—and a sanitized archive receipt beginning `d06678`. Ninety-four guard probes remained healthy through the deployments; the final state response took 1.917 seconds, and database disk use was 5.244 GB of 10.464 GB. Authenticated API lookup QA returned the expected rank-one exact-name candidate in 2.831 seconds, rank-one typo candidate in 1.182 seconds and PO result in 0.462 seconds; an impossible strict filter returned no result and every candidate retained its confirmation flags. The final References UI check showed both source counts, rank-one exact and typo results in 1.222 and 1.293 seconds, three exact PO results in 0.446 seconds, visible provenance/confirmation warnings, zero mutations and zero page errors. Search results retain source/conflict context and require explicit operator confirmation; they do not become approved matching data. Supplier/site mappings and the accepted-receipt and prior-invoicing baseline also remain unresolved.
- Editable evidence review, duplicate protection, cumulative receipt allocation and atomic approved export.
- Exactly **Header (13 columns), Tax_Breakdown (3), Details (6)**. Printed line net and tax amounts are preserved separately from printed unit price; the application does not reprice a line to force arithmetic. A combined `EXTRACTION_REVIEW_ONLY` workbook can contain multiple saved processed invoices without approval or ledger changes. Approved single/batch exports remain separate, revalidate and reserve receipts. The manual editor can also download an acknowledged `DRAFT_UNVALIDATED` workbook while OCR is running.
- Seller name and printed buyer name remain source facts. The internal buyer/company code is a separate reference field and is never populated from a printed company name alone.
- Permanent deletion requires an explicit confirmation and current revisions. It removes live job data, source objects, detailed audit evidence and downloadable workbooks. Approved invoices retain only the minimal invoice-key and PO-line quantity ledger needed for duplicate and receipt controls, plus a minimal deletion audit. Provider backups, logs, object versions and soft-delete copies follow their own retention policies.

## Your reference files

Download the reference template in the portal. The canonical workbook has Suppliers, Routes, Items, POs, POLines, Receipts and TaxRules sheets. Canonical JSON and ODS equivalents are accepted. A completed target invoice workbook is detected and rejected as a reference file.

Large business Item Master and PO/GRN extracts need a mapped staging step; do not rename their tabs and assume they are approved references. Supplier entity, site, buying company, route, currency, receipt status and baseline already-invoiced quantity must be source-proven or explicitly supplied by the data owner. The [data contract](docs/data-contract.md) explains these rules. Source files, private analysis, keys and `.data` must not be committed.

The lookup index helps an operator find a product or PO record in those large extracts without loading it into the approval engine. Confirming a lookup result copies evidence into the manual form only; it does not approve a supplier, route, item, receipt or invoice for the guarded export.

## Read the delivery pack

- [Solution and build design](docs/solution-design.md): product decisions, architecture, roadmap, responsibilities and sizing assumptions.
- [Operating guide](docs/operator-guide.md): the day-to-day process and exception handling.
- [Data and Excel contract](docs/data-contract.md): field meaning, matching and output types.
- [Stories and acceptance criteria](docs/stories.md).
- [Interactive design artifact](docs/review-artifact/index.html) — open locally in a browser.
- [Verification record](docs/validation.md).
- [Cloud deployment and recovery](docs/cloud-deployment.md).
- [SLT PowerPoint](docs/delivery/Invoice_Studio_SLT.pptx) and [Word design and stories](docs/delivery/Invoice_Studio_Design_and_Stories.docx).

## Verify

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check app tests scripts
npm ci
npx playwright install chromium
npm run test:browser
```

The browser test runs against the server on port 8765 and creates synthetic jobs, imports synthetic references and exports workbooks. Use a separate data directory for testing:

```bash
INV_STUDIO_DATA=/tmp/inv-studio-test ./scripts/run.sh
```

**Current release evidence:** 214 backend tests pass with PostgreSQL included and no skips; Ruff, both Chromium workflows and all four GitHub checks pass. All three packaged reader warm-ups recovered two synthetic lines. Native PDF tables recovered 570/570 rows and 3,990/3,990 tested line facts from one layout family. Three adjudicated scans produced a 60/66 (90.91%) conservative Paddle floor when every unproven description is counted wrong. On a separate challenging two-page scan, the 80.755-second local baseline recovered 23/23 rows and 90/99 checked facts (90.9%). Because quantity/price cells were missing, a bounded 86.542-second higher-resolution pass ran and the whole alternate met every non-regression gate, improving the result to 96/99 (97.0%): 23/23 rows, 6/7 headers, 21/23 SKUs and 23/23 quantities, prices and printed net amounts. The absent PO stayed null. Combined local reader time was about 168 seconds and peak cgroup memory was 2.31 GiB. The three earlier scan cases did not trigger the alternate and retained their baseline results. Heavy local OCR work is serialized to fit the memory envelope while native/AI work can continue; an early alternate failure keeps the baseline. Revision `00007` took 134.082 seconds for the hosted baseline and correctly retained 90/99 because it used the older 100-second eligibility gate and 240-second Paddle budget. Revision `00008` introduced a Paddle-only 360-second reader limit, a 150-second baseline eligibility ceiling and the exact remaining budget passed to the worker. Its hosted Paddle run completed the adaptive path in 242.98 seconds and returned the same 23 rows and 96/99 checked facts (97.0%). Docling remains capped at 240 seconds, and the preflight discloses the six-minute Paddle limit. Separately, a hosted managed-AI check on the challenging scan returned 23 rows and 97/99 checked facts in 16.18 seconds, preserved the printed buyer name and left the internal buyer code null. Docling recovered bounded native examples and 10/23 lines on that scan, while a ten-page timeout remains unresolved. A separate private managed-AI check covered 15 documents and 570 lines from one layout family, with all source-tested fields exact. Final revision `00009-vxw` uses the same tested core image; the owner-only allowlist, verified owner-account flag, temporary-identity deletion and unauthenticated denial checks passed. These narrow cohorts do not establish broad supplier accuracy. Field completeness and arithmetic checks are diagnostics, not accuracy. See [docs/validation.md](docs/validation.md) for denominators, limits and token usage.

## Operational boundaries

The local mode is a single-operator MVP bound to loopback. The hosted pilot adds Firebase authentication, an owner allowlist, Cloud SQL, private Cloud Storage and Secret Manager. It still uses one Cloud Run instance and an in-process queue. Drain jobs before deployments; interrupted work requires a confirmed retry. A ChatGPT credential bundle is a one-time ownership transfer: successful export removes the local encrypted copy, so protect the file until the hosted import succeeds and delete it afterward. Add role separation, durable distributed workers, schema migrations, backup restoration tests, monitoring and business acceptance before a wider rollout. Neither ledger detects invoices posted in another system unless that history is imported.

Invoice limits: 12 MB, 20 PDF pages or image frames, 25 megapixels per image, 1,000 structured lines. Office archives must expand to at most 30 MB. A readable PDF that binds several invoices is split by page at upload (one job per invoice number, the original file name kept with a part suffix); a page that prints two invoice numbers, or a number that reappears after another, is refused naming both numbers and must be split by hand. Scanned PDFs without a text layer are not split. Credit notes, multiple POs, mixed tax rates, unit conversions, freight and discount allocation need dedicated contracts before automatic export. Unsupported or ambiguous cases remain on hold.

The application code is MIT licensed. Reader libraries and downloaded model weights retain their own licenses; review them before redistribution.
