# Fine-grained matching rules: gap table and implementation map

Sources: `ULTA_Invoice_Matching_Fine_Grained_Rules_v3` (sheets 00-13, 06A, 01A, 02A) and the owner's pasted
POGRN rules of 2026-10-05. The workbook has no `06B_POGRN_Rules` sheet. The owner's text gives the POGRN rules
as six titled sections without IDs; this table numbers them **R-025..R-030 in the order they were written**
(location ID, location type, market, quantity, value, final approval gate).

Baseline is `36ea6c5`. "Current" cites that commit. The engine now lives in `app/fine_rules.py`, the
lookup-catalog adapter in `app/fine_rules_source.py` and the review workbook in `app/fine_rules_export.py`.
Every rule has a test called `test_<RULE_ID>_...` in `tests/test_fine_rules.py`.

Status: done = already satisfied at baseline, partial, missing, conflict = sources disagree with each other, with
code or with an earlier owner answer (resolution stated, never silent).

## 01_Rulebook

| Rule | Rule (one line) | Current @36ea6c5 | Status | Planned change |
|---|---|---|---|---|
| R-001 | Capture file, supplier, number, date, lines, raw description unchanged; unreadable lines to exception | engines.py:118 `process` keeps text/boxes; no raw-intake record, no exception for unreadable pages | partial | `raw_invoice_rows` (06_Raw_Invoice) keeps raw values; unreadable/no-line invoice raises `Unreadable Invoice` exception |
| R-002 | Leading number in description = candidate VPN; keep description; several numbers: no auto-select without unique master match | none | missing | `vpn_candidates`: leading number is the primary candidate, other numbers kept as secondary; ambiguity resolved only by a unique master match |
| R-003 | 6-digit number in description/supplier field: search Item Master VPN and ITEM_PARENT; none/many to Item Exception | matching.py:70 compares canonical `sku` only | missing | VPN route searches VPN, then ITEM_PARENT; 0 or >1 rows = `Item Exception` |
| R-004 | Strip only `ULT_` from Item Master ITEM; keep leading zeros as text | matching.py:70 compares ITEM with `ULT_` kept (earlier no-strip approach) | conflict (code) | `strip_ult` removes only a leading `ULT_`, which also covers the 48 lower-case `ult_` rows. Nothing else is changed. The owner's later answer replaces no-strip |
| R-005 | Barcode exact, then VPN exact, then exact ITEM_PARENT; fuzzy is suggestion only | matching.py:70-72 item_id, then gtin, then sku | conflict (order) | Barcode, then VPN, then exact ITEM_PARENT, then description. Description is never auto-approved (V-009 threshold is open) |
| R-006 | Supplier name to Item Master SUPPLIER_NAME, then SUPPLIER code; several codes resolved by market/entity/currency | matching.py:47 canonical sites only | missing | `resolve_supplier`: normalized exact SUPPLIER_NAME lookup; >1 SUPPLIER value = `Supplier Exception`, narrowed only by the item match |
| R-007 | Brand = UDA_LV_1_VALUE | none | missing | Brand from `UDA_LV_1_VALUE`. **Data gap:** the source Item Master has UDA_LV_1_VALUE, but the live lookup catalog did not select that column. Brand stays blank with a data-quality warning until the integrator re-imports the catalog with it. BRAND is not substituted |
| R-008 | Supplier/site suffix (RA1, RB2, RA4, RE2) mapped through the maintained entity table; text alone is not enough | none | missing | `entity_hint` parses the suffix after the 6-char key; a hint only, never the market |
| R-009 | RA1=Kuwait, RB2=KSA, RA4=UAE; RE2 unconfirmed | none | missing | `ENTITY_MAP` holds the three confirmed rows; RE2 and other unmapped suffixes = `Entity Hint` review warning (V-001); never sets the market |
| R-010 | 8000-series locations: classify by Location Master, never hardcode | excel.py:37 requires a reference type | conflict | Conflicts with ALG-012/013 and the owner's 800=W / 380=S. Resolved as the owner wrote: master first, prefix as fallback and cross-check, disagreement = `Location Mapping` exception |
| R-011 | Return location code, name, type, country, market from POGRN + Location Master; missing master = exception | none | partial-blocked | Code from POGRN; type by master or prefix; name, country and market need the location master (V-007) and stay blank on the review list |
| R-012 | Currency from supplier-site-market map, never from ship-from country | matching.py:56 canonical routes | missing-blocked | `resolve_currency` reads only a supplied map; with no map, Currency is blank plus a `Currency Mapping` exception (V-010) |
| R-013 | Cross-border: market from receiving location/entity, not supplier country | none | missing-blocked | Market only from location to market map; supplier country never read |
| R-014 | UAE supplier in USD: manual review in MVP | none | missing | USD with no approved exception = `USD Review` exception |
| R-015 | Auto-match only when supplier + market + identifier give one record; else list candidates | matching.py:71 `len==1` | partial | Uniqueness gate after supplier constraint; all candidates listed in exception evidence |
| R-016 | Three output tabs only when mandatory fields pass; never approved with missing mandatory fields | excel.py:26 requires `ready` | partial | Fine-rules target workbook refuses unless every invoice is `Approved`; review workbook always available and labelled |
| R-017 | User correction to Feedback Log with evidence and approver; promote only after review | none | missing | `feedback_entry` + `POST /api/fine-rules/feedback`; status `Proposed` until approver/decision; never applied automatically |
| R-018 | Store original, transformed, rule ID, reference row, confidence for every transform | provenance in matching.py:19 for PO fields only | partial | Every populated output field gets a lineage record; missing lineage blocks approval |
| R-019 | PO missing: SUPPLIER_NAME to first 6 **digits of SUPPLIER** as EBS key | none | conflict | Conflicts with ALG-006 and the owner (first 6 **characters of SUPPLIER_NAME**, ABC001RA1KWD to ABC001). Live data: every POGRN EBS_SUPPLIER_CODE has the 6-char `AAA999` shape (one 4-char family), and for 387 of 413 distinct SUPPLIER_NAMEs the first 6 characters equal an EBS code; SUPPLIER is a 5-7 digit site ID. ALG-006 implemented; a 6-char key with no POGRN family (e.g. 4-letter-prefix names) or a key shorter than 6 = `Supplier Exception` |
| R-020 | Filter POGRN by EBS code; none = Missing PO | none | missing | `pogrn_candidates` filters on exact EBS_SUPPLIER_CODE |
| R-021 | Compare invoice quantity with POGRN quantity; mismatch never auto-approved | none | missing | See R-028 |
| R-022 | Compare invoice pre-tax value with POGRN pre-tax value; mismatch never auto-approved | none | missing | See R-029 |
| R-023 | Exactly one passing order: RMS order no is PO; several = Ambiguous PO | none | missing | See R-030 |
| R-024 | Recovered PO: PO source `Derived from POGRN` plus evidence; never overwrite an invoice PO without approval | none | missing | PO source + evidence row refs; a printed PO that differs from the derived one stays, with a `PO Conflict` exception |

