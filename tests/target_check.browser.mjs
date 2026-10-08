/**
 * Playwright check of the target-sheet check on screen: the owner line and cells by status
 * with evidence on the review screen, the inbox chip, and the accuracy screen. Every job and
 * summary is mocked synthetic data. SCREENSHOT_DIR=<dir> also saves screenshots.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/target_check.browser.mjs
 */
import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const baseURL = (process.env.BASE_URL || "http://127.0.0.1:8765").replace(/\/$/, "");
const timeout = Number(process.env.BROWSER_TIMEOUT_MS || 120_000);
const shots = process.env.SCREENSHOT_DIR || "";
const syntheticScan = readFileSync(resolve(repoRoot, "samples/invoice-scan.png"));
if (shots) mkdirSync(shots, { recursive: true });

const printed = [{ kind: "printed", source: "Printed on invoice", reference: "page 1 box [0.1, 0.2, 0.3, 0.25]", original: "", rule: "", confidence: "Exact" }];
const sheet = [{ kind: "sheet", source: "Item Master", reference: "Item Master!2", original: "", rule: "V-001", confidence: "Exact" }];
const value = (v, evidence) => ({ value: v, evidence, flagged: false, reason: "" });
const ev = (kind, source, reference, rule = "") => ({ kind, source, reference, original: "", rule });
const cell = (sheetName, column, line, v, status, group, reason, evidence, sub = "") => (
  { sheet: sheetName, column, line, value: v, status, group, sub, reason, scope: "metric", evidence });
const buckets = { verified: 4, empty_owner_rule: 2, empty_flagged: 1, mismatch: 1, over_cited: 0, no_evidence: 0, unverifiable: 1, data_gap: 0, owner_entry_unattributed: 0 };
const counts = { cells: 9, verified: 4, empty_owner_rule: 2, empty_flagged: 1, needs_checking: 2, buckets, owner_entry: 0 };
const targetCheck = {
  version: 1, checked_at: "2026-10-05T10:00:00+00:00", transaction: 1, upc: "empty", counts, metric: counts, holds: true,
  summary: "Target sheet: 4 verified · 2 empty by owner rule · 1 empty (flagged) · 2 needs checking",
  checks: [
    { check: "lines_to_net", status: "pass", detail: "Lines sum to the net at 2 decimals" },
    { check: "tax_breakdown_to_header", status: "pass", detail: "Tax breakdown equals the header tax" },
    { check: "net_plus_tax_gross", status: "skipped", detail: "No gross printed" },
    { check: "joins", status: "pass", detail: "Every row joins to exactly one Header" },
    { check: "template", status: "pass", detail: "Columns, order, types and decimals match the owner's template" },
  ],
  cells: [
    cell("Header", "Document", null, "SYN-TC-1", "verified", "verified", "", ev("printed", "Printed on invoice", "page 1 box [0.1, 0.1, 0.2, 0.12]")),
    cell("Header", "Supplier Site", null, "900001", "verified", "verified", "", ev("sheet", "Supplier Sites", "Supplier Sites!4", "V-001")),
    cell("Header", "Net Amount", null, "100.00", "mismatch", "needs_checking", "printed text shows a different amount", ev("printed", "Printed on invoice", "page 1 box [0.6, 0.8, 0.7, 0.82]")),
    cell("Header", "Order No", null, "", "empty_flagged", "empty_flagged", "Not found in owner sheets or on the invoice", ev("", "", "")),
    cell("Header", "Comment", null, "", "empty_owner_rule", "empty_owner_rule", "left empty by the owner's rule", ev("", "", "")),
    cell("Tax_Breakdown", "Tax Code", null, "S", "verified", "verified", "", ev("sheet", "Item Master", "Item Master!2", "V-001")),
    cell("Details", "Item", 1, "345000001", "verified", "verified", "", ev("sheet", "Item Master", "Item Master!2", "V-001")),
    cell("Details", "UPC", 1, "", "empty_owner_rule", "empty_owner_rule", "UPC left empty by the owner's rule", ev("", "", "")),
    cell("Details", "Unit Cost", 1, "10.00", "unverifiable", "needs_checking", "evidence not re-checkable: read by AI from a scan; no box to re-check", ev("printed", "Invoice", "page 1")),
  ],
};

