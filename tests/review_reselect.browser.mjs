/**
 * Playwright check that reopening the open invoice never loses or blocks the reviewer's work (FT3 CI race).
 * (a) Edits typed while the click's GET for the same job is in flight are kept, and the save sends them.
 * (b) A save that fails with 'Invoice changed' still reloads the server version over the dirty form.
 * Every job is mocked synthetic data.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/review_reselect.browser.mjs
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

const lines = [
  { sku: "SYN-DESC-1", part_code: "SYN-PC-0001", description: "Synthetic part", qty: 2, price: 10 },
  { sku: "SYN-DESC-2", description: "Synthetic other", qty: 1, price: 5 },
];
const rules = {
  status: "Review",
  fields: {
    number: field("Invoice number", "Document", value("SYN-RS-INV", printed)),
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
  id: "review-reselect",
  filename: "SYNTHETIC-reselect.png",
  size: syntheticScan.length,
  created_at: "2026-10-05T10:00:00+00:00",
  status: "review",
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  invoice: { number: "SYN-RS-INV", supplier_name: "Synthetic Supplier", origin: "AE", date: "2026-10-04", currency: "AED", net: 25, tax: 0, lines },
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

// The server's copy after another save: revision 2 with a new line-1 description.
const serverJob = {
  ...job,
  revision: 2,
  invoice: { ...job.invoice, lines: [{ ...lines[0], description: "Synthetic part, server version" }, lines[1]] },
  rules: { ...rules, revision: 2 },
};
const isJobGet = (request) => request.url().endsWith(`/api/jobs/${job.id}`) && request.method() === "GET";
const description = '#line-items tr:nth-child(1) [name="description"]';

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

async function openPage(context, viewport, state) {
  const page = await context.newPage();
  await page.setViewportSize(viewport);
  page.setDefaultTimeout(timeout);
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
  await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));
  return { page, pageErrors };
}

try {
  // One context: --single-process Chromium does not survive opening a second one.
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce" });
  const baseline = await context.request.get(`${baseURL}/api/state`);
  assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
  const state = { ...(await baseline.json()), jobs: [job], exports: [], accounts: [], active_account: null };
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const at = `${viewport.width}px`;

    // (a) The boot opens the job; the reviewer clicks it again and types while that GET is held.
    {
      const { page, pageErrors } = await openPage(context, viewport, state);
      let gets = 0;
      let release;
      const held = new Promise((resolveHeld) => { release = resolveHeld; });
      let saved = null;
      await page.route("**/api/jobs/*", async (route) => {
        if (!isJobGet(route.request())) return route.fallback();
        gets += 1;
        if (gets > 1) await held;
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) });
      });
      await page.route("**/api/jobs/*/review", (route) => {
        saved = route.request().postDataJSON();
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) });
      });
      await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
      await page.locator("#line-items tr").first().waitFor({ state: "visible" });
      await waitFor("the boot GET", async () => gets === 1);
      await Promise.all([page.waitForRequest(isJobGet), page.locator('[aria-label="Review SYNTHETIC-reselect.png"]').click()]);
      await page.locator(description).fill("Synthetic part, edited");
      await page.locator("#add-line").click();
      await page.locator('#line-items tr:nth-child(3) [name="sku"]').fill("SYN-ADDED");
      const answered = page.waitForResponse((response) => isJobGet(response.request()));
      release();
      await answered;
      await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => setTimeout(done, 50))));
      assert.equal(await page.locator(description).inputValue(), "Synthetic part, edited", `${at}: the in-flight GET replaced the typed edit`);
      assert.equal(await page.locator("#line-items tr").count(), 3, `${at}: the in-flight GET dropped the added line`);
      await page.locator("#save-review").click();
      await waitFor("the review save", async () => saved);
      assert.equal(saved.invoice.lines.length, 3, `${at}: the save sends the added line`);
      assert.equal(saved.invoice.lines[0].description, "Synthetic part, edited", `${at}: the save sends the typed edit`);
      assert.deepEqual(pageErrors, []);
      await page.close();
    }

    // (b) A save conflict on a dirty form still loads the server version.
    {
      const { page, pageErrors } = await openPage(context, viewport, state);
      let served = job;
      let posted = 0;
      await page.route("**/api/jobs/*", (route) => (isJobGet(route.request())
        ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(served) })
        : route.fallback()));
      await page.route("**/api/jobs/*/review", (route) => {
        posted += 1;
        served = serverJob;
        return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "Invoice changed. Refresh before saving." }) });
      });
      await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
      await page.locator("#line-items tr").first().waitFor({ state: "visible" });
      await page.locator(description).fill("Synthetic part, edited");
      await page.locator("#add-line").click();
      await page.locator("#save-review").click();
      await waitFor("the server version after the conflict", async () => posted === 1
        && await page.locator(description).inputValue() === "Synthetic part, server version");
      assert.equal(await page.locator("#line-items tr").count(), 2, `${at}: the conflict reload shows the server's 2 lines`);
      assert.deepEqual(pageErrors, []);
      await page.close();
    }
  }
  console.log(`Review reselect browser test passed (in-flight edit and save conflict at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}
