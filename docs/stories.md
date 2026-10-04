# Invoice Studio delivery stories

Status distinguishes verified local behavior from business-data acceptance and proposed expansion. Estimates and production gates are in the solution design.

## INV-01 · Select and confirm an invoice batch

As an **AP operator**, I want to choose several invoices and confirm the processing rules, so I know which files, data and reader will be used.

**Status:** Local and hosted processing gate verified.

- Cancel sends no invoice-processing request.
- A confirmed plan is bound to filenames/sizes, engine/model, reference version and tolerances.
- Changed rules require a fresh confirmation.

## INV-02 · Read digital invoices and scans

As an **AP operator**, I want to extract headers and every item line from supported files, so I avoid retyping supplier documents.

**Status:** Prior three-reader baseline verified; readable-PDF fast path and visible-stage release validation pending.

- Digital invoice2data, PaddleOCR scan and Docling reader paths produce structured synthetic results.
- A readable PDF with no matching supplier template keeps native text and skips expensive image OCR; a confirmed AI fallback can use that text.
- The job view names the active stage rather than presenting extraction as one opaque wait.
- Unread or incomplete output records an explainable reader trace.
- Unsupported, encrypted, oversized and multi-invoice files receive a clear exception or split instruction.

## INV-03 · Use a chosen AI connection for exceptions

As an **AP operator**, I want to select a provider/model and enable automatic fallback, so unfamiliar layouts can be proposed for review.

**Status:** Adapters verified with mocked provider responses; live account evaluation pending.

- AI is called after explicit selection/confirmed fallback and local extraction failure or incompleteness.
- Provider output must pass the strict invoice schema; missing facts remain missing.
- Credentials never appear in browser storage, exported files or ordinary API errors.
- Claude setup-token and move-only ChatGPT owner-transfer paths remain subject to live account eligibility and release validation; mocked adapters do not prove subscription inference.

## INV-04 · Map and approve business reference extracts

As an **data steward**, I want to profile the whole item and PO/GRN files and approve explicit mappings, so the validator uses evidence with known meaning.

**Status:** Actual-source lookup indexes 114,940 item rows and 297,199 PO rows; release validation and business approval remain pending.

- Preserve original bytes, hashes and row/column provenance; inspect all populated cells.
- Separate unique matches, duplicates, ambiguous joins and missing fields.
- Identifier and product-name searches show source/conflict context and require explicit confirmation; confirmed lookup evidence is not approved matching data.
- Unknown seller/buyer/route, receipt acceptance and invoiced baseline block approval; never default them silently.

## INV-05 · Match the exact supplier route and items

As an **AP operator**, I want to check supplier, site, company, PO, item identity and unit, so an invoice cannot borrow approval from another market or entity.

**Status:** Canonical matching implemented and tested.

- Domestic and cross-border routes require their exact approval tuple.
- Conflicting SKU and barcode identity is held.
- Location type comes from an explicit reference field, not a numeric prefix.

## INV-06 · Protect receipt quantity and commercial controls

As an **finance owner**, I want to check price, accepted receipt balance and tax with decimal rules, so we avoid incorrect or duplicate charges.

**Status:** Implemented and tested.

- Accepted receipts minus returns, external baseline and this app allocations determine available quantity.
- Repeated lines and multiple invoices consume quantity cumulatively.
- Tolerance and currency rounding are explicit; no credit or mixed-tax allocation is invented.

## INV-07 · Review evidence and resolve exceptions

As an **AP reviewer**, I want to compare the source with editable fields and record review, so every exported value has checked evidence.

**Status:** Implemented and browser verified.

- Review confirmation cannot override validation holds.
- Concurrent edits with stale revisions are rejected.
- Rules changing during extraction prevent stale approval and require a new processing plan.
- A manual draft created from an active job cannot alter the job revision, extracted invoice or review status.

## INV-08 · Export one joined three-sheet workbook

As an **AP operator**, I want to download all selected ready invoices into the exact target, so the receiving team can import without manual Excel assembly.

**Status:** Implemented and browser/workbook verified.

