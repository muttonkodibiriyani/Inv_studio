/**
 * Playwright check that a document with no item lines or no key fields never shows a fields-found percentage (D88).
 * The box reads "Not read" with the reason from the reader trace (scanned image, OCR, AI), the empty panel names the
 * same reason, trace entries that found nothing print no percentage, and a fully read invoice keeps its figure.
 * A read invoice whose figure is lowered names what lowers it, and an empty Tax code shows its R-016 exception.
 * Every job is mocked synthetic data.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/read_failure.browser.mjs
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

const base = {
  size: syntheticScan.length,
  created_at: "2026-10-05T10:00:00+00:00",
  status: "review",
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  revision: 1,
  reviewed: false,
  provenance: [],
  selected_engine: "paddleocr",
  validation: { ready: false, issues: [], matches: [] },
  export_id: null,
  boxes: [],
};
const taxReason = "Printed tax amount is missing; the region rate cannot be checked";
const barcodeReason = "printed barcode fails the GTIN check digit (likely misprint) - check the printout";
const unreadLine = (n) => ({ description: `Synthetic row ${n}`, qty: 1, price: 2 });
const jobs = [
  {
    // One header value and no lines: the old box showed a low percentage beside nothing read.
    ...base,
    id: "rf-one-field",
    filename: "SYNTHETIC-one-field.pdf",
    invoice: { currency: "AED", lines: [] },
    completeness: 0.17,
    text: "",
    trace: [
      { engine: "invoice2data", status: "needs_ocr", reason: "This PDF has no usable text layer." },
      { engine: "paddleocr", status: "unavailable", reason: "Optional engine is not installed" },
      { engine: "docling", status: "unavailable", reason: "Optional engine is not installed" },
    ],
  },
  {
    // Line rows but no key fields: the old box showed a high percentage.
    ...base,
    id: "rf-rows-only",
    filename: "SYNTHETIC-rows-only.pdf",
    invoice: { lines: Array.from({ length: 7 }, (_, index) => unreadLine(index + 1)) },
    completeness: 0.76,
    text: "SYNTHETIC OCR TEXT ".repeat(40),
    trace: [
      { engine: "paddleocr", status: "extracted", method: "layout", text_characters: 760, extracted_fields: 0, line_items: 7, completeness: 0.76 },
      { engine: "openai", model: "", status: "needs_connection", reason: "Select a model in AI connections" },
    ],
  },
  {
    // Nothing at all: the empty panel shows, with the reason.
    ...base,
    id: "rf-empty",
    filename: "SYNTHETIC-empty.pdf",
    invoice: { lines: [] },
    completeness: 0,
    text: "",
    trace: [
      { engine: "invoice2data", status: "text_only", method: "text_only", text_characters: 2, extracted_fields: 0, line_items: 0, completeness: 0 },
      { engine: "paddleocr", status: "failed", reason: "Synthetic OCR failure" },
      { engine: "openai", model: "synthetic-model", status: "failed", reason: "Synthetic AI failure" },
    ],
  },
  {
    // A read invoice keeps its figure.
    ...base,
    id: "rf-read",
    filename: "SYNTHETIC-read.png",
    selected_engine: "docling",
    invoice: { number: "SYN-RF-1", date: "2026-10-04", currency: "AED", net: 25, tax: 0, lines: [{ sku: "SYN-1", qty: 2, price: 10, uom: "EA" }, { sku: "SYN-2", qty: 1, price: 5, uom: "EA" }] },
    completeness: 0.8,
    text: "SYNTHETIC DEMONSTRATION DATA",
    trace: [
      { engine: "paddleocr", status: "text_only", method: "text_only", text_characters: 30, extracted_fields: 0, line_items: 0, completeness: 0 },
      { engine: "docling", status: "extracted", method: "docling_table", text_characters: 900, extracted_fields: 6, line_items: 2, completeness: 0.8 },
    ],
  },
  {
    // Lines and header read, tax total not: the figure names the tax, and Tax code shows the rule's reason.
    ...base,
    id: "rf-no-tax",
    filename: "SYNTHETIC-no-tax.pdf",
    selected_engine: "openai",
    // The printed line amounts reach the net only through float noise (0.1 + 0.2 - 0.29), which the server's Decimal check accepts.
    invoice: { number: "SYN-RF-2", date: "2026-10-04", currency: "AED", net: 0.29, lines: [{ sku: "SYN-1", qty: 2, price: 10, uom: "EA", net_amount: 0.1 }, { sku: "SYN-2", qty: 1, price: 5, uom: "EA", net_amount: 0.2 }] },
    completeness: 0.93,
    text: "SYNTHETIC DIGITAL TEXT",
    trace: [{ engine: "openai", model: "synthetic-model", status: "extracted", extracted_fields: 4, line_items: 2, completeness: 0.93 }],
    validation: {
      ready: false,
      source: "fine_rules",
      status: "Review",
      issues: [{ code: "Tax Code", message: taxReason, owner: "Tax reviewer", line: null, rule: "R-016", blocking: true }],
      matches: [],
      item_lines: { resolved: 2, total: 2, rate: "1.0000", threshold: "0.95", owner_review: false, definition: "Synthetic definition" },
    },
    rules: {
      status: "Review",
      fields: {
        tax: { label: "Tax total", target: "Tax Amount", value: null, evidence: [], flagged: true, reason: "" },
        taxCode: { label: "Tax code", target: "Tax Code", value: null, evidence: [], flagged: true, reason: "Not found" },
        location: { label: "Delivery location", target: "Location", value: null, evidence: [], flagged: true, reason: "Not found" },
      },
      lines: [{
        line: 1,
        cells: { Item: { value: null, evidence: [], flagged: true, reason: "Not found" }, UPC: { value: null, evidence: [], flagged: true, reason: barcodeReason } },
        status: "",
        source_row: "",
        match_method: "",
      }],
      issues: [],
      item_lines: { resolved: 2, total: 2, rate: "1.0000", threshold: "0.95", owner_review: false, definition: "Synthetic definition" },
      revision: 1,
    },
  },
];

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

try {
  // One context: --single-process Chromium does not survive opening a second one.
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce" });
  const baseline = await context.request.get(`${baseURL}/api/state`);
  assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
  const state = { ...(await baseline.json()), jobs, exports: [], accounts: [], active_account: null };
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const at = `${viewport.width}px`;
    const page = await context.newPage();
    await page.setViewportSize(viewport);
    page.setDefaultTimeout(timeout);
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
    await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));
    await page.route("**/api/jobs/*", (route) => {
      const job = jobs.find((candidate) => route.request().url().endsWith(`/api/jobs/${candidate.id}`));
      return job && route.request().method() === "GET"
        ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) })
        : route.fallback();
    });
    await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
    await page.locator("#review-panel").waitFor({ state: "visible" });
    const open = async (job) => {
      await Promise.all([
        page.waitForResponse((response) => response.url().endsWith(`/api/jobs/${job.id}`) && response.request().method() === "GET"),
        page.locator(`[aria-label="Review ${job.filename}"]`).click(),
      ]);
      await page.waitForFunction((filename) => document.querySelector("#selected-filename")?.textContent === filename, job.filename);
    };
    const box = page.locator("#completeness");
    const figure = page.locator("#completeness strong");
    const reason = page.locator("#completeness-reason");

    assert.equal(await page.locator("#completeness span").textContent(), "Fields found", `${at}: the box is not named Fields found`);
    assert.match(await box.getAttribute("title"), /does not check that any value is correct/, `${at}: the tooltip does not say it is not correctness`);

    await open(jobs[0]);
    assert(await box.isVisible(), `${at}: one-field job hides the box`);
    assert.equal(await figure.textContent(), "Not read", `${at}: one header field and 0 lines still shows a percentage`);
    assert.equal(await reason.textContent(), "Could not read this document: no item lines and no key fields were found (scanned image, no text layer; OCR reader not installed; AI fallback not run).");
    assert(await reason.isVisible(), `${at}: the reason is hidden`);

    await open(jobs[1]);
    assert.equal(await figure.textContent(), "Not read", `${at}: rows without key fields still show a percentage`);
    assert.equal(await reason.textContent(), "Could not read this document: no key fields were found (AI fallback not set up).");
    const rowsTrace = await page.locator("#trace-list li").allTextContents();
    assert.match(rowsTrace[0], /0 fields · 7 items$/, `${at}: a reader with 0 fields still printed a percentage`);

    await open(jobs[2]);
    assert(await box.isHidden(), `${at}: the empty job shows the box`);
    assert.equal(await page.locator("#empty-extraction-title").textContent(), "This document could not be read");
    assert.match(await page.locator("#empty-extraction-detail").textContent(), /^Why: scanned image, no text layer; OCR failed; AI read failed\. /);

    await open(jobs[3]);
    assert.equal(await figure.textContent(), "80%", `${at}: a read invoice lost its figure`);
    assert(await reason.isHidden(), `${at}: a read invoice shows a not-read reason`);
    assert(!(await box.evaluate((element) => element.classList.contains("not-read"))), `${at}: a read invoice is styled as not read`);
    const readTrace = await page.locator("#trace-list li").allTextContents();
    assert.doesNotMatch(readTrace[0], /%/, `${at}: a reader with 0 items printed a percentage`);
    assert.match(readTrace[1], /6 fields · 2 items · 80% fields found/);

    await open(jobs[4]);
    assert.equal(await figure.textContent(), "93%", `${at}: the tax-missing invoice lost its figure`);
    assert.equal(await reason.textContent(), "Lowered by: Tax total missing.", `${at}: the figure does not say the tax total lowers it`);
    const ruleField = async (label) => page.locator("#rules-fields dt").filter({ hasText: new RegExp(`^${label}$`) }).locator("xpath=following-sibling::dd[1]").textContent();
    assert.match(await ruleField("Tax code"), new RegExp(`^${taxReason}`), `${at}: the empty Tax code does not show the R-016 reason`);
    assert.match(await ruleField("Delivery location"), /^Not found in owner sheets or on the invoice/, `${at}: a field with no reason lost the generic text`);
    const lineCell = (column) => page.locator(`#rules-lines tr:first-child td:nth-child(${column})`).textContent();
    assert.match(await lineCell(3), new RegExp(`^${barcodeReason.replace(/[()]/g, "\\$&")}`), `${at}: the empty UPC cell does not show its own reason`);
    assert.match(await lineCell(2), /^Not found/, `${at}: an empty line cell with no reason lost "Not found"`);

    assert.deepEqual(pageErrors, []);
    await page.close();
  }
  console.log(`Read failure browser test passed (${jobs.length} synthetic jobs at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}
