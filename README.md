# Inv Studio

Read supplier invoices, check them against your reference data, resolve exceptions and download the three-sheet invoice workbook. Runs locally or in a restricted Firebase/GCP pilot. The repository contains synthetic examples only.

**Hosted portal:** https://inv-studio-740495548022.web.app — access hotfix revision `00005-zdk` is deployed after an availability incident. Authenticated verification and deployment of the current feature bundle remain pending, so the repository does not claim that the newest extraction and batch features are live. Validated export still requires approved business references.

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

- Multiple files, each containing one invoice: PDF, PNG, JPEG, WebP, BMP, TIFF, DOCX, XLSX, CSV, TXT and structured invoice JSON.
- Confirm the engine, fallback provider/model, reference version and tolerances before processing. Cancel makes no processing request.
- Automatic reading through supplier templates, conservative native-layout rules, PaddleOCR and Docling, with optional selected AI fallback for unread or incomplete invoices. Native rules require an invoice heading, reject purchase orders, leave conflicting labels unset and never invent internal codes or totals. You can select a particular reader or AI explicitly, and the UI reports each active stage.
- Managed Google Cloud AI through Vertex AI and the workspace service identity, plus OpenAI and Anthropic API connections. A Claude setup token can connect the restricted Claude CLI on the server where that CLI is installed. ChatGPT uses the application's own local OAuth registration; an explicit export/import moves its encrypted credentials from the local instance to the authenticated hosted owner workspace and disconnects the local copy. Provider/account eligibility, model access, project charges and usage limits apply. No live subscription inference is claimed by the automated tests.
- Canonical reference import and strict matching remain the approval path. The private source profile contains **412,139 rows**—114,940 item rows and 297,199 PO/GRN rows—but the hosted lookup import is paused/cancelling during API recovery and is not confirmed available. When loaded, searches retain source/conflict context and require explicit operator confirmation; they do not become approved matching data. Supplier/site mappings and the accepted-receipt and prior-invoicing baseline also remain unresolved.
- Editable evidence review, duplicate protection, cumulative receipt allocation and atomic approved export.
- Exactly **Header (13 columns), Tax_Breakdown (3), Details (6)**. Printed line net and tax amounts are preserved separately from printed unit price; the application does not reprice a line to force arithmetic. A combined `EXTRACTION_REVIEW_ONLY` workbook can contain multiple saved processed invoices without approval or ledger changes. Approved single/batch exports remain separate, revalidate and reserve receipts. The manual editor can also download an acknowledged `DRAFT_UNVALIDATED` workbook while OCR is running.

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

**Current release evidence:** 148 backend tests pass with PostgreSQL included and no skips; Ruff and the full Chromium workflow pass. All three Docker reader warmups passed in 63 seconds and recovered both synthetic lines. Native PDF tables recovered 570/570 rows and 3,990/3,990 tested line facts from one layout family. On three private scans, Paddle recovered 6/6 lines and 60/60 adjudicated header, identifier and numeric facts; treating all six descriptions as unproven gives a conservative 60/66 (90.91%) floor. Docling recovered bounded native examples, but a ten-page run exceeded 528 seconds and three scans returned no reliable lines after 103–170 seconds. A private Vertex check covered 15 documents and 570 lines from one layout family, with all source-tested fields exact. These narrow cohorts do not establish broad supplier accuracy. See [docs/validation.md](docs/validation.md) for denominators, limits, token usage and pending hosted verification.

## Operational boundaries

The local mode is a single-operator MVP bound to loopback. The hosted pilot adds Firebase authentication, an owner allowlist, Cloud SQL, private Cloud Storage and Secret Manager. It still uses one Cloud Run instance and an in-process queue. Drain jobs before deployments; interrupted work requires a confirmed retry. A ChatGPT credential bundle is a one-time ownership transfer: successful export removes the local encrypted copy, so protect the file until the hosted import succeeds and delete it afterward. Add role separation, durable distributed workers, schema migrations, backup restoration tests, monitoring and business acceptance before a wider rollout. Neither ledger detects invoices posted in another system unless that history is imported.

Invoice limits: 12 MB, 20 PDF pages or image frames, 25 megapixels per image, 1,000 structured lines. Office archives must expand to at most 30 MB. Multi-invoice PDFs must be split first. Credit notes, multiple POs, mixed tax rates, unit conversions, freight and discount allocation need dedicated contracts before automatic export. Unsupported or ambiguous cases remain on hold.

The application code is MIT licensed. Reader libraries and downloaded model weights retain their own licenses; review them before redistribution.
