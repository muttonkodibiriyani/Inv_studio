/**
 * Playwright check that read reviews (evidence.*.review) show beside their review-form inputs, worded by code
 * (F4, decisions 144-146): header and line, a neutral "Check" without a code, nothing where there is no review,
 * and line notes hidden once a saved line count no longer matches the read. Every job is mocked synthetic data.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/review_notes.browser.mjs
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

const review = (reason, code, other) => ({ review: { reason, other_value: other ?? null, ...(code ? { code } : {}) } });
// Line 1: gtin unreadable + sku not read; line 2: gtin unconfirmed + line net not read; line 3: no review.
const lines = [
  { sku: null, gtin: null, description: "Synthetic one", qty: 1, price: 5 },
  { sku: "SYN-2", gtin: null, description: "Synthetic two", qty: 2, price: 5 },
  { sku: "SYN-3", gtin: null, description: "Synthetic three", qty: 3, price: 5 },
];
const evidence = {
  header: {
    tax: review("no tax total printed on the invoice", "not_printed"),
    po: review("a P.O. Box is not an order number"),
  },
  lines: [
    { gtin: review("barcode unreadable or incomplete", "unreadable", "12345"), sku: review("an item code column is printed but this value was not read", "not_read") },
    { gtin: review("barcode not confirmed on the page", "unconfirmed", "40000008"), net_amount: review("a line net column is printed but this value was not read", "not_read") },
    {},
  ],
};
const job = {
  id: "review-notes",
  filename: "SYNTHETIC-review-notes.png",
  size: syntheticScan.length,
  created_at: "2026-10-06T10:00:00+00:00",
  status: "review",
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  invoice: { number: "SYN-RN-INV", supplier_name: "Synthetic Supplier", date: "2026-10-04", currency: "AED", net: 30, tax: null, lines },
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
  evidence,
};

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

try {
  // One context and one page: --single-process Chromium does not survive opening a second one.
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce" });
  const baseline = await context.request.get(`${baseURL}/api/state`);
  assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
  // The list payload carries no evidence (the server pops it); only the full job GET has the reviews.
  const { evidence: _listless, ...listed } = job;
  const state = { ...(await baseline.json()), jobs: [listed], exports: [], accounts: [], active_account: null };
  let served = job;
  const page = await context.newPage();
  page.setDefaultTimeout(timeout);
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
  await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));
  await page.route("**/api/jobs/*", (route) => (route.request().method() === "GET" && route.request().url().endsWith(`/api/jobs/${job.id}`)
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(served) })
    : route.fallback()));
  await page.route("**/api/jobs/*/review", (route) => {
    // As today's server: the saved lines replace the read, the evidence stays as read.
    served = { ...job, revision: 2, invoice: route.request().postDataJSON().invoice };
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(served) });
  });
  await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
  await page.locator("#line-items tr").nth(2).waitFor({ state: "visible" });

  const note = (scope, name) => page.locator(`${scope} [name="${name}"] + .review-note`);
  const row = (n) => `#line-items tr:nth-child(${n})`;
  assert.equal(await note("#header-fields", "tax").textContent(), "Not printed: no tax total printed on the invoice");
  assert.equal(await note("#header-fields", "tax").getAttribute("data-review-code"), "not_printed");
  assert.equal(await note("#header-fields", "po").textContent(), "Check: a P.O. Box is not an order number", "no code reads neutrally");
  assert.equal(await note(row(1), "gtin").textContent(), "Barcode unreadable: barcode unreadable or incomplete · other read: 12345");
  assert.equal(await note(row(1), "sku").textContent(), "Not read: an item code column is printed but this value was not read");
  assert.equal(await note(row(2), "gtin").textContent(), "Barcode not confirmed: barcode not confirmed on the page · other read: 40000008");
  assert(await note(row(2), "net_amount").isVisible(), "a line net review opens its printed amounts");
  assert.equal(await note(row(2), "net_amount").textContent(), "Not read: a line net column is printed but this value was not read");
  assert.equal(await page.locator(`${row(3)} .review-note`).count(), 0, "no review leaves the row unchanged");
  assert.equal(await page.locator(`${row(3)} details.line-amounts`).getAttribute("open"), null);
  assert.equal(await page.locator("#line-reviews-hidden").isVisible(), false);

  // Remove the unreviewed third line and save: the read's line notes no longer match the rows.
  await page.locator(`${row(3)} .remove-line`).click();
  await page.locator("#save-review").click();
  await waitFor("the saved form", async () => (await page.locator("#line-items tr").count()) === 2 && served.revision === 2
    && await page.locator("#line-reviews-hidden").isVisible());
  assert.equal(await page.locator("#line-items .review-note").count(), 0, "line notes hidden once the line count changed");
  assert.equal(await page.locator("#line-reviews-hidden").textContent(), "Line read notes hidden after a line was added or removed.");
  assert.equal(await note("#header-fields", "tax").textContent(), "Not printed: no tax total printed on the invoice", "header reviews still shown");
  assert.deepEqual(pageErrors, []);
  console.log(`Review notes browser test passed (header and line reviews by code, hidden after a line count change; ${baseURL}).`);
} finally {
  await browser.close();
}
