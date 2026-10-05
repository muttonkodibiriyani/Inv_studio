# Learning from verified answers

Inv Studio learns from invoices the owner has verified. The learned data is built from real
invoices, so it lives in a private store and never in git. Learning never takes over from the
built-in readers: it fills gaps, overlays single fields it has proven on the supplier, and gives the
AI fallback verified examples of the same supplier.

## What counts as verified

| Source | Trigger | What it teaches |
| --- | --- | --- |
| Exported invoice | `POST /api/jobs/{id}/export` and `/api/jobs/export` (batch) | The supplier's template and an AI example |
| Approved correction | Fine-rules feedback decided `Approved` (`/api/fine-rules/feedback/{id}/decision`) | A correction line in the AI prompt for that supplier |
| Manual replay | `POST /api/learned/jobs/{id}` (exported jobs only) | Same as export, for jobs exported before learning was switched on |

A rejected correction is retracted. Deleting jobs forgets what was learned from them: the
supplier's template is deleted when its source invoice goes, and re-learned from the newest
remaining sample. `DELETE /api/learned/{supplier_key}` forgets a supplier entirely, corrections included.

## How a template is learned (`app/learned.py`)

`learn_template(text, invoice)` turns one verified invoice and its extracted text into an
invoice2data template:

1. **Header fields** (number, date, currency, net, tax, po). Each verified value is located in the
   text and anchored to the printed label on the same line or the line above. A field only gets a
   regex if that regex finds exactly the verified value, nothing else.
2. **Line rows.** Rows are located by their numeric columns (qty, price, net amount, tax amount),
   assigned right to left so a price equal to the amount cannot shift the columns. The row regex is
   merged from all rows, so an optional brand token or a GTIN label on some rows does not break it.
3. **Continuations.** Wrapped descriptions, barcode lines and stray column values are learned from
   what the verified description kept and dropped. Page-break noise becomes `skip_line` rules.
4. **Reproduction.** The template is kept only if applying it to the same text reproduces the
   verified invoice (every header field and every row exact or near). A template that cannot
   reproduce its own source is reported with the reason and not stored.
5. **Fixes.** The template records which fields the built-in reading got wrong on this invoice
   and the template gets right. Only those fields may be overlaid later.

Template files are plain invoice2data YAML with a `learned:` block (version, supplier key, source
job id, continuation rules, fidelity, fixes). Invoice2data ignores the block.

## Precedence in the worker (`app/ocr_worker.py`, `apply_learned`)

The built-in chain runs first exactly as before (`template -> columnar_tax_invoice -> table ->
layout -> text_only`). Then, if a learned template matches the text:

| Built-in result | Learned action | `learned.mode` |
| --- | --- | --- |
| No lines | Learned reading is used | `read` |
| Same rows and same qty/price/amount arithmetic | Only fields listed in `fixes` that differ are overlaid; otherwise untouched | `overlay` or none |
| Different arithmetic | Learned reading replaces it only if it reconciles with the header and the built-in one does not | `replace` or none |

An invoice the learned store does not touch is byte-identical to the result without the store.
The method becomes `<method>+learned` on an overlay and `learned_template` on a read or replace.
`--learned-only` reads with learned templates alone (benchmark ablation, not used by the app).

## AI examples (`Providers.prompt`)

When the AI fallback runs, up to three verified invoices of the same supplier (matched by the
stored keywords) are appended to the prompt as compact JSON: header fields, row count and the first
two lines. Approved corrections for that supplier follow. The block tells the model to read the
current document's own values and never copy the examples. The examples are the same convention
the owner verified: for example, when a supplier's invoices print no SKU column, `sku` stays blank, the shade
code stays in the description and the barcode goes to `gtin` only.

## Privacy

- The store holds real values (invoice numbers, descriptions, prices, OCR text samples). It lives
  under the data directory, `learned/`, with `0700` directories and `0600` files, and is excluded
  from git by the existing `.data` rule. Nothing in it is written to logs or audits: audit entries
  carry counts, status words and supplier keys only.
- `GET /api/learned` returns counts and status per supplier, never values.
- The learner reads only the invoice being exported. It never reads other jobs.

## Provisioning (integrator)

- **Cloud storage.** The bucket prefix `learned/` is persisted and restored on start, like
  `templates/`. No new bucket, service or secret. Cost: a few kilobytes per supplier, $0.
- **Environment.** `INV_STUDIO_LEARN=0` switches learning off (no store, no examples, no overlay).
  Default is on. `INV_STUDIO_LEARNED_TEMPLATES` (os.pathsep-separated directories) points the
  worker at a store outside the data directory; the benchmark uses it, the app does not.
- **No fine-tuning and no new paid service.** The Gemini fallback receives examples in the prompt
  only; the model is unchanged.
- **Local checkout.** Nothing to install. The store is created on first start.

## Limits

- One layout per supplier key. A supplier that changes its layout is re-learned from the newest
  verified invoice; the old template is replaced only if the new one reproduces the stored samples.
- Currency is a template constant; a supplier invoicing in two currencies needs two verified invoices
  and gets the first one's currency on the second layout until it is re-learned.
- A page break pattern not present in the training invoice is not skipped.
- Two-token SKUs are joined in the order learned from the first verified invoice.
- Scanned invoices learn from OCR text; a different OCR engine or image quality changes the text and
  the template may not match. The learner refuses rather than guesses.