## Owner POGRN rules (pasted 2026-10-05; IDs assigned by order, see top)

| Rule | Rule (one line) | Current | Status | Planned change |
|---|---|---|---|---|
| R-025 | Location = exact LOCATION of the candidate RMS order, as text; several locations not combined; blank = Location ID exception | none | missing | Aggregate per (EBS, order, location); an order spanning >1 location is not combined, so each group is evaluated separately and the order is flagged `Location ID` |
| R-026 | 800*=W, 380*=S; master file overrides; prefix stays as a check; disagreement = Location Mapping exception | excel.py:37 needs a reference type | missing | `location_type(loc, master)`. Live POGRN locations are all `380xx` or `800xxx`: no ID starts 38 without 380, so no owner form was needed |
| R-027 | Market from location to market map; supplier country is not the market; suffix only a check; none/many = Market Mapping exception | none | missing-blocked | Map not supplied: Market blank, `Market Mapping` exception. **Effect:** the R-030 gate cannot pass, so no PO is auto-approved until the map arrives. The unique candidate is still shown for review |
| R-028 | QTY_RECEIVED aggregated by (EBS, order, location) vs invoice qty at same level; variance tolerance 0 | none | missing | Exact Decimal sums; variance = invoice − POGRN; tolerance from config, default 0 |
| R-029 | TOTAL COST aggregated over the same rows vs invoice pre-tax value; currency must agree first; tolerance 0 | none | missing | Currency check (invoice vs POGRN CURRENCY_CODE) first, then value variance; mismatch blocks selection |
| R-030 | Approve only when supplier, order, location, type, market, qty, value pass and exactly one remains; else Missing PO / Ambiguous PO | none | missing | Gate implemented; 06A_POGRN_Validation sheet has every listed column |

