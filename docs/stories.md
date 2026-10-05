# Invoice Studio delivery stories

Status distinguishes verified local behavior from business-data acceptance and proposed expansion. Estimates and production gates are in the solution design.

## INV-01 · Select and confirm an invoice batch

As an **AP operator**, I want to choose several invoices and confirm the processing rules, so I know which files, data and reader will be used.

**Status:** Local and hosted processing gate verified.

- Cancel sends no invoice-processing request.
- A confirmed plan is bound to filenames/sizes, engine/model, reference version and tolerances.
- Changed rules require a fresh confirmation.
- Each file becomes one invoice job; up to two run concurrently and the rest remain visibly queued.

## INV-02 · Read digital invoices and scans

As an **AP operator**, I want to extract headers and every item line from supported files, so I avoid retyping supplier documents.

**Status:** Local paths measured; broad 95% quality and current hosted deployment remain pending.

- Native AI-off conversion recovered 570/570 lines and 3,990/3,990 tested line facts on one 15-PDF layout family. Paddle's conservative floor is 60/66 (90.91%) with descriptions unproven. Docling works on bounded native tables but failed the ten-page and scan cases recorded in validation.
- A readable PDF with no matching supplier template keeps native text and skips expensive image OCR. Conservative layout rules require an invoice heading, reject purchase orders, extract only explicit labels/spatial rows and never infer internal business codes or header totals.
- The job view names the active stage rather than presenting extraction as one opaque wait.
- Unread or incomplete output records an explainable reader trace.
- Unsupported, encrypted, oversized and multi-invoice files receive a clear exception or split instruction.

## INV-03 · Use a chosen AI connection for exceptions

As an **AP operator**, I want to select a provider/model and enable automatic fallback, so unfamiliar layouts can be proposed for review.

**Status:** Adapter contracts and a narrow 15-document/570-line source check passed; representative accuracy and current hosted deployment remain pending.

- Managed Vertex uses the hosted workspace service identity and project billing; API/subscription options remain selectable. AI is called only after explicit selection/confirmed fallback and local extraction failure or incompleteness.
- Provider output must pass the strict invoice schema; missing facts remain missing.
- Credentials never appear in browser storage, exported files or ordinary API errors.
- Claude setup-token and move-only ChatGPT owner-transfer paths remain subject to live account eligibility and release validation; mocked adapters do not prove subscription inference.
- The measured one-layout result cannot establish the expected 95% across suppliers, scans, languages and lengths. Frozen held-out gold still governs acceptance.

## INV-04 · Map and approve business reference extracts

As an **data steward**, I want to profile the whole item and PO/GRN files and approve explicit mappings, so the validator uses evidence with known meaning.

**Status:** The private source profile contains 412,139 rows; hosted import is paused/cancelling and live lookup availability is unconfirmed.

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
- Printed line net/tax amounts survive edits without changing printed unit price; there is no automatic repricing.

## INV-08 · Download combined review or approved workbooks

As an **AP operator**, I want one combined workbook for saved processed invoices and a separate approved export, so review can move quickly without weakening controls.

**Status:** Implemented and browser/workbook verified.

- Header/Tax_Breakdown/Details have exactly 13/3/6 columns and no helper formulas.
- Invoice transaction IDs are 1..N and consistent across all three sheets.
- `EXTRACTION_REVIEW_ONLY` accepts saved processed records, keeps unknown codes blank and changes no job, approval, ledger or allocation.
- Export rechecks and reserves quantities atomically; repeat download does not duplicate an allocation.

## INV-09 · Recover and audit local operations

As a **service owner**, I want to retain sources, rule versions, edits and exports, so a failed reader or restart can be explained and retried.

**Status:** Instance-replacement persistence verified; database backup restoration remains a production gate.

- Restarted in-flight jobs visibly require retry; completed exports remain downloadable.
- New audit events distinguish text read, fields extracted and extraction failed. The legacy `extracted` label says only that a reader run finished.
- Export records include reference version, policy and allocations.
- Recovery procedure preserves the database plus the corresponding encryption key.

## INV-10 · Run a restricted cloud workspace

As a **workspace owner**, I want to sign in to a Firebase frontend backed by durable GCP services, so the team can use a hosted portal without losing evidence on restart.

**Status:** Hosted synthetic workflow and instance-replacement persistence verified.

- Unauthenticated and non-allowlisted accounts cannot access invoices, references or connections.
- Database and document evidence survive a replacement application instance.
- Live UI, auth rejection, synthetic extraction/export and persistence smoke checks pass before operational handoff.
- Access hotfix `00005-zdk` passed login, 31 retained jobs and sign-out; the complete current feature bundle still needs hosted verification.

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

**Status:** Implemented and locally browser/workbook verified; hosted release validation pending.

- Header/Tax_Breakdown/Details expose exactly 13/3/6 editable fields with one joined transaction, leading-zero-safe identifiers and Decimal arithmetic.
- Download requires an explicit unvalidated acknowledgement and the filename, workbook properties and cell comments say `DRAFT_UNVALIDATED` and no receipt reservation.
- Draft creation does not read the active extraction payload, approve a reference, change job status/revision/invoice, write the approved export ledger or reserve receipt quantity.

## INV-14 · Search large source extracts without implying approval

As a **data steward**, I want to search actual item and PO sources by identifier or product name, so I can find evidence without loading unapproved rows into canonical matching.

**Status:** The 412,139-row private source profile is known, but hosted import is paused/cancelling and live lookup reports zero rows.

- Results retain source row and conflict/duplicate context, with filters and bounded pagination.
- The operator explicitly confirms a selected result before it can populate the manual workspace.
- Summary, search and confirmation state clearly that source evidence is not approved for matching, receipt allocation or export readiness.

## INV-15 · Move an owner subscription connection to the hosted workspace

As a **workspace owner**, I want to connect the restricted Claude CLI or move my ChatGPT registration into my authenticated hosted workspace, so the provider can use my authorized account without sharing a raw secret through the browser state.

**Status:** Connection and transfer controls are implemented in source; live subscription inference, release validation and hosted deployment pending.

- Claude setup tokens are encrypted, omitted from state, removable, and supplied only to the restricted server CLI with tools disabled.
- ChatGPT starts local OAuth under the application's own registration; successful export constructs a one-time bundle, deletes local encrypted credentials, disconnects and clears the active local account.
- Only an authenticated cloud owner can import and verify the bundle. Export is denied in cloud mode, import is denied locally, and no test result implies provider eligibility or live inference quality.
