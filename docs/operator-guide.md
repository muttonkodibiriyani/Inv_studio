# Operator guide: from invoice to checked workbook

**Owner roles proposed:** AP process owner and reference-data steward. **Review cadence:** monthly during pilot, then quarterly. Individual owners must be assigned before business rollout.

## Before the first batch

Open the hosted portal and sign in with the approved Firebase account, or run the local application. Final revision `inv-studio-api-00009-vxw` has 100% traffic; its extraction, lookup and control checks passed. The allowlist is owner-only, the owner account remained email-verified and unauthenticated API requests are denied. Real-document extraction and source lookup do not make reference records valid for approval.

The data steward prepares a reference snapshot with approved supplier sites, routes, items, purchase orders, receipt balances and tax rules. Import it in References and check counts, hashes, version and recovery receipt. A target invoice example cannot substitute for this snapshot. Keep an untouched copy of every source and its profile. The private evidence catalog completed atomic cutover with 412,139 live rows—114,940 item and 297,199 PO/GRN—and a sanitized receipt beginning `d06678`. Authenticated API QA returned the expected rank-one exact-name and typo candidates, the expected PO result, no result for an impossible strict filter, and retained confirmation flags. Catalog results stay evidence only until supplier/site mappings, accepted-receipt meaning and prior-invoicing baseline are approved.

The AP owner agrees price and total tolerances and the currency precision. Start with exact price matching; broaden a tolerance only for a documented business reason. Connect an AI provider if wanted, retrieve its model list and choose the model. A key shown as saved has only been encrypted and stored; successful model retrieval or a provider probe verifies access. If there is no selected connected model, the UI may offer an available model from a connected provider. It does not silently replace a user's still-connected model. The hosted workspace can offer managed Google Cloud AI through its service identity; usage is charged to the configured Google Cloud project and still requires processing-plan confirmation. OpenAI and Anthropic use API keys. The server-side Claude CLI can use an encrypted setup token where the CLI is installed. ChatGPT begins with local OAuth under the application's own registration, then an explicit export/import moves the credential to the authenticated hosted owner workspace. Live subscription access and inference must be checked with the owner's account; automated adapter tests do not prove eligibility.

## Daily process

1. **Select invoices.** Add several files together, with one invoice per file. A readable PDF that binds several invoices back to back (each starting on its own page with its own invoice number) is split for you at upload: each part becomes its own job named `<file> [n of N <invoice number>].pdf`, and the record states which pages of the original it came from. A file whose pages mix two invoice numbers is refused with both numbers named; split it by hand and upload the parts. Scanned PDFs without a text layer are never split. The workspace runs up to two jobs at once and leaves the rest visibly queued.
2. **Review the plan.** Check filenames, reader, language, fallback, provider/model, reference version and tolerances. Confirm to start. Cancel makes no extraction request.
3. **Watch the stages.** Each invoice progresses independently. The selected job shows stages such as template matching, native PDF text, conservative layout extraction, OCR/document conversion, AI fallback and validation. On a scan, the invoice2data route can use Paddle for image text and says so in the trace. For a readable PDF with no supplier template, automatic mode keeps native text and skips expensive image OCR. Native rules require a strong invoice heading, reject purchase orders, extract only explicit labels/spatial rows and leave ambiguous or internal business fields empty. The trace distinguishes an engine failure from a completed text-only read with zero structured items. Missing credentials or provider failure creates a visible exception.
4. **Inspect the evidence.** Use **Select processed** to work through completed records. Open each result, check supplier, printed buyer name, internal buyer/company code, invoice number/date, PO and every line against the document, and save before moving to another invoice. A printed company name never proves an internal buyer code. Unsaved changes stay in the form and block batch download; there is no hidden autosave. Field completeness is read coverage, not proof of accuracy.
5. **Resolve holds.** Correct a misread field, or ask the data steward to repair missing reference evidence. Product-name search can rank candidates with exact identifiers plus price, quantity and unit clues; inspect the source row and confirm the item yourself. The line's internal Item value comes only from manual entry or confirmed reference `item_id`; AI leaves it empty and a supplier SKU never substitutes for it. Save edits and revalidate. Do not change a master record simply to make an invoice pass.
6. **Confirm review.** This acknowledges comparison with source evidence. The backend still checks every rule; confirmation does not bypass a hold.
7. **Choose the correct combined workbook.** **Download review workbook** combines saved processed records and leaves unknown codes blank. It is marked `EXTRACTION_REVIEW_ONLY` and changes no job, approval, ledger or receipt allocation. **Export approved** accepts only selected ready invoices, revalidates references and reserves receipt quantities atomically. Never treat the review workbook as the approved import.
8. **Use a manual draft only when required.** The manual editor exposes all 13 Header, 3 Tax_Breakdown and 6 Details fields and works even while OCR is queued or processing. Check every value and arithmetic, acknowledge that it is unvalidated, then download. The file says `DRAFT_UNVALIDATED`; it does not validate references, reserve a receipt, approve the invoice or change the extraction job.
9. **Retain evidence.** Keep source files, reference versions, reviews and approved export receipts together. Retain the manual-draft audit separately from the approved ledger. Local mode uses `.data`; hosted mode uses Cloud SQL and private Cloud Storage. Back up database, source evidence and the corresponding encryption-key version together. Drain active work before a deployment; a database backup-restoration exercise is still required before business production.
10. **Delete only with the retention boundary understood.** Permanent deletion requires explicit confirmation and current job revisions. It removes live source objects, job payloads, detailed audit evidence and downloadable workbooks. For an approved invoice, the service keeps only the invoice key and PO-line quantity allocations needed for duplicate/receipt controls, plus a minimal deletion audit. Provider backups, logs, object versions and soft-delete copies follow the provider's retention schedule and are not erased by this action.

