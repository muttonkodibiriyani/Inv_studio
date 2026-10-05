/**
 * Playwright check of the demo path polish at 1440 and 390 px: every bottom-bar item fits the phone bar, inbox
 * rows keep the number and time readable beside the target chip, another invoice opens at the top of its details,
 * a tap reveals the review on stacked layouts, long staged-source ids wrap, the target-check reason column is
 * readable, and the model inputs carry a valid pattern (no console error). Every job is mocked synthetic data.
 * SCREENSHOT_DIR=<dir> also saves screenshots.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/demo_readiness.browser.mjs
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

const printed = [{ kind: "printed", source: "Printed on invoice", reference: "page 1", original: "", rule: "", confidence: "Exact" }];
const value = (v) => ({ value: v, evidence: printed, flagged: false, reason: "" });
const counts = { cells: 3, verified: 1, empty_owner_rule: 0, empty_flagged: 0, needs_checking: 2, owner_entry: 0 };
const targetCheck = {
  version: 1, counts, metric: counts, holds: true, upc: "empty",
  summary: "Target sheet: 1 verified · 0 empty by owner rule · 0 empty (flagged) · 2 needs checking",
  checks: [{ check: "lines_to_net", status: "pass", detail: "Lines sum to the net at 2 decimals" }],
  cells: [
    { sheet: "Header", column: "Document", line: null, value: "SYN-DR-1", status: "verified", group: "verified", sub: "", reason: "", scope: "metric",
      evidence: { kind: "printed", source: "Printed on invoice", reference: "page 1", original: "", rule: "" } },
    { sheet: "Header", column: "Net Amount", line: null, value: "520.00", status: "mismatch", group: "needs_checking", sub: "", scope: "metric",
      reason: "printed text shows a different amount than the value in the cell", evidence: { kind: "printed", source: "Printed on invoice", reference: "page 1 box [0.6, 0.8, 0.7, 0.82]", original: "", rule: "" } },
    { sheet: "Details", column: "Unit Cost", line: 1, value: "10.00", status: "unverifiable", group: "needs_checking", sub: "", scope: "metric",
      reason: "evidence not re-checkable: read by AI from a scan; no box to re-check", evidence: { kind: "printed", source: "Invoice", reference: "page 1", original: "", rule: "" } },
  ],
};
const rules = {
  status: "Review",
  fields: { number: { label: "Invoice number", target: "Document", ...value("SYN-DR-1") }, net: { label: "Net total", target: "Net Amount", ...value("520.00") } },
  lines: [{ line: 1, cells: { Item: value("345000001"), "Unit Cost": value("10.00"), Quantity: value("52") }, status: "", source_row: "", match_method: "" }],
  issues: [],
  item_lines: { resolved: 1, total: 1, rate: "1.0000", threshold: "0.95", owner_review: false, definition: "" },
  po_candidates: 0,
  revision: 1,
  target_check: targetCheck,
};
const trimmed = (r) => ({ ...r, target_check: Object.fromEntries(["counts", "metric", "holds", "summary", "upc"].map((k) => [k, r.target_check[k]])) });
const makeJob = (id, number, createdAt) => ({
  id,
  filename: `SYNTHETIC-${id}.png`,
  size: syntheticScan.length,
  created_at: createdAt,
  status: "review",
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  invoice: { number, supplier_name: "Synthetic Trading Supplies Company", seller: "SYN-S", date: "2026-10-04", currency: "AED", net: 520, tax: 26,
    lines: Array.from({ length: 6 }, (_, index) => ({ sku: `SYN-${index + 1}`, description: `Synthetic item ${index + 1}`, qty: 52, price: 10 })) },
  revision: 1,
  reviewed: false,
  trace: [],
  provenance: [],
  completeness: 0.8,
  selected_engine: "invoice2data",
  validation: {
    ready: false, source: "fine_rules", status: "Review", matches: [], item_lines: rules.item_lines,
    issues: [{ code: "Target Mismatch", message: "1 cell(s) do not equal the source their evidence points to: Header.Net Amount", owner: "Accounts payable", line: null, rule: "TARGET-CHECK", blocking: true, level: "review" }],
  },
  rules,
  extraction_status: "fields_extracted",
  export_id: null,
  text: "SYNTHETIC DEMONSTRATION DATA",
  boxes: [],
});
const confirmedJob = makeJob("demo-ready-c", "SYN-INV-2026-000419", "2026-10-05T08:00:00+00:00");
confirmedJob.status = "ready";
confirmedJob.reviewed = true;
confirmedJob.validation = { ...confirmedJob.validation, ready: true, issues: confirmedJob.validation.issues.map((issue) => ({ ...issue, accepted: true })) };
const jobs = [makeJob("demo-ready-a", "SYN-INV-2026-000417", "2026-10-05T10:00:00+00:00"), makeJob("demo-ready-b", "SYN-INV-2026-000418", "2026-10-05T09:00:00+00:00"), confirmedJob];
const lookupSummary = {
  approved_for_matching: false, requires_confirmation: true, counts: { item: 2, po: 3, total: 5 },
  sources: [{ id: `dataset:${"0e9b7714b4fada09".repeat(4)}`, version: "lookup-catalog.v1", counts: { item: 2, po: 3, total: 5 } }],
};

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce" });
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const baseline = await context.request.get(`${baseURL}/api/state`);
    assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
    const state = { ...(await baseline.json()), jobs: jobs.map((job) => ({ ...job, rules: trimmed(job.rules) })), exports: [], accounts: [], active_account: null };
    const page = await context.newPage();
    await page.setViewportSize(viewport);
    page.setDefaultTimeout(timeout);
    const pageErrors = [];
    const consoleErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error" && !/Failed to load resource/.test(message.text())) consoleErrors.push(message.text()); });
    await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
    await page.route("**/api/jobs/*", (route) => (route.request().method() === "GET"
      ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(jobs.find((job) => route.request().url().endsWith(job.id)) || jobs[0]) })
      : route.fallback()));
    await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));
    await page.route("**/api/reference-lookup/summary", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(lookupSummary) }));
    const tag = viewport.width;
    const phone = viewport.width <= 700;

    await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
    await page.locator("#app-shell").waitFor({ state: "visible" });
    await page.locator(".target-chip").first().waitFor({ state: "visible" });

    // Every navigation item sits inside the phone's bottom bar (Engines & AI holds the AI toggle).
    if (phone) {
      const nav = await page.evaluate(() => [...document.querySelectorAll(".sidebar [data-nav]")].map((item) => {
        const rect = item.getBoundingClientRect();
        return { nav: item.dataset.nav, top: rect.top, bottom: rect.bottom, width: rect.width, labelFits: item.scrollWidth <= item.clientWidth + 1 };
      }));
      assert.equal(nav.length, 4);
      for (const item of nav) {
        assert(item.top >= 844 - 62 - 1 && item.bottom <= 844 + 1, `${item.nav} sits outside the bottom bar (${item.top}-${item.bottom})`);
        assert(item.labelFits, `${item.nav} label is clipped`);
      }
    }

    // Inbox rows: the number and status stay readable and the time stays inside the row beside the target chip.
    const rows = await page.evaluate(() => [...document.querySelectorAll(".job-item")].map((item) => {
      const status = item.querySelector(".job-meta-status");
      const time = item.querySelector(".job-time");
      const chip = item.querySelector(".target-chip");
      const meta = item.querySelector(".job-meta").getBoundingClientRect();
      return { size: `status ${status.scrollWidth}/${status.clientWidth} meta ${Math.round(meta.width)} chip ${Math.round(chip.getBoundingClientRect().width)}`, status: status.scrollWidth <= status.clientWidth + 1, time: time.getBoundingClientRect().right <= meta.right + 1, chip: chip.getBoundingClientRect().right <= meta.right + 1 };
    }));
    assert.equal(rows.length, 3);
    rows.forEach(({ size, ...row }, index) => assert.deepEqual(row, { status: true, time: true, chip: true }, `inbox row ${index + 1} is truncated at ${tag}px (${size})`));

    // A tap opens the review where the reviewer can see it.
    const scrollBefore = await page.evaluate(() => window.scrollY);
    await page.locator('[aria-label="Review SYNTHETIC-demo-ready-a.png"]').click();
    await page.locator("#review-panel").waitFor({ state: "visible" });
    await page.locator("#target-check").waitFor({ state: "visible" });
    await expectReviewInView(page, viewport.height, phone);
    if (!phone) assert.equal(await page.evaluate(() => window.scrollY), scrollBefore, "the side-by-side review must not jump the page");

    // The date label matches a calendar picker, not ISO typing.
    assert.match(await page.locator("#header-fields").textContent(), /Invoice date · confirm calendar date/);

    // Target-sheet reasons get a readable column.
    const reasonWidth = await page.locator(".target-group.needs_checking td:nth-child(4)").first().evaluate((cell) => cell.getBoundingClientRect().width);
    assert(reasonWidth >= 165, `target-check reason column is ${reasonWidth}px`);
    if (shots) await page.screenshot({ path: `${shots}/review-a-${tag}.png` });

    // Another invoice opens at the top of its details, not at the previous invoice's scroll position.
    if (!phone) {
      const scrolled = await page.locator(".fields-card").evaluate((card) => { card.scrollTop = 600; return card.scrollTop; });
      assert(scrolled > 0, "the details pane does not scroll at 1440");
    } else {
      await page.evaluate(() => window.scrollTo(0, 0));
    }
    await page.locator('[aria-label="Review SYNTHETIC-demo-ready-b.png"]').click();
    await waitFor(() => page.evaluate(() => document.querySelector(".job-row.active .job-item")?.getAttribute("aria-label") === "Review SYNTHETIC-demo-ready-b.png"));
    await waitFor(async () => (await page.locator(".fields-card").evaluate((card) => card.scrollTop)) === 0);
    await expectReviewInView(page, viewport.height, phone);
    if (shots) await page.screenshot({ path: `${shots}/review-b-${tag}.png` });

    // After confirm, the banner does not contradict the "ready to export" toast.
    await page.locator('[aria-label="Review SYNTHETIC-demo-ready-c.png"]').click();
    await page.locator("#validation-summary.accepted").waitFor();
    assert.match(await page.locator(".validation-details summary").textContent(), /^1 note accepted at review/);
    assert.match(await page.locator("#validation-summary li").first().textContent(), /\(accepted at review\)$/);

    // Long staged-source ids wrap inside the References cards.
    await page.locator('.sidebar [data-nav="references"]').click();
    await page.locator("#reference-source-summary", { hasText: "dataset:" }).waitFor();
    const wide = await page.evaluate(() => [...document.querySelectorAll("#references-section .card")]
      .filter((card) => card.offsetParent !== null && card.getBoundingClientRect().right > document.documentElement.clientWidth + 1).length);
    assert.equal(wide, 0, `${wide} References card(s) run past the viewport at ${tag}px`);
    if (shots) await page.screenshot({ path: `${shots}/references-${tag}.png`, fullPage: true });

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `Page scrolls horizontally by ${overflow}px at ${viewport.width}px`);
    assert.deepEqual(pageErrors, []);
    assert.deepEqual(consoleErrors, []);
    await page.close();
  }
  console.log(`Demo-readiness browser test passed (synthetic mocked jobs at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}

// Stacked (phone): the review starts near the top, under the sticky bar. Side by side: the page does not jump.
async function expectReviewInView(page, height, phone) {
  await waitFor(() => page.locator("#review-panel").evaluate((panel, limit) => {
    const top = panel.getBoundingClientRect().top;
    return top >= 0 && top <= limit;
  }, phone ? height * 0.25 : height * 0.6));
}

async function waitFor(check) {
  const deadline = Date.now() + timeout;
  while (!(await check())) {
    if (Date.now() > deadline) throw new Error(`condition never held: ${check}`);
    await new Promise((done) => setTimeout(done, 100));
  }
}
