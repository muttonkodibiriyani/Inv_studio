/**
 * Playwright check that the manual draft's Item follows the target workbook (decision 56):
 * when matches are present, a line's Item is the rules' match only, so a typed item_id with no match
 * leaves Item empty (ITM-006); with no matches the typed item_id fills Item as before (decision 57).
 * Every job is mocked synthetic data.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/manual_draft_item.browser.mjs
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

const sheet = (reference) => [{ kind: "sheet", source: "Item Master", reference, original: "", rule: "V-001", confidence: "Exact" }];
const printed = [{ kind: "printed", source: "Printed on invoice", reference: "page 1 box [0.1, 0.2, 0.3, 0.25]", original: "", rule: "", confidence: "Exact" }];
const value = (v, evidence) => ({ value: v, evidence, flagged: false, reason: "" });
const missing = { value: null, evidence: [], flagged: true, reason: "Not found" };
const field = (label, target, cell) => ({ label, target, ...cell });

// Line 1 has a rules match and a different typed item; line 2 has only a typed item; line 3 has neither.
const lines = [
  { sku: "SYN-1", qty: 5, price: 10, item_id: "SYN-TYPED-1" },
  { sku: "SYN-2", qty: 5, price: 10, item_id: "SYN-TYPED-2" },
  { sku: "SYN-3", qty: 1, price: 0 },
];
const rules = {
  status: "Review",
  fields: {
    number: field("Invoice number", "Document", value("SYN-DRAFT-1", printed)),
    site: field("Supplier site", "Supplier Site", value("900001", sheet("Supplier Sites!4"))),
    po: field("Purchase order", "Order No", missing),
    location: field("Delivery location", "Location", value("901", sheet("Locations!7"))),
    location_type: field("Location type", "Location Type", value("Store (S)", sheet("Locations!7"))),
    date: field("Invoice date", "Document Date", value("10/4/2026", printed)),
    taxCode: field("Tax code", "Tax Code", value("S", sheet("Item Master!2"))),
    net: field("Net total", "Net Amount", value("100.00", printed)),
    tax: field("Tax total", "Tax Amount", value("5.00", printed)),
  },
  lines: lines.map((_, index) => ({
    line: index + 1,
    cells: {
      Item: index === 0 ? value("345000001", sheet("Item Master!2")) : missing,
      UPC: index === 0 ? value("00012345678905", sheet("Item Master!2")) : missing,
      "Unit Cost": value("10.00", printed),
      Quantity: value("5", printed),
      "Unit Tax Code": value("S", sheet("Item Master!2")),
    },
    status: "",
    source_row: "",
    match_method: "",
  })),
  issues: [],
  item_lines: { resolved: 1, total: 3, rate: "0.3333", threshold: "0.95", owner_review: true, definition: "Synthetic definition of a resolved line" },
  revision: 1,
};

const baseJob = {
  size: syntheticScan.length,
  created_at: "2026-10-05T10:00:00+00:00",
  status: "review",
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  revision: 1,
  reviewed: false,
  trace: [],
  provenance: [],
  completeness: 0.8,
  selected_engine: "invoice2data",
  extraction_status: "fields_extracted",
  export_id: null,
  text: "SYNTHETIC DEMONSTRATION DATA",
  boxes: [],
};
const invoice = { number: "SYN-DRAFT-1", supplier_name: "Synthetic Supplier", origin: "AE", date: "2026-10-04", currency: "AED", net: 100, tax: 5, lines };
const current = {
  ...baseJob,
  id: "draft-item-current",
  filename: "SYNTHETIC-draft-current.png",
  invoice,
  validation: {
    ready: false,
    source: "fine_rules",
    status: "Review",
    issues: [],
    matches: [{ line: 1, item: "345000001", gtin: "00012345678905" }, { line: 2, item: "", gtin: "" }, { line: 3, item: "", gtin: "" }],
    item_lines: rules.item_lines,
    location_type: "Store (S)",
  },
  rules,
};
// A stale or missing rules result: the server sends no matches and no rules view.
const pending = {
  ...baseJob,
  id: "draft-item-pending",
  filename: "SYNTHETIC-draft-pending.png",
  created_at: "2026-10-05T09:00:00+00:00",
  invoice: { ...invoice, number: "SYN-DRAFT-2", lines: [{ sku: "SYN-4", qty: 2, price: 3, item_id: "SYN-TYPED-4" }] },
  validation: {
    ready: false,
    source: "fine_rules",
    issues: [{ code: "Rules Pending", message: "The fine rules have not run on this revision yet", owner: "", line: null, blocking: true, level: "hard" }],
    matches: [],
  },
  rules: null,
};
const jobs = [current, pending];

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
    const page = await context.newPage();
    await page.setViewportSize(viewport);
    page.setDefaultTimeout(timeout);
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
    await page.route("**/api/jobs/*", (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      const job = jobs.find((candidate) => new URL(route.request().url()).pathname.endsWith(`/${candidate.id}`));
      return job ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) }) : route.fallback();
    });
    await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));

    const draftItems = async (job) => {
      await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
      await page.locator("#app-shell").waitFor({ state: "visible" });
      await page.locator(`[aria-label="Review ${job.filename}"]`).click();
      await page.getByRole("button", { name: "Create unvalidated draft" }).click();
      const dialog = page.locator("#manual-draft-dialog");
      await dialog.waitFor({ state: "visible" });
      const rows = dialog.locator("#manual-draft-lines tr");
      const read = (name) => rows.locator(`[name="${name}"]`).evaluateAll((inputs) => inputs.map((input) => input.value));
      return { Item: await read("Item"), UPC: await read("UPC") };
    };

    // Current rules: Item is the rules' match only, the same cell the target workbook writes.
    const withRules = await draftItems(current);
    assert.deepEqual(withRules.Item, ["345000001", "", ""], "Manual draft Item differs from the rules' Item");
    assert.deepEqual(withRules.UPC, ["00012345678905", "", ""]);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `Page scrolls horizontally by ${overflow}px at ${viewport.width}px`);

    // No current rules: the typed item fills Item, as the server extraction draft does.
    const withoutRules = await draftItems(pending);
    assert.deepEqual(withoutRules.Item, ["SYN-TYPED-4"]);

    assert.deepEqual(pageErrors, []);
    await page.close();
  }
  console.log(`Manual draft Item browser test passed (synthetic mocked jobs at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}
