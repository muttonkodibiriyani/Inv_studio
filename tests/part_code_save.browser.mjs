/**
 * Playwright check that a review save keeps each line's printed part code (FT3, decision 57).
 * part_code has no input, and the review replaces the stored lines with the saved ones, so the
 * row carries it; a line without one, and a line the reviewer adds, send no part_code key.
 * Every job is mocked synthetic data.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/part_code_save.browser.mjs
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

const printed = [{ kind: "printed", source: "Printed on invoice", reference: "page 1 box [0.1, 0.2, 0.3, 0.25]", original: "", rule: "", confidence: "Exact" }];
const value = (v, evidence) => ({ value: v, evidence, flagged: false, reason: "" });
const missing = { value: null, evidence: [], flagged: true, reason: "Not found" };
const field = (label, target, cell) => ({ label, target, ...cell });

// Line 1 carries a printed part code beside a different description code in sku; line 2 has none.
const lines = [
  { sku: "SYN-DESC-1", part_code: "SYN-PC-0001", description: "Synthetic part", qty: 2, price: 10 },
  { sku: "SYN-DESC-2", description: "Synthetic other", qty: 1, price: 5 },
];
const rules = {
  status: "Review",
  fields: {
    number: field("Invoice number", "Document", value("SYN-PC-INV", printed)),
    po: field("Purchase order", "Order No", missing),
    net: field("Net total", "Net Amount", value("25.00", printed)),
  },
  lines: lines.map((_, index) => ({
    line: index + 1,
    cells: { Item: missing, UPC: missing, "Unit Cost": value("10.00", printed), Quantity: value("2", printed), "Unit Tax Code": missing },
    status: "",
    source_row: "",
    match_method: "",
  })),
  issues: [],
  item_lines: { resolved: 0, total: 2, rate: "0.0000", threshold: "0.95", owner_review: true, definition: "Synthetic definition of a resolved line" },
  revision: 1,
};
const job = {
  id: "part-code-save",
  filename: "SYNTHETIC-part-code.png",
  size: syntheticScan.length,
  created_at: "2026-10-05T10:00:00+00:00",
  status: "review",
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  invoice: { number: "SYN-PC-INV", supplier_name: "Synthetic Supplier", origin: "AE", date: "2026-10-04", currency: "AED", net: 25, tax: 0, lines },
  revision: 1,
  reviewed: false,
  trace: [],
  provenance: [],
  completeness: 0.8,
  selected_engine: "invoice2data",
  validation: { ready: false, source: "fine_rules", status: "Review", issues: [], matches: [{ line: 1, item: "", gtin: "" }, { line: 2, item: "", gtin: "" }], item_lines: rules.item_lines },
  rules,
  extraction_status: "fields_extracted",
  export_id: null,
  text: "SYNTHETIC DEMONSTRATION DATA",
  boxes: [],
};

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
    const state = { ...(await baseline.json()), jobs: [job], exports: [], accounts: [], active_account: null };
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
    // Wait for the click's own GET: the boot already opened this job, and editing while the click's refetch is in
    // flight is covered by review_reselect.browser.mjs.
    await Promise.all([
      page.waitForResponse((response) => response.url().endsWith(`/api/jobs/${job.id}`) && response.request().method() === "GET"),
      page.locator('[aria-label="Review SYNTHETIC-part-code.png"]').click(),
    ]);
    await page.locator("#line-items tr").first().waitFor({ state: "visible" });

    // The reviewer edits line 1's description and adds a line; the printed part code stays on line 1 only.
    await page.locator('#line-items tr:nth-child(1) [name="description"]').fill("Synthetic part, edited");
    await page.locator("#add-line").click();
    await page.locator('#line-items tr:nth-child(3) [name="sku"]').fill("SYN-ADDED");
    await page.locator("#save-review").click();
    await waitFor("the review save", async () => saved);

    const sent = saved.invoice.lines;
    assert.equal(sent.length, 3);
    assert.equal(sent[0].part_code, "SYN-PC-0001", "a save drops the printed part code");
    assert.equal(sent[0].sku, "SYN-DESC-1");
    assert.equal(sent[0].description, "Synthetic part, edited");
    // No key at all where there is no part code: a server without the field rejects unknown keys.
    assert(!("part_code" in sent[1]), "a line without a part code sends one");
    assert(!("part_code" in sent[2]), "an added line sends a part code");
    assert.equal(await page.locator('#line-items [name="part_code"]').count(), 0, "part_code is not an editable input");

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `Page scrolls horizontally by ${overflow}px at ${viewport.width}px`);
    assert.deepEqual(pageErrors, []);
    await page.close();
  }
  console.log(`Part code save browser test passed (synthetic mocked job at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}
