/**
 * Playwright check of the inbox "Export selected (draft)" button (FT6, decisions 67/70/72). It is enabled when at
 * least one selected invoice has extracted data, sends only those to /api/exports/target-draft with their revisions,
 * keeps the server's DRAFT file name, reports the server's included/skipped counts, shows a 409 as-is, and never calls
 * an export route. Export approved stays greyed for unapproved invoices. Every job is mocked synthetic data.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/target_draft.browser.mjs
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
const makeJob = (id, status, invoice) => ({
  id,
  filename: `SYNTHETIC-${id}.png`,
  size: syntheticScan.length,
  created_at: "2026-10-05T10:00:00+00:00",
  status,
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  invoice,
  revision: 3,
  reviewed: false,
  trace: [],
  provenance: [],
  completeness: 0.8,
  selected_engine: "invoice2data",
  validation: { ready: false, issues: [] },
  extraction_status: invoice.lines.length ? "fields_extracted" : "no_fields",
  export_id: null,
  text: "SYNTHETIC DEMONSTRATION DATA",
  boxes: [],
});
const filled = (number) => ({ number, supplier_name: "Synthetic Supplier", origin: "AE", date: "2026-10-04", currency: "AED", net: 10, tax: 0, lines });
// 2 held invoices with data, 1 with nothing extracted: none is approved.
const jobs = [
  makeJob("draft-1", "review", filled("SYN-D1")),
  makeJob("draft-2", "review", filled("SYN-D2")),
  makeJob("draft-empty", "review", { number: "", supplier_name: "", origin: "", date: "", currency: "", net: null, tax: null, lines: [] }),
];

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

try {
  // One context: --single-process Chromium does not survive opening a second one.
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce", acceptDownloads: true });
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const baseline = await context.request.get(`${baseURL}/api/state`);
    assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
    const state = { ...(await baseline.json()), jobs, exports: [], accounts: [], active_account: null };
    const page = await context.newPage();
    await page.setViewportSize(viewport);
    page.setDefaultTimeout(timeout);
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    const posted = [];
    page.on("request", (request) => { if (request.method() !== "GET") posted.push(new URL(request.url()).pathname); });
    let stateLoads = 0;
    await page.route("**/api/state", (route) => {
      stateLoads += 1;
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) });
    });
    let draft = null;
    let reply = { status: 200, contentType: "application/octet-stream", headers: { "content-disposition": 'attachment; filename="DRAFT_Target_SYNTHETIC.xlsx"', "x-draft-included": "1", "x-draft-skipped": "1" }, body: "SYNTHETIC" };
    await page.route("**/api/exports/target-draft", (route) => {
      draft = route.request().postDataJSON();
      return route.fulfill(reply);
    });

    await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
    await page.locator("#app-shell").waitFor({ state: "visible" });
    const button = page.locator("#batch-draft");
    const rowBox = (id) => page.locator(`[aria-label="Select SYNTHETIC-${id}.png for download or deletion"]`);
    await rowBox("draft-1").waitFor({ state: "visible" });
    assert.equal(await button.isDisabled(), true, "enabled with nothing selected");
    // Only the invoice without data: still nothing to export.
    await rowBox("draft-empty").check();
    assert.equal(await button.isDisabled(), true, "enabled with no extracted data selected");
    // Select all: 2 with data + 1 without. The draft enables; Export approved and the review workbook stay greyed.
    await page.locator("#select-all-jobs").check();
    assert.equal(await page.locator("#selected-count").textContent(), "3 selected");
    assert.equal(await button.isDisabled(), false, "a selection with extracted data cannot be drafted");
    assert.equal(await page.locator("#batch-export").isDisabled(), true, "Export approved enabled without approvals");

    await button.click();
    const dialog = page.locator("#batch-draft-dialog");
    await dialog.waitFor({ state: "visible" });
    assert.equal(await page.locator("#batch-draft-count").textContent(), "2 invoices");
    assert.match(await page.locator("#batch-draft-skipped").textContent(), /1 selected without extracted data is left out/);
    assert.match(await dialog.textContent(), /No invoice is marked exported, approved or confirmed/);

    const loadsBefore = stateLoads;
    const download = page.waitForEvent("download");
    await page.locator("#confirm-batch-draft").click();
    assert.equal((await download).suggestedFilename(), "DRAFT_Target_SYNTHETIC.xlsx");
    await waitFor("the draft request", async () => draft);
    assert.deepEqual(draft, { jobs: [{ id: "draft-1", revision: 3 }, { id: "draft-2", revision: 3 }] });
    await dialog.waitFor({ state: "hidden" });
    const banner = page.locator("#global-message");
    assert.match(await banner.textContent(), /1 invoice in one draft target workbook; 1 left out, see the Checks sheet\. Nothing was exported or approved\./);
    assert.match(await banner.getAttribute("class"), /warning/);
    // The inbox reloads once to show any rules refresh; the selection stays, and the draft wrote nothing else.
    await waitFor("the state reload", async () => stateLoads > loadsBefore);

    assert.equal(await page.locator("#selected-count").textContent(), "3 selected");
    assert.deepEqual(posted, ["/api/exports/target-draft"]);

    // Without the count headers it reports what it sent, with none left out.
    draft = null;
    reply = { status: 200, contentType: "application/octet-stream", headers: { "content-disposition": 'attachment; filename="DRAFT_Target_SYNTHETIC.xlsx"' }, body: "SYNTHETIC" };
    await button.click();
    const second = page.waitForEvent("download");
    await page.locator("#confirm-batch-draft").click();
    await second;
    await waitFor("the fallback banner", async () => /^2 invoices in one draft target workbook\. Nothing was exported or approved\.$/.test(await banner.textContent()));
    assert.match(await banner.getAttribute("class"), /success/);
    assert.deepEqual(posted, ["/api/exports/target-draft", "/api/exports/target-draft"]);

    // When 0 qualify the server answers 409; it is shown as the server wrote it; the dialog stays open for another try.
    draft = null;
    reply = { status: 409, contentType: "application/json", body: JSON.stringify({ detail: "No selected invoice has current rules. Refresh before exporting." }) };
    await button.click();
    await page.locator("#confirm-batch-draft").click();
    await waitFor("the refused draft", async () => draft);
    await waitFor("the error banner", async () => /No selected invoice has current rules/.test(await banner.textContent()));
    assert.match(await banner.getAttribute("class"), /error/);
    assert.equal(await dialog.isVisible(), true);
    assert.equal(await page.locator("#confirm-batch-draft").isDisabled(), false);
    await page.locator("#batch-draft-dialog [value=cancel]").last().click();

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `Page scrolls horizontally by ${overflow}px at ${viewport.width}px`);
    assert.deepEqual(pageErrors, []);
    await page.close();
  }
  console.log(`Target draft browser test passed (3 synthetic mocked jobs at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}
