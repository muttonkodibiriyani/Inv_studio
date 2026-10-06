/**
 * Playwright check that the review shows the POGRN Order No beside the reader's Purchase order (FT5a, decision 62).
 * Shown only when POG-001 found the order (printed and found, or selected by qty and value) or POG-008 matched one
 * unreceived order on items and ordered qty, as a note: the Purchase order input keeps the reader's value and a save
 * sends it unchanged. The POG-009 Order Date (decision 76) shows under it, formatted for display with the exact cell
 * text in the tooltip, or empty with its reason. Every job is mocked synthetic data.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/pogrn_order.browser.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const baseURL = (process.env.BASE_URL || "http://127.0.0.1:8765").replace(/\/$/, "");
const timeout = Number(process.env.BROWSER_TIMEOUT_MS || 120_000);
const syntheticScan = readFileSync(resolve(repoRoot, "samples/invoice-scan.png"));

async function waitFor(label, check, waitMs = timeout) {
  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await new Promise((resolveWait) => setTimeout(resolveWait, 100));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

const ev = (kind, rule, source) => [{ kind, source, reference: "POGRN!7", original: "", rule, confidence: "Exact" }];
const value = (v, evidence) => ({ value: v, evidence, flagged: false, reason: "" });
const missing = { value: null, evidence: [], flagged: true, reason: "Not found" };
const field = (label, target, cell) => ({ label, target, ...cell });
// POG-009 Order Date (decision 76): evidence only, the CREATED_DATE text of the rows cited for Order No.
const orderDate = (v, reason = "", reference = "POGRN!7") => ({ value: v, reason, rule: "POG-009",
  source: "POGRN order date of the rows cited for Order No: CREATED_DATE", reference: v ? reference : "", evidence_kind: "pogrn_order_date" });
const DATE_SOURCE = "POGRN CREATED_DATE of the rows cited for Order No, evidence only";

// Synthetic jobs: the reader's Purchase order (invoice.po) and the rules' Order No (rules.fields.po).
const cases = [
  { id: "pogrn-selected", po: "", order: value("SYN-ORD-1", ev("selected", "POG-001", "Selected by POG-001 among 3 order/location candidates")),
    note: "Order No SYN-ORD-1 · POGRN order selected by qty and value under the supplier code, not printed",
    date: orderDate("2026-09-28T00:00:00"), dateNote: `Order Date Sep 28, 2026 · ${DATE_SOURCE}`, dateTitle: "2026-09-28T00:00:00 · POGRN!7" },
  { id: "pogrn-printed", po: "SYN-ORD-2", order: value("SYN-ORD-2", ev("printed", "POG-001", "Invoice PO found as POGRN RMS_ORDER_NO")),
    note: "Order No SYN-ORD-2 · Invoice PO / Reference # found in POGRN as RMS_ORDER_NO under the supplier code",
    // Not ISO: shown as the cell's text, never reinterpreted.
    date: orderDate("28/09/2026"), dateNote: `Order Date 28/09/2026 · ${DATE_SOURCE}`, dateTitle: "28/09/2026 · POGRN!7" },
  { id: "pogrn-not-received", po: "", order: value("SYN-ORD-5", ev("sheet", "POG-008", "POGRN order matched on items and ordered qty, not received: RMS_ORDER_NO")),
    note: "Order No SYN-ORD-5 · POGRN order matched on items and ordered qty, not yet received",
    date: orderDate(null, "CREATED_DATE differs across the 2 cited rows (2 values)"),
    dateNote: "Order Date empty · CREATED_DATE differs across the 2 cited rows (2 values)", dateTitle: null },
  // A sheet citation from another rule is not a POGRN order find: no note.
  { id: "pogrn-sheet-other", po: "", order: value("SYN-ORD-6", ev("sheet", "ALG-011", "Synthetic sheet lookup")), note: null },
  // Not found in POGRN (R-024), entered by the reviewer, or empty: no note.
  { id: "pogrn-r024", po: "SYN-ORD-3", order: value("SYN-ORD-3", ev("printed", "R-024", "Invoice PO (not in POGRN)")), note: null,
    date: orderDate(null, "Order No is not a POGRN order (no rows cited)"),
    dateNote: "Order Date empty · Order No is not a POGRN order (no rows cited)", dateTitle: null },
  { id: "pogrn-entered", po: "", order: value("SYN-ORD-4", ev("owner_entry", "OWNER-ENTRY", "Reviewer")), note: null },
  { id: "pogrn-missing", po: "", order: missing, note: null },
];
const lines = [{ sku: "SYN-SKU-1", description: "Synthetic part", qty: 1, price: 10 }];
const makeJob = ({ id, po, order, date }) => ({
  id,
  filename: `SYNTHETIC-${id}.png`,
  size: syntheticScan.length,
  created_at: "2026-10-05T10:00:00+00:00",
  status: "review",
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  invoice: { number: `SYN-${id}`, supplier_name: "Synthetic Supplier", po, origin: "AE", date: "2026-10-04", currency: "AED", net: 10, tax: 0, lines },
  revision: 1,
  reviewed: false,
  trace: [],
  provenance: [],
  completeness: 0.8,
  selected_engine: "invoice2data",
  validation: { ready: false, source: "fine_rules", status: "Review", issues: [], matches: [{ line: 1, item: "", gtin: "" }] },
  rules: {
    status: "Review",
    fields: {
      number: field("Invoice number", "Document", value(`SYN-${id}`, ev("printed", "", "Invoice"))),
      po: field("Purchase order", "Order No", order),
    },
    lines: [{ line: 1, cells: { Item: missing, UPC: missing, "Unit Cost": missing, Quantity: missing, "Unit Tax Code": missing }, status: "", source_row: "", match_method: "" }],
    issues: [],
    item_lines: { resolved: 0, total: 1, rate: "0.0000", threshold: "0.95", owner_review: true, definition: "Synthetic definition" },
    revision: 1,
    ...(date ? { order_date: date } : {}),
  },
  extraction_status: "fields_extracted",
  export_id: null,
  text: "SYNTHETIC DEMONSTRATION DATA",
  boxes: [],
});
const jobs = cases.map(makeJob);

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

try {
  // One context: --single-process Chromium does not survive opening a second one.
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce" });
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const baseline = await context.request.get(`${baseURL}/api/state`);
    assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
    const state = { ...(await baseline.json()), jobs, exports: [], accounts: [], active_account: null };
    for (const [index, item] of cases.entries()) {
      const job = jobs[index];
      const page = await context.newPage();
      await page.setViewportSize(viewport);
      page.setDefaultTimeout(timeout);
      const pageErrors = [];
      page.on("pageerror", (error) => pageErrors.push(error.message));
      await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
      let saved = null;
      await page.route("**/api/jobs/*", (route) => (route.request().method() === "GET"
        ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) })
        : route.fallback()));
      await page.route("**/api/jobs/*/review", (route) => {
        saved = route.request().postDataJSON();
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) });
      });
      await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));

      await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
      await page.locator("#app-shell").waitFor({ state: "visible" });
      await page.locator(`[aria-label="Review ${job.filename}"]`).click();
      const input = page.locator('#header-fields [name="po"]');
      await input.waitFor({ state: "visible" });

      const notes = page.locator("#header-fields .po-pogrn");
      if (item.note) {
        assert.equal(await notes.count(), 1, `${item.id}: one Order No note`);
        assert.equal((await notes.textContent()).trim(), item.note, `${item.id}: note text`);
        assert(await notes.isVisible(), `${item.id}: note visible`);
        const sameField = await notes.evaluate((note) => Boolean(note.closest("label")?.querySelector('[name="po"]')));
        assert(sameField, `${item.id}: the note sits in the Purchase order field`);
      } else {
        assert.equal(await notes.count(), 0, `${item.id}: no Order No note`);
      }
      const dates = page.locator("#header-fields .po-order-date");
      if (item.dateNote) {
        assert.equal(await dates.count(), 1, `${item.id}: one Order Date note`);
        assert.equal((await dates.textContent()).trim(), item.dateNote, `${item.id}: Order Date note text`);
        assert.equal(await dates.getAttribute("title"), item.dateTitle, `${item.id}: the exact cell text and rows in the tooltip`);
        const inPo = await dates.evaluate((note) => Boolean(note.closest("label")?.querySelector('[name="po"]')));
        assert(inPo, `${item.id}: the Order Date note sits in the Purchase order field`);
      } else {
        assert.equal(await dates.count(), 0, `${item.id}: no Order Date note without an order_date`);
      }
      // Display only: the input keeps the reader's value and a save sends it unchanged.
      assert.equal(await input.inputValue(), item.po, `${item.id}: Purchase order input unchanged`);
      await page.locator("#save-review").click();
      await waitFor("the review save", async () => saved);
      assert.equal(saved.invoice.po ?? "", item.po, `${item.id}: a save sends the reader's Purchase order unchanged`);

      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 1, `Page scrolls horizontally by ${overflow}px at ${viewport.width}px`);
      assert.deepEqual(pageErrors, []);
      await page.close();
    }
  }
  console.log(`POGRN Order No browser test passed (7 synthetic mocked jobs at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}