const rules = {
  status: "Review",
  fields: {
    number: { label: "Invoice number", target: "Document", ...value("SYN-TC-1", printed) },
    net: { label: "Net total", target: "Net Amount", ...value("100.00", printed) },
  },
  lines: [{ line: 1, cells: { Item: value("345000001", sheet), UPC: { value: null, evidence: [], flagged: true, reason: "Not found" }, "Unit Cost": value("10.00", printed), Quantity: value("10", printed), "Unit Tax Code": value("S", sheet) }, status: "", source_row: "", match_method: "" }],
  issues: [],
  item_lines: { resolved: 1, total: 1, rate: "1.0000", threshold: "0.95", owner_review: false, definition: "" },
  po_candidates: 0,
  revision: 1,
  target_check: targetCheck,
};
const trimmed = { ...rules, target_check: Object.fromEntries(["counts", "metric", "holds", "summary", "upc"].map((k) => [k, targetCheck[k]])) };

const job = {
  id: "target-check-a",
  filename: "SYNTHETIC-target-a.png",
  size: syntheticScan.length,
  created_at: "2026-10-05T10:00:00+00:00",
  status: "review",
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  invoice: { number: "SYN-TC-1", supplier_name: "Synthetic Supplier", seller: "SYN-S", date: "2026-10-04", currency: "AED", net: 100, tax: 5, lines: [{ sku: "SYN-1", qty: 10, price: 10 }] },
  revision: 1,
  reviewed: false,
  trace: [],
  provenance: [],
  completeness: 0.8,
  selected_engine: "invoice2data",
  readers: {
    ai: { status: "gap_fill", reason: "The local readers left gaps; the AI filled only empty fields", calls: 1 },
    header: { number: "native", net: "ocr" }, lines: [{ price: "ai" }],
  },
  evidence: { header: { net: { review: {
    reason: "The AI read a different value here", other_value: "SYNTHETIC", other_quote: "Net SYNTHETIC", other_page: 1,
  } } }, lines: [{ net_amount: { review: { reason: "a line net column is printed but this value was not read", code: "not_read" } } }] },
  validation: {
    ready: false,
    source: "fine_rules",
    status: "Review",
    issues: [
      { code: "Target Mismatch", message: "1 cell(s) do not equal the source their evidence points to: Header.Net Amount", owner: "Accounts payable", line: null, rule: "TARGET-CHECK", blocking: true, level: "review" },
      { code: "Target Unverified", message: "1 filled cell(s) could not be proven from their evidence: Details.Unit Cost", owner: "Accounts payable", line: null, rule: "TARGET-CHECK", blocking: true, level: "review" },
    ],
    matches: [],
    item_lines: rules.item_lines,
  },
  rules,
  extraction_status: "fields_extracted",
  export_id: null,
  text: "SYNTHETIC DEMONSTRATION DATA",
  boxes: [],
};
const verifiedJob = { ...job, id: "target-check-b", filename: "SYNTHETIC-target-b.png", created_at: "2026-10-05T09:00:00+00:00", status: "ready",
  rules: { ...rules, target_check: { ...targetCheck, holds: false, summary: "Target sheet: 7 verified · 2 empty by owner rule · 0 empty (flagged) · 0 needs checking" } } };
const trimmedVerified = { ...verifiedJob, rules: { ...verifiedJob.rules, target_check: Object.fromEntries(["counts", "metric", "holds", "summary", "upc"].map((k) => [k, verifiedJob.rules.target_check[k]])) } };