## 01A_Algorithm_Rules

| Rule | Rule (one line) | Current | Status | Planned change |
|---|---|---|---|---|
| ALG-001 | Scan all pages; page count and text map; unreadable/missing page = exception | engines.py:118 keeps full text and boxes; models.py:21 caps line page at 20 | partial | `scan_pages` builds a per-page text map from boxes or form-feed text; empty or missing page in sequence = `OCR Review` exception |
| ALG-002 | Search the supplier name over the whole invoice, footer too; keep each candidate with page/position | layout_extract.py:122 header label patterns only | partial | `supplier_candidates` scans every page line against known SUPPLIER_NAMEs and labelled lines; 0 or >1 = `Supplier Exception` |
| ALG-003 | Document maps to Invoice Number unchanged; blank or duplicate = header exception | matching.py:46 duplicate check, exact | partial | Header Document = raw number, trimmed only; blank or duplicate in batch = `Header Exception` |
| ALG-004 | Document Date maps to Invoice Date; keep raw + parsed; ambiguous = date review | layout_extract.py:658 parses d/m silently | partial | Raw kept; `n/n/yyyy` with both parts ≤12 and different = `Date Review` (V-008), parsed date not used for approval |
| ALG-005 | Supplier Site = Item Master SUPPLIER (e.g. 10001), as text | canonical `site` only | missing | From the approved matched item rows; several values = exception |
| ALG-006 | EBS key = first 6 chars of SUPPLIER_NAME; search POGRN EBS_SUPPLIER_CODE | none | missing | `ebs_key`; see R-019 conflict |
| ALG-007 | Suffix is an entity/currency hint only | none | missing | `entity_hint` returns the suffix + mapped country; checked against the resolved market when one exists |
| ALG-008 | Printed PO validated against POGRN supplier, qty, value, location, date before use; invalid = recovery path | matching.py:57 canonical PO only | missing | Printed PO evaluated through the same gate; failing it falls to recovery (R-024 keeps it) |
| ALG-009 | PO missing: filter by EBS, then qty and value; unique candidate | none | missing | See R-020..R-023, R-028..R-030 |
| ALG-010 | Write RMS_ORDER_NO as Order No, source `Derived from POGRN`; never if >1 | none | missing | Header Order No only on approval |
| ALG-011 | Location from accepted POGRN order, never Item Master | Invoice.location from canonical PO | missing | Header Location only from the accepted group |
| ALG-012 | Location starting 800 = Warehouse (W), evaluated as text | none | missing | Prefix rule (R-026) |
| ALG-013 | Location starting 380 = Store (S), e.g. 38001 | none | conflict (earlier answer) | Owner earlier said `38xxx`; new rule says `380`. Implemented as `380` (newer, written source). Live data has no 38-but-not-380 IDs |
| ALG-014 | Location master overrides prefix; prefix kept as comparison; conflict = Location Mapping | none | missing-pending file | Master hook implemented and tested with a synthetic master (V-007) |
| ALG-015 | Barcode from UPC/barcode column first, then long numeric strings in description; strip spaces/hyphens only | Line.gtin read by extractors | partial | `barcode_candidates` |
| ALG-016 | Item ITEM minus `ULT_` exact-equals invoice barcode; >1 = exception, 0 = VPN route | none | missing | Barcode route |
| ALG-017 | VPN column first; else leading 6-digit number in description | Line.sku read by extractors | partial | `vpn_candidates` |
| ALG-018 | VPN exact against Item Master VPN, constrained by supplier where possible | none | missing | VPN route with supplier constraint |
| ALG-019 | Description normalization (case, punctuation, units; drop extracted IDs; keep brand/size/shade) | product_candidates.py:29 numeric similarity only | missing | `normalize_description` + token score; description-only candidates go to review |
| ALG-020 | Triple assurance: barcode, VPN, description; description never overrides an exact identifier; conflict = Item Conflict | matching.py:76-77 sku/gtin conflict | partial | Barcode Check / VPN Check / Description Check columns; barcode-vs-VPN disagreement = `Item Conflict` |
| ALG-021 | Item column = ITEM_PARENT of the approved row; never the `ULT_` value; blank = blocking exception | excel.py:49 writes match `item`; UPC may get `ULT_…` from reference gtin | partial (bug) | Target Item = ITEM_PARENT. excel.py also strips `ULT_` from UPC |
| ALG-022 | Brand from UDA_LV_1_VALUE; missing = data-quality warning | none | missing | See R-007 data gap |
| ALG-023 | Currency from supplier-site + market map; not from country | none | missing-blocked | See R-012 |
| ALG-024 | USD only with explicit supplier-site exception | none | missing | `usd_exceptions` config; otherwise `USD Review` |
| ALG-025 | Cross-border: market from receiving POGRN location; currency from supplier-site-market config | none | missing-blocked | See R-013/R-027 |
| ALG-026 | One transaction row per accepted line; Item=ITEM_PARENT, UPC, Unit Cost, Quantity, Unit Tax Code; line exceptions do not block other lines | excel.py:46 | partial | Line rows per line with per-line status |
| ALG-027 | UPC = invoice/normalized UPC as text, leading zeros kept | excel.py:49 text cell | partial | Normalized invoice barcode; never `ULT_` |
| ALG-028 | Every invoice runs the full chain; isolated failures do not fail the batch | per-job only | missing | `run_batch` catches per invoice, each gets a status |
| ALG-029 | Auto-approve only one unique item and one unique PO | matching.py:71 | partial | Single gate in item and PO paths |
| ALG-030 | Never invent supplier, PO, location, currency, barcode, VPN, item, brand; record missing/conflicts | partial throughout | partial | Blank + exception; lineage required for every populated value (R-018) |

