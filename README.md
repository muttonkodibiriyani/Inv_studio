# Inv Studio

Read supplier invoices, check them against your reference data, resolve exceptions and download the three-sheet invoice workbook. Runs locally. The repository contains synthetic examples only.

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

The first OCR run downloads public model weights and takes longer. No invoice is sent to an AI provider unless you enable fallback or select AI and confirm the processing plan. API credentials are entered in the portal, encrypted on the server and never stored in browser storage.

## What works

- Multiple files, each containing one invoice: PDF, PNG, JPEG, WebP, BMP, TIFF, DOCX, XLSX, CSV, TXT and structured invoice JSON.
- Confirm the engine, fallback provider/model, reference version and tolerances before processing. Cancel makes no processing request.
- Automatic reading through invoice2data → PaddleOCR → Docling, with optional selected AI fallback for unread or incomplete invoices. You can select a particular reader or AI explicitly.
- OpenAI and Anthropic API connections; an official ChatGPT subscription sign-in adapter; a local Claude CLI adapter when installed and signed in. Subscription availability and usage limits depend on the provider and account. Live sign-in and paid inference require your connection; they are not certified by mocked tests.
- Reference import, strict matching, editable evidence review, duplicate protection, cumulative receipt allocation and atomic batch export.
- Exactly **Header (13 columns), Tax_Breakdown (3), Details (6)**. Each invoice receives a transaction number, shared across all three sheets. Leading-zero identifiers remain text.

## Your reference files

Download the reference template in the portal. The canonical workbook has Suppliers, Routes, Items, POs, POLines, Receipts and TaxRules sheets. Canonical JSON and ODS equivalents are accepted. A completed target invoice workbook is detected and rejected as a reference file.

Large business Item Master and PO/GRN extracts need a mapped staging step; do not rename their tabs and assume they are approved references. Supplier entity, site, buying company, route, currency, receipt status and baseline already-invoiced quantity must be source-proven or explicitly supplied by the data owner. The [data contract](docs/data-contract.md) explains these rules. Source files, private analysis, keys and `.data` must not be committed.

## Read the delivery pack

- [Solution and build design](docs/solution-design.md): product decisions, architecture, roadmap, responsibilities and sizing assumptions.
- [Operating guide](docs/operator-guide.md): the day-to-day process and exception handling.
- [Data and Excel contract](docs/data-contract.md): field meaning, matching and output types.
- [Stories and acceptance criteria](docs/stories.md).
- [Interactive design artifact](docs/review-artifact/index.html) — open locally in a browser.
- [Verification record](docs/validation.md).

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

## Operational boundaries

This delivery is a local, single-operator MVP. Bind to loopback. Before a shared service rollout, add authenticated users, role separation, a managed secrets service, durable distributed workers, database migrations, backup/restore tests, monitoring and business acceptance testing. The current SQLite ledger is local to this installation; it cannot detect an invoice posted in another system unless that history is imported.

Invoice limits: 12 MB, 20 PDF pages or image frames, 25 megapixels per image, 1,000 structured lines. Office archives must expand to at most 30 MB. Multi-invoice PDFs must be split first. Credit notes, multiple POs, mixed tax rates, unit conversions, freight and discount allocation need dedicated contracts before automatic export. Unsupported or ambiguous cases remain on hold.

The application code is MIT licensed. Reader libraries and downloaded model weights retain their own licenses; review them before redistribution.