- Header/Tax_Breakdown/Details have exactly 13/3/6 columns and no helper formulas.
- Invoice transaction IDs are 1..N and consistent across all three sheets.
- Export rechecks and reserves quantities atomically; repeat download does not duplicate an allocation.

## INV-09 · Recover and audit local operations

As a **service owner**, I want to retain sources, rule versions, edits and exports, so a failed reader or restart can be explained and retried.

**Status:** Instance-replacement persistence verified; database backup restoration remains a production gate.

- Restarted in-flight jobs visibly require retry; completed exports remain downloadable.
- Export records include reference version, policy and allocations.
- Recovery procedure preserves the database plus the corresponding encryption key.

## INV-10 · Run a restricted cloud workspace

As a **workspace owner**, I want to sign in to a Firebase frontend backed by durable GCP services, so the team can use a hosted portal without losing evidence on restart.

**Status:** Hosted synthetic workflow and instance-replacement persistence verified.

- Unauthenticated and non-allowlisted accounts cannot access invoices, references or connections.
- Database and document evidence survive a replacement application instance.
- Live UI, auth rejection, synthetic extraction/export and persistence smoke checks pass before operational handoff.
- Current source additions require a fresh authenticated deployment smoke; prior hosted results are not evidence that those additions are deployed.

## INV-11 · Qualify suppliers for a new store or market

As a **procurement owner**, I want to reuse approved coverage and start discovery only for gaps, so store openings do not repeat every supplier task.

**Status:** Proposed next product increment.

- Same-market coverage reuses valid route facts with provenance/expiry.
- New market or buying entity creates an explicit qualification case.
- AI suggestions cannot approve a legal entity, route or commercial agreement.

## INV-12 · Normalize supplier items for buying and digital channels

As an **item-data steward**, I want to turn varied supplier files into approved item identities and channel content, so purchasing and commerce teams can reuse a consistent catalog.

**Status:** Proposed next product increment.

- Supplier codes, internal items, barcodes, packaging and units have explicit mappings.
- Conflicting identities and unit conversions need steward approval.
- Channel-specific text, attributes, images and translations are versioned separately from purchasing identity.

## INV-13 · Download an explicitly unvalidated manual draft

As an **AP operator**, I want to edit every target workbook field and download a draft while OCR is still running, so urgent manual work is not blocked by a slow reader.

**Status:** Implemented in the current source; integrated release and hosted validation pending.

- Header/Tax_Breakdown/Details expose exactly 13/3/6 editable fields with one joined transaction, leading-zero-safe identifiers and Decimal arithmetic.
- Download requires an explicit unvalidated acknowledgement and the filename, workbook properties and cell comments say `DRAFT_UNVALIDATED` and no receipt reservation.
- Draft creation does not read the active extraction payload, approve a reference, change job status/revision/invoice, write the approved export ledger or reserve receipt quantity.

## INV-14 · Search large source extracts without implying approval

As a **data steward**, I want to search actual item and PO sources by identifier or product name, so I can find evidence without loading unapproved rows into canonical matching.

**Status:** Current source indexes 114,940 item rows and 297,199 PO rows; integrated release validation pending.

- Results retain source row and conflict/duplicate context, with filters and bounded pagination.
- The operator explicitly confirms a selected result before it can populate the manual workspace.
- Summary, search and confirmation state clearly that source evidence is not approved for matching, receipt allocation or export readiness.

## INV-15 · Move an owner subscription connection to the hosted workspace

As a **workspace owner**, I want to connect the restricted Claude CLI or move my ChatGPT registration into my authenticated hosted workspace, so the provider can use my authorized account without sharing a raw secret through the browser state.

**Status:** Connection and transfer controls are implemented in source; live subscription inference, release validation and hosted deployment pending.

- Claude setup tokens are encrypted, omitted from state, removable, and supplied only to the restricted server CLI with tools disabled.
- ChatGPT starts local OAuth under the application's own registration; successful export constructs a one-time bundle, deletes local encrypted credentials, disconnects and clears the active local account.
- Only an authenticated cloud owner can import and verify the bundle. Export is denied in cloud mode, import is denied locally, and no test result implies provider eligibility or live inference quality.