## 02A_Target_Mapping and working sheets

| Rule | Rule (one line) | Current | Status | Planned change |
|---|---|---|---|---|
| 02A-Document … 02A-Currency (12 rows) | Each target column's source, fallback and validation | excel.py:8 HEADERS | partial | `target_mapping` builds each column exactly as listed; test per row |
| 09_Output_Header | Document, Supplier Site, Order No, Location, Location Type, Document Date, Currency, Gross, Tax, Net, Market, Validation Status | excel.py Header has 13 template columns, no Currency/Market | conflict | Target `Header` keeps the 13 template columns (prior owner answer: template unchanged). The 12 columns of sheet 09 go in the review workbook |
| 10_Output_Lines | 6 template columns + audit columns + POGRN columns | excel.py Details 6 columns | done/extend | Target `Details` stays at 6 columns; the 16 columns of sheet 10 go in the review workbook |
| 06/07/08/11/12/06A sheets | Working-sheet layouts | none | missing | Review workbook has these sheets with the exact headers |
| 03_Entity_Map, 05_Location_Master | Reference rows | none | missing | 03 rows in `ENTITY_MAP`; 05 rows are the prefix fallback |

## 13_Validation_Log: open decisions (not rules; they block parts of rules above)

V-001 RE2 mapping (R-009) · V-002 6-digit always VPN? (R-003) · V-003 target mandatory columns (R-016) ·
V-004 cross-border combinations (R-013) · V-005 USD exceptions (R-014, deferred) · V-006 tie-break (R-015) ·
V-007 POGRN location file (ALG-014, R-011, R-027) · V-008 date convention (ALG-004) · V-009 description
threshold (ALG-019) · V-010 supplier-site-market-currency list (R-012, ALG-023). Until each is closed, the
engine routes the affected case to review rather than guessing.