The audit distinguishes `Text read`, `Invoice fields extracted` and `Extraction failed`. A historical `extracted` event is displayed as `Reader run finished (legacy event)` and must not be interpreted as proof that fields or lines were extracted.

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
| Printed buyer name differs from internal company code | Preserve the printed name as evidence and select the internal code only from approved reference data. Do not copy the name into the code field. |
| Reader fails | Inspect its field/item/text counts and method. Paddle and Docling are selectable paths, not universal guarantees. Change reader or enable permitted AI, then confirm a new plan. |
| AI connection fails or limits are reached | Reconnect/select a model, retry later or enter verified values manually. The invoice stays on hold. |
| OCR is slow but an immediate workbook is required | Use the all-field manual editor and download only the clearly marked `DRAFT_UNVALIDATED` file. It does not consume receipts or make the job ready. |
| Lookup shows duplicate or conflicting products/POs | Compare source context, matching basis, price/quantity/unit clues and identifiers. Explicitly confirm only supported evidence; fuzzy rank is a suggestion, not approval. Keep the invoice unapproved until canonical reference owners resolve the conflict. |
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
| Header extraction | Target ≥90% exact accuracy; not yet proven | All frozen gold header cells, including expected nulls; errors/timeouts score zero |
| Line extraction | Target ≥90% exact line-cell accuracy; not yet proven | Align to adjudicated source order; report line recall plus exact item/description/qty/unit/price cells |
| Zero-correction invoices | Target ≥90%; not yet proven | Invoices needing no extraction correction divided by all attempts, including failures/timeouts |
| AI dependence | Visible and within agreed budget | Fallback rate, tokens, latency and failure rates |
| Traceability | Every export reconstructable | Source hash, reference version, edits, policy and allocations |

Freeze and adjudicate the gold set before tuning. Stratify by supplier, native/scan, language, document quality and page length, and publish subgroup results so an aggregate cannot hide a weak cohort. The three-scan Paddle result has a conservative 90.91% floor with descriptions treated unproven. On a separate challenging two-page scan, the bounded adaptive path improved the whole local result from 90/99 (90.9%) to 96/99 (97.0%) while retaining 23/23 rows and 6/7 headers; it took about 168 seconds across both passes and peaked at 2.31 GiB. Revision `00007` retained 90/99 in the hosted run because its 134.082-second baseline exceeded that revision's 100-second eligibility gate. Revision `00008` is live with Paddle's 360-second limit and 150-second baseline eligibility ceiling; its hosted Paddle run completed the adaptive path in 242.98 seconds and returned 23/23 rows and 96/99 facts (97.0%). Docling remains at 240 seconds. The alternate runs only for eligible short PDFs with missing baseline quantity/price, keeps the baseline on early failure or any row/header regression, and did not trigger on the three earlier scans. Heavy local OCR work is serialized while native/AI work can continue. A separate hosted managed-AI check returned 23 rows and 97/99 facts in 16.18 seconds, preserved the printed buyer name and kept the internal buyer code null. Docling recovered bounded native tables and 6/6 rows across three earlier scans after adapter fixes, but field presence is not accuracy: it produced only 10/23 rows with poor source-position matches on the challenging two-page scan and timed out on ten pages. Native PDF and earlier managed-AI checks were exact on the source-tested fields of one layout family. These results do not prove the expected 95% across the pilot population.
