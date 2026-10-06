/**
 * Playwright check of the inbox "Select all" checkbox (FT6, decision 64). It ticks every selectable invoice in the
 * list as shown (a search narrows it), unticking clears them, a partial pick shows the indeterminate state, and
 * "N selected" counts them. The combined download sends exactly the ticked invoices. Every job is mocked synthetic data.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/select_all.browser.mjs
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

const lines = [{ sku: "SYN-SKU-1", description: "Synthetic part", qty: 1, price: 10 }];
const makeJob = (id, supplier, status = "review") => ({
  id,
  filename: `SYNTHETIC-${id}.png`,
  size: syntheticScan.length,
  created_at: "2026-10-05T10:00:00+00:00",
  status,
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  invoice: { number: `SYN-${id}`, supplier_name: supplier, origin: "AE", date: "2026-10-04", currency: "AED", net: 10, tax: 0, lines },
  revision: 2,
  reviewed: false,
  trace: [],
  provenance: [],
  completeness: 0.8,
  selected_engine: "invoice2data",
  extraction_status: "fields_extracted",
  export_id: null,
  text: "SYNTHETIC DEMONSTRATION DATA",
  boxes: [],
});
// 3 selectable Alpha/Beta jobs, plus 1 still processing that can never be ticked.
const jobs = [
  makeJob("sel-a1", "Synthetic Alpha"),
  makeJob("sel-a2", "Synthetic Alpha"),
  makeJob("sel-b1", "Synthetic Beta"),
  makeJob("sel-a3", "Synthetic Alpha", "processing"),
];

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
    let batch = null;
    await page.route("**/api/exports/extraction-batch", (route) => {
      batch = route.request().postDataJSON();
      return route.fulfill({ status: 200, contentType: "application/octet-stream", headers: { "content-disposition": 'attachment; filename="SYNTHETIC_REVIEW.xlsx"' }, body: "SYNTHETIC" });
    });

    await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
    await page.locator("#app-shell").waitFor({ state: "visible" });
    const all = page.locator("#select-all-jobs");
    await all.waitFor({ state: "visible" });
    const count = page.locator("#selected-count");
    const rowBox = (id) => page.locator(`[aria-label="Select SYNTHETIC-${id}.png for download or deletion"]`);
    const ticked = async () => {
      const ids = [];
      for (const job of jobs) if (await rowBox(job.id).count() && await rowBox(job.id).isChecked()) ids.push(job.id);
      return ids.sort();
    };
    const mixed = () => all.evaluate((box) => box.indeterminate);
    assert.equal((await all.locator("xpath=..").textContent()).trim(), "Select all");

    // Ticks every selectable invoice in the list; the processing one stays unticked.
    await all.check();
    assert.deepEqual(await ticked(), ["sel-a1", "sel-a2", "sel-b1"]);
    assert.equal(await count.textContent(), "3 selected");
    assert.equal(await mixed(), false);
    // A partial pick is indeterminate; ticking again selects all.
    await rowBox("sel-a2").uncheck();
    assert.equal(await count.textContent(), "2 selected");
    assert.equal(await all.isChecked(), false);
    assert.equal(await mixed(), true);
    await all.check();
    assert.equal(await count.textContent(), "3 selected");
    assert.equal(await mixed(), false);
    // Unticking clears them all.
    await all.uncheck();
    assert.deepEqual(await ticked(), []);
    assert.equal(await count.textContent(), "0 selected");

    // A search narrows it: only the shown Alpha invoices are ticked.
    await page.locator("#job-search").fill("alpha");
    await all.check();
    assert.equal(await count.textContent(), "2 selected");
    await page.locator("#job-search").fill("");
    assert.deepEqual(await ticked(), ["sel-a1", "sel-a2"]);
    assert.equal(await mixed(), true, "the full list is partly ticked");

    // The existing combined download sends exactly the ticked invoices.
    await all.check();
    await page.locator("#batch-review").click();
    await page.locator("#confirm-batch-review").click();
    await waitFor("the combined download", async () => batch);
    assert.deepEqual(batch.jobs.map((job) => job.id).sort(), ["sel-a1", "sel-a2", "sel-b1"]);
    assert(batch.jobs.every((job) => job.revision === 2));

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `Page scrolls horizontally by ${overflow}px at ${viewport.width}px`);
    assert.deepEqual(pageErrors, []);
    await page.close();
  }
  console.log(`Select all browser test passed (4 synthetic mocked jobs at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}
