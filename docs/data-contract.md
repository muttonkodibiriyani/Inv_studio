# Reference data and Excel contract

## Keep three types of data separate

An **invoice** states what the supplier is charging. **Reference data** identifies approved suppliers, items, orders and accepted receipts. The **target workbook** is the output format. A completed target workbook is valuable for checking columns and types, but it does not prove supplier approval, a purchase order balance or receipt acceptance.

Identifiers are stored as strings, preserving leading zeros. Strip surrounding Unicode whitespace; do not remove internal characters or coerce identifiers through floating-point numbers. Money and quantities use decimal arithmetic. Dates must resolve to ISO `YYYY-MM-DD` before export. Ambiguous dates need review.

## Canonical reference import

Download the populated synthetic template, replace its contents with approved records, and retain its exact headers. JSON equivalents use the keys below. ODS uses the same seven sheet names. Imports validate joins and duplicate keys before replacing the active snapshot. Changing a snapshot invalidates all unexported reviews and pending processing plans.

| Sheet / JSON key | Required columns | Meaning |
|---|---|---|
| Suppliers / sites | id, seller, name, country | Operational site ID, supplier legal entity ID, approved invoice-facing name, country |
| Routes / routes | site, buyer, origin, market, currency, status | Full commercial route; only `approved` is usable |
| Items / items | id, site, sku, gtin, uom | Internal item ID, supplier-site item code, optional barcode, purchasing unit |
| POs / orders | id, site, buyer, origin, market, currency, location, location_type, status | Exact PO scope; status `open`; location type `Store (S)` or `Warehouse (W)` |
| POLines / nested orders.lines | po, id, item, ordered, invoiced, price, uom | Ordered quantity; external baseline already invoiced; approved unit cost |
| Receipts / receipts | id, po, line, qty, status | Receipt line, exact PO line; status accepted/pending/rejected |
| TaxRules / taxRules | origin, market, currency, code, rate | Explicit approved tax treatment; rate is a fraction such as 0.05 |

In JSON, PO lines are nested under each order and omit `po`. Supplier `aliases` may be a list of approved alternative names. A supplier name mismatch is a hold; the name is not silently ignored because an AI model found a PO number.

An accepted negative receipt represents a return. Available receipt balance = sum of accepted receipt quantities − external already-invoiced baseline − quantities allocated by this installation. The baseline must exclude this installation's exports to avoid double-counting. This model assumes an approved snapshot and does not invent a settlement history from receipt rows.

PO location type is explicit. Prefixes such as 900 do not prove warehouse versus store. The printed buyer name is a source fact and is stored separately from the internal buyer/company code. A name or address cannot populate that code without approved reference evidence. Same-name suppliers in different companies or sites are not interchangeable. Currency equality does not prove domestic supply. Invoice-line `item_id` means the approved internal item only; AI leaves it null, and the supplier SKU never falls back into it. An operator may enter it manually or confirm it from reference evidence. A missing GTIN can remain blank when an exact supplier-site SKU maps uniquely to an internal item; conflicting SKU/GTIN identity is held.

## Large source extracts

Keep original files immutable, compute a SHA-256 hash, inspect every populated cell and build a profile with source sheet/row/column provenance. Map fields into a staging model before proposing a canonical snapshot. Report duplicates, one-to-many joins, missing internal scope, formula/cached-value differences and date/identifier conversion risks.

Do not resolve conflicting item records with “first row wins.” Fuzzy product-name search may use price, quantity and unit as ranking clues, but the operator must inspect source provenance and explicitly confirm a candidate. A rank never approves an item. Do not treat repeated receipt rows as new goods without proving the row grain. Do not substitute ordered quantity for accepted quantity. A source value called received or shipped still needs its business definition confirmed. Missing company, approval, currency or tax values stay unresolved until the data owner supplies the rule.

The portal's canonical import is deliberately bounded (12 MB compressed, 30 MB expanded, 50,000 rows per worksheet). Large raw extracts are handled by separate private staging/profiling. They are not silently truncated or uploaded to AI. A scalable source adapter is a distinct integration step with its own approval and regression tests.

## Exact output

| Sheet | Columns, in order |
|---|---|
| Header | TransactionNumber, Document, SupplierSite, OrderNo, Location, LocationType, DocumentDate, TotalCostExTax, TaxAmount, Ref1, Ref2, Ref3, Comment |
| Tax_Breakdown | TransactionNumber, TaxCode, TaxBasis |
| Details | TransactionNumber, Item, UPC, UnitCost, Quantity, UnitTaxCode |

TransactionNumber is a workbook-local integer starting at 1. It is the join key, not the supplier's invoice number. Document stays text to preserve leading zeros; UPC stays text to preserve long barcodes. IDs without leading zeros and with at most 15 digits may be numeric to match the target examples; otherwise they remain text. Dates are real Excel dates, and costs/quantities are numeric cells. There are no helper columns or generated formulas. Ref1/2/3 and Comment are empty unless a future approved mapping defines them.

The current contract supports one PO, one currency and one tax code per invoice. A tax rule supplies the rate; the application checks tax against rounded net × rate. Tolerances are explicitly configured and confirmed. Currency decimal places are explicit; KWD uses three in the default policy. Mixed taxes, freight, discounts and credits require an extension rather than an invented allocation.

## Readiness and traceability

All required identities, route and PO scope, items, units, unit prices, accepted quantity balances, arithmetic and tax must pass. The operator must confirm evidence review. Review alone cannot override failed matching. Export revalidates against the latest ledger inside a database transaction, preventing two concurrent invoices from consuming the same available quantity.

Every export records the reference version, policy and line allocations. Corrections and reader attempts are audited. A changed review revision is rejected rather than overwriting another edit. Previously exported invoices download their existing result; they are not reposted by repeated clicks.

Permanent deletion removes the live source, job payload, detailed invoice audit and downloadable workbook only after explicit confirmation and revision checks. For an approved invoice, the service retains a minimal tombstone containing the invoice key and PO-line quantity allocations so duplicate and receipt controls remain correct, plus a minimal deletion audit. Provider backups, logs, object versions and soft-delete copies are governed by their separate retention policies.
