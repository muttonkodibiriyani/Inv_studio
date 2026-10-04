# Inv Studio

Read supplier invoices, check them against your reference data, resolve exceptions and download the three-sheet invoice workbook. Runs locally or in a restricted Firebase/GCP pilot. The repository contains synthetic examples only.

**Hosted portal:** https://inv-studio-740495548022.web.app — sign in with the approved owner's existing Firebase email/password account. The initial workspace contains synthetic reference data. Real invoices can be tested for extraction; validated export requires approved business references. The URL is the previously verified pilot baseline; the manual-draft, large-reference lookup and subscription-transfer changes in the current source still require release validation and a new deployment.

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

The first image-OCR run downloads public model weights and takes longer. For a readable PDF with no matching supplier template, the automatic path keeps the native text and skips the expensive image-OCR pass; it can continue to the selected AI provider when the confirmed plan permits fallback. The job view shows the active stage instead of presenting the whole chain as one wait. No invoice is sent to an AI provider unless you enable fallback or select AI and confirm the processing plan. Credentials are encrypted on the server and never stored in browser storage.

## What works

- Multiple files, each containing one invoice: PDF, PNG, JPEG, WebP, BMP, TIFF, DOCX, XLSX, CSV, TXT and structured invoice JSON.
- Confirm the engine, fallback provider/model, reference version and tolerances before processing. Cancel makes no processing request.
- Automatic reading through supplier templates, a readable-PDF fast path, PaddleOCR and Docling, with optional selected AI fallback for unread or incomplete invoices. You can select a particular reader or AI explicitly, and the UI reports each active stage.
- OpenAI and Anthropic API connections. A Claude setup token can connect the restricted Claude CLI on the server where that CLI is installed. ChatGPT uses the application's own local OAuth registration; an explicit export/import moves its encrypted credentials from the local instance to the authenticated hosted owner workspace and disconnects the local copy. Provider/account eligibility, model access and usage limits apply. No live subscription inference is claimed by the automated tests.
- Canonical reference import and strict matching remain the approval path. A separate, read-only product/PO lookup indexes the actual source extracts—**114,940 item rows and 297,199 PO rows**—for exact identifier or product-name evidence searches. Search results retain source/conflict context and require explicit operator confirmation; they do not become approved matching data.
- Editable evidence review, duplicate protection, cumulative receipt allocation and atomic approved export.
- Exactly **Header (13 columns), Tax_Breakdown (3), Details (6)**. Approved single/batch exports revalidate and reserve receipts. A separate manual editor exposes every target field and can immediately download an explicitly acknowledged `DRAFT_UNVALIDATED` workbook while OCR is still running. That draft does not validate references, reserve receipts or change the job.

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

**Current extension release validation:** 101 backend tests pass, including the real local PostgreSQL contract, Ruff is clean, and the container's invoice2data/PaddleOCR/Docling warm checks each recovered both synthetic lines. Browser, final image publication, private catalog import and authenticated hosted-extension checks remain pending in [docs/validation.md](docs/validation.md).

## Operational boundaries

The local mode is a single-operator MVP bound to loopback. The hosted pilot adds Firebase authentication, an owner allowlist, Cloud SQL, private Cloud Storage and Secret Manager. It still uses one Cloud Run instance and an in-process queue. Drain jobs before deployments; interrupted work requires a confirmed retry. A ChatGPT credential bundle is a one-time ownership transfer: successful export removes the local encrypted copy, so protect the file until the hosted import succeeds and delete it afterward. Add role separation, durable distributed workers, schema migrations, backup restoration tests, monitoring and business acceptance before a wider rollout. Neither ledger detects invoices posted in another system unless that history is imported.

Invoice limits: 12 MB, 20 PDF pages or image frames, 25 megapixels per image, 1,000 structured lines. Office archives must expand to at most 30 MB. Multi-invoice PDFs must be split first. Credit notes, multiple POs, mixed tax rates, unit conversions, freight and discount allocation need dedicated contracts before automatic export. Unsupported or ambiguous cases remain on hold.

The application code is MIT licensed. Reader libraries and downloaded model weights retain their own licenses; review them before redistribution.
