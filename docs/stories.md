# Invoice Studio delivery stories

Status distinguishes verified local behavior from business-data acceptance and proposed expansion. Estimates and production gates are in the solution design.

## INV-01 · Select and confirm an invoice batch

As an **AP operator**, I want to choose several invoices and confirm the processing rules, so I know which files, data and reader will be used.

**Status:** Local implementation verified.

- Cancel sends no invoice-processing request.
- A confirmed plan is bound to filenames/sizes, engine/model, reference version and tolerances.
- Changed rules require a fresh confirmation.

## INV-02 · Read digital invoices and scans

As an **AP operator**, I want to extract headers and every item line from supported files, so I avoid retyping supplier documents.

**Status:** Local synthetic tests verified.

- Digital invoice2data, PaddleOCR scan and Docling reader paths produce structured synthetic results.
- Unread or incomplete output records an explainable reader trace.
- Unsupported, encrypted, oversized and multi-invoice files receive a clear exception or split instruction.

## INV-03 · Use a chosen AI connection for exceptions

As an **AP operator**, I want to select a provider/model and enable automatic fallback, so unfamiliar layouts can be proposed for review.

**Status:** Adapters verified with mocked provider responses; live account evaluation pending.

- AI is called after explicit selection/confirmed fallback and local extraction failure or incompleteness.
- Provider output must pass the strict invoice schema; missing facts remain missing.
- Credentials never appear in browser storage, exported files or ordinary API errors.

## INV-04 · Map and approve business reference extracts

As an **data steward**, I want to profile the whole item and PO/GRN files and approve explicit mappings, so the validator uses evidence with known meaning.

**Status:** Full private profiling active; source adapter and business approval pending.

- Preserve original bytes, hashes and row/column provenance; inspect all populated cells.
- Separate unique matches, duplicates, ambiguous joins and missing fields.
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

## INV-08 · Export one joined three-sheet workbook

As an **AP operator**, I want to download all selected ready invoices into the exact target, so the receiving team can import without manual Excel assembly.

**Status:** Implemented and browser/workbook verified.

- Header/Tax_Breakdown/Details have exactly 13/3/6 columns and no helper formulas.
- Invoice transaction IDs are 1..N and consistent across all three sheets.
- Export rechecks and reserves quantities atomically; repeat download does not duplicate an allocation.

## INV-09 · Recover and audit local operations

As an **service owner**, I want to retain sources, rule versions, edits and exports, so a failed reader or restart can be explained and retried.

**Status:** Local audit/restart behavior implemented; recovery exercise pending.

- Restarted in-flight jobs visibly require retry; completed exports remain downloadable.
- Export records include reference version, policy and allocations.
- Recovery procedure preserves the database plus the corresponding encryption key.

## INV-10 · Run a restricted cloud workspace

As an **workspace owner**, I want to sign in to a Firebase frontend backed by durable GCP services, so the team can use a hosted portal without losing evidence on restart.

**Status:** Cloud implementation and deployment in progress.

- Unauthenticated and non-allowlisted accounts cannot access invoices, references or connections.
- Database and document evidence survive a replacement application instance.
- Live UI, auth rejection, synthetic extraction/export and persistence smoke checks pass before operational handoff.

## INV-11 · Qualify suppliers for a new store or market

As an **procurement owner**, I want to reuse approved coverage and start discovery only for gaps, so store openings do not repeat every supplier task.

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