## Implementation (branch feat/fine-grained-rules)

Code: `app/fine_rules.py` (engine), `app/fine_rules_source.py` (read-only lookup-catalog adapter; every term hit is re-checked against the original column; >50,000 rows refuses), `app/fine_rules_export.py` (review workbook with sheets 06/06A/07/08/09/10/11/12 + Lineage; target workbook with the unchanged 13/3/6 template columns, refused unless every invoice is `Approved`).
Existing engine: `app/matching.py` compares barcodes after `strip_ult` on both sides; `app/excel.py` never writes `ULT_` into UPC.

API: `GET/POST /api/fine-rules/config` (location master, location to market, supplier-site currency, USD exceptions, tolerances; validated, audited), `POST /api/fine-rules/run`, `POST /api/fine-rules/review.xlsx`, `POST /api/fine-rules/target.xlsx` (409 unless all Approved), `GET/POST /api/fine-rules/feedback`, `POST /api/fine-rules/feedback/{id}/decision`.

Tests: `tests/test_fine_rules.py` has one test per rule ID (`test_R_001_…` to `test_R_030_…`, `test_ALG_001_…` to `test_ALG_030_…`, `test_02A_target_mapping[02A-<column>]` for the 12 02A rows, `test_02A_unit_cost_line_value_reconciliation`, `test_working_sheets_have_exact_rulebook_headers`). `tests/test_fine_rules_api.py` runs the rules over an imported synthetic catalog using the deployed manifest's roles, plus the API. All fixtures are synthetic.

### Blocked on business input (implemented with exceptions, cannot pass yet)

| Item | Effect until supplied | Unblocks |
|---|---|---|
| V-007 location to market map (R-011, R-027, R-030) | Market blank; no PO passes the R-030 gate, so nothing is `Approved`; the unique candidate is listed in the `Missing PO` exception | `POST /api/fine-rules/config` `location_master`/`location_market` |
| V-010 supplier-site + market currency list (R-012, ALG-023/024/025) | Currency blank, `Currency Mapping` exception | `supplier_site_currency`, `usd_exceptions` |
| R-007 / ALG-022 Brand | Live catalog did not import `UDA_LV_1_VALUE`; Brand blank with a non-blocking `Data Quality` warning | Integrator re-imports the Item Master with `UDA_LV_1_VALUE` in the manifest |
| V-001 RE2 entity | `Entity Hint` warning | Add the row to `entity_map` |
| V-008 ambiguous dates | `Date Review` exception | Business date-format rule per supplier |
| V-009 description threshold | Description route is review-only | Approved threshold |
| R-016 tax mapping | `Unit Tax Code` only from the reviewed invoice tax code; blank = `Tax Code` exception | Approved ULTA tax mapping |

## Website panel (branch feat/fine-rules-ui)

In the review workspace, select processed invoices in the inbox and click **Run ULTA rules**. The dialog
calls `POST /api/fine-rules/run` and shows, per invoice:

- the status (Approved / Review / Blocked) and the count of blocking exceptions;
- header facts (Supplier Site, Order No, PO source, Location, Location Type, Market, Currency, …). A value the
  rules could not establish is shown as an empty hatched cell, never as invented text;
- the exceptions with rule ID, line, evidence, proposed resolution and whether each one blocks;
- the PO/GRN candidates (06A) with location, market, qty/value match, checks passed and the evidence rows;
- the item matches (barcode, VPN, ITEM_PARENT, ITEM, match method, rule ID and the three checks).

**Download review workbook** always works (`review.xlsx`). **Download target workbook** is enabled only when
every selected invoice is Approved; otherwise the dialog lists the invoices that are not and why (the server
refuses the same request with 409). While the location→market list (V-007) or the supplier-site currency list
(V-010) is missing from `/api/fine-rules/config`, a banner says so: no PO can be confirmed, so invoices stay in
Review.
