/**
 * Playwright check that a stored line break shows, and saves, as a space (F3, decision 134).
 * A text input drops line breaks from its value, so a multi-row description read as "A\nB" would
 * come back from a no-edit save as "AB"; the review form shows "A B" and a no-edit save posts it.
 * Every job is mocked synthetic data.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/oneline_save.browser.mjs
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

// Line 1 has continuation rows joined with \n (and a \r\n); line 2 has none and must save unchanged.
const lines = [
  { sku: "SYN-ML\n1", description: "SYN A\nSYN B\r\nSYN C", qty: 2, price: 10 },
  { sku: "SYN-PLAIN-2", description: "Synthetic plain  two", qty: 1, price: 5 },
];
const job = {
  id: "oneline-save",
  filename: "SYNTHETIC-oneline.png",
  size: syntheticScan.length,
  created_at: "2026-10-06T10:00:00+00:00",
  status: "review",
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  invoice: { number: "SYN-OL-INV", supplier_name: "Synthetic\nSupplier", date: "2026-10-04", currency: "AED", net: 25, tax: 0, lines },
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

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

try {
  // One context and one page: --single-process Chromium does not survive opening a second one.
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce" });
  const baseline = await context.request.get(`${baseURL}/api/state`);
  assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
  const state = { ...(await baseline.json()), jobs: [job], exports: [], accounts: [], active_account: null };
  const page = await context.newPage();
  page.setDefaultTimeout(timeout);
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  let saved = null;
  await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
  await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));
  await page.route("**/api/jobs/*", (route) => (route.request().method() === "GET" && route.request().url().endsWith(`/api/jobs/${job.id}`)
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) })
    : route.fallback()));
  await page.route("**/api/jobs/*/review", (route) => {
    saved = route.request().postDataJSON();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) });
  });
  await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
  await page.locator("#line-items tr").first().waitFor({ state: "visible" });

  const row = (n, name) => page.locator(`#line-items tr:nth-child(${n}) [name="${name}"]`);
  assert.equal(await row(1, "description").inputValue(), "SYN A SYN B SYN C", "each stored line break shows as one space");
  assert.equal(await row(1, "sku").inputValue(), "SYN-ML 1");
  assert.equal(await row(2, "description").inputValue(), "Synthetic plain  two", "a value without breaks shows unchanged");
  assert.equal(await page.locator('#header-fields [name="supplier_name"]').inputValue(), "Synthetic Supplier", "header text fields too");

  await page.locator("#save-review").click();
  await waitFor("the review save", async () => saved);
  assert.equal(saved.invoice.lines[0].description, "SYN A SYN B SYN C", "a no-edit save posts what the form shows, words not fused");
  assert.equal(saved.invoice.lines[0].sku, "SYN-ML 1");
  assert.equal(saved.invoice.lines[1].description, "Synthetic plain  two");
  assert.equal(saved.invoice.supplier_name, "Synthetic Supplier");
  assert.deepEqual(pageErrors, []);
  console.log(`One-line save browser test passed (line and header breaks show and save as spaces; ${baseURL}).`);
} finally {
  await browser.close();
}