const rate = (cells, changed) => ({ cells, unchanged: cells - changed, changed, accuracy: cells ? Number(((cells - changed) / cells).toFixed(4)) : null });
const period = (confirms, n) => ({
  confirms,
  overall: rate(15 * n, n),
  fields: { "Header.Document": rate(n, 0), "Header.Net Amount": rate(n, n), "Details.Item": rate(5 * n, 0) },
  by_status_before: { verified: rate(12 * n, 0), empty_flagged: rate(2 * n, n), mismatch: rate(n, 0) },
  suppliers: { "SYN-001": rate(10 * n, n), "SYN-002": rate(5 * n, 0) },
});
const accuracy = { generated_at: "2026-10-05T10:00:00+00:00", invoices: 4, periods: { "7d": period(1, 1), "30d": period(3, 3), all: period(4, 4) } };

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce" });
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const baseline = await context.request.get(`${baseURL}/api/state`);
    assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
    const state = { ...(await baseline.json()), jobs: [{ ...job, rules: trimmed }, trimmedVerified], exports: [], accounts: [], active_account: null };
    const page = await context.newPage();
    await page.setViewportSize(viewport);
    page.setDefaultTimeout(timeout);
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    const supplierAsked = [];
    await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
    await page.route("**/api/jobs/*", (route) => (route.request().method() === "GET"
      ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(route.request().url().endsWith("target-check-b") ? verifiedJob : job) })
      : route.fallback()));
    await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));
    await page.route("**/api/target-check/accuracy*", (route) => {
      supplierAsked.push(new URL(route.request().url()).searchParams.get("supplier"));
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(accuracy) });
    });
    const tag = viewport.width;

    await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
    await page.locator("#app-shell").waitFor({ state: "visible" });
    // Inbox chips: counts only, the owner line as the tooltip.
    const chips = page.locator(".target-chip");
    await chips.first().waitFor({ state: "visible" });
    assert.deepEqual(await chips.allTextContents(), ["2 to check", "Target verified"]);
    assert.equal(await chips.first().getAttribute("title"), targetCheck.summary);

    await page.locator('[aria-label="Review SYNTHETIC-target-a.png"]').click();
    const panel = page.locator("#target-check");
    await panel.waitFor({ state: "visible" });
    assert.equal(await page.locator("#target-check-line").textContent(), targetCheck.summary);
    assert.match(await page.locator("#target-check-hold").textContent(), /Held at review/);
    assert(await panel.evaluate((node) => node.open), "a held invoice opens the panel");
    const groups = await page.locator(".target-group > summary").allTextContents();
    assert.deepEqual(groups, ["Needs checking · 2", "Empty (flagged) · 1", "Verified · 4", "Empty by owner rule · 2"]);
    const needs = await page.locator(".target-group.needs_checking").textContent();
    assert.match(needs, /Header\.Net AmountMismatch100\.00printed text shows a different amountPrinted on invoice · page 1 box/);
    assert.match(needs, /Details\.Unit Cost line 1Unverifiable10\.00evidence not re-checkable: read by AI from a scan/);
    assert.equal(await page.locator(".target-group.verified").evaluate((node) => node.open), false);
    assert.equal(await page.locator("#target-check-checks .target-check-row").count(), 5);
    assert.match(await page.locator("#validation-summary").textContent(), /Target Mismatch: 1 cell\(s\)/);
    // The reader per field, why the AI ran, and both values where the readers disagree.
    assert.deepEqual(await page.locator("#rules-fields .reader-badge").allTextContents(), ["native", "ocr"]);
    assert.deepEqual(await page.locator("#rules-lines .reader-badge").allTextContents(), ["ai"]);
    // A review without a code reads neutrally; a line field with no rules column shows beside the line number.
    assert.match(await page.locator("#rules-fields").textContent(), /Check: The AI read a different value here/);
    assert.equal(await page.locator('#rules-lines [data-review-code="not_read"]').textContent(),
      "Line net · Not read: a line net column is printed but this value was not read");
    assert.match(await page.locator("#rules-fields").textContent(), /other read: SYNTHETIC \(page 1\)/);
    assert.equal(await page.locator("#rules-ai-reader").textContent(),
      "AI reader: Gap fill · 1 call — The local readers left gaps; the AI filled only empty fields");
    if (shots) await page.screenshot({ path: `${shots}/review-${tag}.png`, fullPage: true });

    await page.locator('[aria-label="Review SYNTHETIC-target-b.png"]').click();
    await page.locator("#target-check-hold", { hasText: "Target sheet OK" }).waitFor();

    await page.locator('[data-nav="accuracy"]').first().click();
    await page.locator("#accuracy-section").waitFor({ state: "visible" });
    await page.locator("#accuracy-overall", { hasText: "4 confirmed invoices" }).waitFor();
    assert.match(await page.locator("#accuracy-overall").textContent(), /60 cells · 56 unchanged by the owner · accuracy 93\.3%/);
    assert.match(await page.locator("#accuracy-fields").textContent(), /Header\.Net Amount4040\.0%/);
    assert.deepEqual(await page.locator("#accuracy-supplier option").allTextContents(), ["All suppliers", "SYN-001", "SYN-002"]);
    await page.locator('[data-accuracy-period="7d"]').click();
    await page.locator("#accuracy-overall", { hasText: "1 confirmed invoice ·" }).waitFor();
    await page.locator("#accuracy-supplier").selectOption("SYN-002");
    await waitForSupplier(supplierAsked, "SYN-002");
    if (shots) await page.screenshot({ path: `${shots}/accuracy-${tag}.png`, fullPage: true });

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `Page scrolls horizontally by ${overflow}px at ${viewport.width}px`);
    assert.deepEqual(pageErrors, []);
    await page.close();
  }
  console.log(`Target-check browser test passed (synthetic mocked jobs at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}

async function waitForSupplier(asked, supplier) {
  const deadline = Date.now() + timeout;
  while (!asked.includes(supplier)) {
    if (Date.now() > deadline) throw new Error(`accuracy was never asked for ${supplier}`);
    await new Promise((done) => setTimeout(done, 100));
  }
}
