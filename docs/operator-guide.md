# Operator guide: from invoice to checked workbook

**Owner roles proposed:** AP process owner and reference-data steward. **Review cadence:** monthly during pilot, then quarterly. Individual owners must be assigned before business rollout.

## Before the first batch

Open the hosted portal and sign in with the approved Firebase account, or run the local application. The deployed hosted workspace is the previously verified baseline and starts with synthetic references and clearly named test invoices. Real-document extraction is available, but dummy references cannot validate real invoice facts. The manual-draft, source-lookup and subscription-transfer changes described below are in the current source and still require release validation and a hosted deployment.

The data steward prepares a reference snapshot with approved supplier sites, routes, items, purchase orders, receipt balances and tax rules. Import it in References and check counts and version. A target invoice example cannot substitute for this snapshot. Keep an untouched copy of every source and its profile. The separate lookup index can search the actual 114,940 item rows and 297,199 PO rows by identifier or product-name token, but those results are source evidence only. Review conflicts and explicitly confirm a selection before copying it into a draft; never treat lookup confirmation as canonical approval.

The AP owner agrees price and total tolerances and the currency precision. Start with exact price matching; broaden a tolerance only for a documented business reason. Connect an AI provider if wanted, retrieve its model list and choose the model. OpenAI and Anthropic use API keys. The server-side Claude CLI can use an encrypted setup token where the CLI is installed. ChatGPT begins with local OAuth under the application's own registration, then an explicit export/import moves the credential to the authenticated hosted owner workspace. Live subscription access and inference must be checked with the owner's account; automated adapter tests do not prove eligibility.

## Daily process

1. **Select invoices.** Add one invoice per file. Split PDFs containing several invoices. Confirm that scans are legible and page order is correct.
2. **Review the plan.** Check filenames, reader, language, fallback, provider/model, reference version and tolerances. Confirm to start. Cancel makes no extraction request.
3. **Watch the stages.** Each invoice progresses independently. The selected job shows stages such as template matching, native PDF text, OCR/document conversion, AI fallback and validation. For a readable PDF with no supplier template, automatic mode keeps native text and skips expensive image OCR. Missing credentials or provider failure creates a visible exception.
4. **Inspect the evidence.** Open each result. Check supplier, invoice number/date, PO and every line against the document. Completeness is not proof of accuracy. For office files that cannot preview in the browser, use the original download and extracted text.
5. **Resolve holds.** Correct a misread field, or ask the data steward to repair missing reference evidence. Save edits and revalidate. Do not change a master record simply to make an invoice pass.
6. **Confirm review.** This acknowledges comparison with source evidence. The backend still checks every rule; confirmation does not bypass a hold.
7. **Export approved work.** Select ready invoices and download one workbook. Check invoice count and the shared transaction numbers across all three sheets. This route revalidates references and reserves receipt quantities atomically.
8. **Use a manual draft only when required.** The manual editor exposes all 13 Header, 3 Tax_Breakdown and 6 Details fields and works even while OCR is queued or processing. Check every value and arithmetic, acknowledge that it is unvalidated, then download. The filename, workbook properties and cell comments say `DRAFT_UNVALIDATED`; it does not validate references, reserve a receipt, approve the invoice or change the extraction job. Do not send it into the approved import path without the receiving owner's separate procedure.
9. **Retain evidence.** Keep source files, reference versions, reviews and approved export receipts together. Retain the manual-draft audit separately from the approved ledger. Local mode uses `.data`; hosted mode uses Cloud SQL and private Cloud Storage. Back up database, source evidence and the corresponding encryption-key version together. Drain active work before a deployment; a database backup-restoration exercise is still required before business production.

## Owner connection transfer

For Claude, generate a setup token through the account's supported flow and paste it only into the protected connection form. The server encrypts it and supplies it to the restricted Claude CLI for the request; invoice tools are disabled and only the required extracted text is provided. Delete the connection to remove the saved token.

For ChatGPT, sign in from the local application, select the connected account, and export the one-time credential bundle. A successful bundle construction removes the local encrypted credential, marks the local account disconnected and clears the active selection. Protect the downloaded file, import it promptly while signed in to the hosted owner workspace, then delete it from disk and downloads. If transfer fails after export, reconnect locally and repeat. Do not copy the bundle to chat, email, source control or shared storage.

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
| OCR is slow but an immediate workbook is required | Use the all-field manual editor and download only the clearly marked `DRAFT_UNVALIDATED` file. It does not consume receipts or make the job ready. |
| Lookup shows duplicate or conflicting products/POs | Compare source context, refine the search and explicitly confirm only supported evidence. Keep the invoice unapproved until canonical reference owners resolve the conflict. |
| ChatGPT transfer download is lost | The local credential was detached after bundle construction. Reconnect locally and create a new transfer; never try to reconstruct token values. |
| Reference/rule changes during extraction | Review the new plan and retry; the old confirmation does not authorize new rules. |
| Restart during reading | Job shows an interrupted status. Review the plan and retry; existing exports remain intact. |
| Credit, multiple POs or mixed taxes | Keep for the agreed exception process until the product has an approved contract for that case. |

## Pilot measures

Targets below are proposed acceptance gates, not measured results.

| Measure | Proposed gate | Method |
|---|---|---|
| Export validity | Zero known false-ready approved invoices in the adjudicated pilot | Independently compare every approved export field and allocation; report manual drafts separately |
| Target compatibility | All agreed downstream import tests pass | Load an accepted synthetic and controlled business test batch |
| Manual effort | Establish baseline, then demonstrate reduction | Record minutes per invoice before and after, including exceptions |
| Reader quality | Report by supplier, language and document type | Header exact match, line recall, numeric accuracy and correction count |
| AI dependence | Visible and within agreed budget | Fallback rate, tokens, latency and failure rates |
| Traceability | Every export reconstructable | Source hash, reference version, edits, policy and allocations |

Do not use synthetic test success as a real-world OCR accuracy estimate. Review representative difficult scans, Arabic/English mixes, leading zeros, repeated item codes, partial receipts and returns before widening scope.
