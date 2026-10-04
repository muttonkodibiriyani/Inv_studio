# Operator guide: from invoice to checked workbook

**Owner roles proposed:** AP process owner and reference-data steward. **Review cadence:** monthly during pilot, then quarterly. Individual owners must be assigned before business rollout.

## Before the first batch

The data steward prepares a reference snapshot with approved supplier sites, routes, items, purchase orders, receipt balances and tax rules. Import it in References and check counts and version. A target invoice example cannot substitute for this snapshot. Keep an untouched copy of every source and its profile.

The AP owner agrees price and total tolerances and the currency precision. Start with exact price matching; broaden a tolerance only for a documented business reason. Connect an AI provider if wanted, retrieve its model list and choose the model. API access and subscription adapters are separate connection choices.

## Daily process

1. **Select invoices.** Add one invoice per file. Split PDFs containing several invoices. Confirm that scans are legible and page order is correct.
2. **Review the plan.** Check filenames, reader, language, fallback, provider/model, reference version and tolerances. Confirm to start. Cancel makes no extraction request.
3. **Watch the queue.** Each invoice progresses independently. A failed reader can fall through to another engine or the selected AI connection. Missing credentials or provider failure creates a visible exception.
4. **Inspect the evidence.** Open each result. Check supplier, invoice number/date, PO and every line against the document. Completeness is not proof of accuracy. For office files that cannot preview in the browser, use the original download and extracted text.
5. **Resolve holds.** Correct a misread field, or ask the data steward to repair missing reference evidence. Save edits and revalidate. Do not change a master record simply to make an invoice pass.
6. **Confirm review.** This acknowledges comparison with source evidence. The backend still checks every rule; confirmation does not bypass a hold.
7. **Export.** Select ready invoices and download one workbook. Check invoice count and the shared transaction numbers across all three sheets. Hand it to the receiving process under the agreed acceptance procedure.
8. **Retain evidence.** Keep source files, reference versions, reviews and export receipts together. The local installation retains these in `.data`; back up the database and encryption key securely as a pair.

## Responsibilities

| Activity | Responsible | Accountable | Consulted | Informed |
|---|---|---|---|---|
| Reference mapping and scope | Data steward | Procurement/data owner | AP, receiving | Operator |
| Invoice evidence review | AP operator | AP owner | Supplier liaison | Finance |
| Receipt or quantity dispute | Receiving team | Receiving owner | AP, procurement | Supplier liaison |
| Tax rule definition | Finance/tax owner | Finance owner | AP | Data steward |
| Provider/model configuration | Application administrator | Product owner | Security/data owner | Operators |
| Release and backup recovery | Engineering/platform | Service owner | AP owner | Operators |

## Exceptions

| What you see | What to do |
|---|---|
| Unknown supplier/site or name conflict | Validate the source identity and approved aliases. Keep on hold until resolved. |
| Same supplier in a different market | Confirm the exact route and buying company; an existing domestic approval is insufficient. |
| Missing PO or multiple possible item matches | Supply source evidence or repair the reference mapping; never choose the first candidate by position. |
| Missing receipt or over-billing | Ask receiving to confirm accepted goods and returns. Ordered quantity is not proof of receipt. |
| Price, unit or tax mismatch | Resolve commercially or add an explicitly approved contract extension. Do not invent unit conversion or tax allocation. |
| Duplicate invoice | Review existing export/history; repeated clicks return the prior export. |
| Reader fails | Inspect the failure trace, change reader or enable AI, then review and confirm a new plan. |
| AI connection fails or limits are reached | Reconnect/select a model, retry later or enter verified values manually. The invoice stays on hold. |
| Reference/rule changes during extraction | Review the new plan and retry; the old confirmation does not authorize new rules. |
| Restart during reading | Job shows an interrupted status. Review the plan and retry; existing exports remain intact. |
| Credit, multiple POs or mixed taxes | Keep for the agreed exception process until the product has an approved contract for that case. |

## Pilot measures

Targets below are proposed acceptance gates, not measured results.

| Measure | Proposed gate | Method |
|---|---|---|
| Export validity | Zero known false-ready invoices in the adjudicated pilot | Independently compare every exported field and allocation |
| Target compatibility | All agreed downstream import tests pass | Load an accepted synthetic and controlled business test batch |
| Manual effort | Establish baseline, then demonstrate reduction | Record minutes per invoice before and after, including exceptions |
| Reader quality | Report by supplier, language and document type | Header exact match, line recall, numeric accuracy and correction count |
| AI dependence | Visible and within agreed budget | Fallback rate, tokens, latency and failure rates |
| Traceability | Every export reconstructable | Source hash, reference version, edits, policy and allocations |

Do not use synthetic test success as a real-world OCR accuracy estimate. Review representative difficult scans, Arabic/English mixes, leading zeros, repeated item codes, partial receipts and returns before widening scope.
