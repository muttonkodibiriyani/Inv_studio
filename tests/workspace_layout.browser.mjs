/**
 * Focused Playwright regression for the inbox upload-time sort and the wide
 * three-pane review workbench. Every job is mocked synthetic data.
 *
 * Run against the isolated development server:
 *   BASE_URL=http://127.0.0.1:8770 node tests/workspace_layout.browser.mjs
 * Set SCREENSHOT_DIR to also save one workbench screenshot per desktop width.
 */
import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const baseURL = (process.env.BASE_URL || "http://127.0.0.1:8765").replace(/\/$/, "");
const timeout = Number(process.env.BROWSER_TIMEOUT_MS || 120_000);
const screenshotDir = process.env.SCREENSHOT_DIR || "";
const now = new Date("2026-10-05T15:00:00Z");
const syntheticScan = readFileSync(resolve(repoRoot, "samples/invoice-scan.png"));

async function waitFor(label, check, waitMs = timeout) {
  const deadline = Date.now() + waitMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const result = await check();
      if (result) return result;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 100));
  }
  throw new Error(`Timed out waiting for ${label}${lastError ? `: ${lastError.message}` : ""}`);
}

const syntheticInvoice = {
  number: "SYN-SORT-001",
  supplier_name: "Synthetic Supplier North",
  buyer_name: "Synthetic Buyer",
  seller: "SYN-SELLER",
  site: "SYN-SITE",
  buyer: "SYN-BUYER",
  po: "70001",
  location: "SYN-LOC",
  date: "2026-10-04",
  currency: "AED",
  origin: "AE",
  market: "AE",
  taxCode: "S",
  net: 800,
  tax: 40,
  lines: [
    { sku: "SYN-SER30", gtin: "00012345678905", description: "Synthetic serum 30 ml", uom: "EA", qty: 10, price: 60, net_amount: 600, tax_amount: 30 },
    { sku: "SYN-CRM50", gtin: "00012345678912", description: "Synthetic cream 50 ml", uom: "EA", qty: 5, price: 40, net_amount: 200, tax_amount: 10 },
  ],
};

function syntheticJob(key, createdAt, status, overrides = {}) {
  const structured = ["review", "ready", "exported"].includes(status);
  return {
    id: `layout-sort-${key}`,
    filename: `SYNTHETIC-sort-${key}.png`,
    size: syntheticScan.length,
    created_at: createdAt,
    status,
    options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
    invoice: structured
      ? { ...syntheticInvoice, number: `SYN-SORT-${key.toUpperCase()}` }
      : { number: null, supplier_name: null, lines: [] },
    revision: 1,
    reviewed: status !== "review" && structured,
    trace: [],
    provenance: [],
    completeness: structured ? 0.9 : 0,
    selected_engine: structured ? "invoice2data" : null,
    validation: { ready: status === "ready", issues: [], matches: [] },
    extraction_status: structured ? "fields_extracted" : null,
    export_id: status === "exported" ? "synthetic-export" : null,
    text: structured ? "SYNTHETIC DEMONSTRATION DATA" : "",
    boxes: [],
    ...overrides,
  };
}

// Server order is by last update, deliberately unrelated to upload time.
const jobs = [
  syntheticJob("c", "2026-10-04T18:20:00+00:00", "review", { invoice: { ...syntheticInvoice, number: "SYN-SORT-C", supplier_name: "Synthetic Supplier North" } }),
  syntheticJob("x", null, "review", { invoice: { ...syntheticInvoice, number: "SYN-SORT-X", supplier_name: "Synthetic Supplier South" } }),
  syntheticJob("a", "2026-10-05T14:32:00+00:00", "ready"),
  syntheticJob("e", "2026-10-01T11:45:00+00:00", "exported", { invoice: { ...syntheticInvoice, number: "SYN-SORT-E", supplier_name: "Synthetic Supplier South" } }),
  syntheticJob("b", "2026-10-05T09:05:00+00:00", "processing"),
  syntheticJob("d", "2025-12-30T08:00:00+00:00", "error", { invoice: { number: null, supplier_name: "Synthetic Supplier North", lines: [] } }),
];
const jobsById = new Map(jobs.map((job) => [job.id, job]));
const newestOrder = ["a", "b", "c", "e", "d", "x"];
const oldestOrder = ["d", "e", "c", "b", "a", "x"];
const expectedTimes = {
  a: "2:32 PM",
  b: "9:05 AM",
  c: "Oct 4, 6:20 PM",
  e: "Oct 1, 11:45 AM",
  d: "Dec 30, 2025, 8:00 AM",
  x: "",
};

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    timezoneId: "UTC",
    locale: "en-US",
    reducedMotion: "reduce",
  });
  const baselineResponse = await context.request.get(`${baseURL}/api/state`);
  assert(baselineResponse.ok(), `Baseline state failed with HTTP ${baselineResponse.status()}`);
  const mockedState = {
    ...(await baselineResponse.json()),
    jobs,
    exports: [],
    accounts: [],
    active_account: null,
  };

  const page = await context.newPage();
  page.setDefaultTimeout(timeout);
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.clock.setFixedTime(now);

  await page.route("**/api/state", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(mockedState),
  }));
  await page.route("**/api/jobs/*", (route) => {
    const id = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1));
    const job = jobsById.get(id);
    if (route.request().method() !== "GET" || !job) return route.fallback();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) });
  });
  await page.route("**/api/jobs/*/document", (route) => route.fulfill({
    status: 200,
    contentType: "image/png",
    body: syntheticScan,
  }));

  const rowKeys = () => page.locator("#job-list .job-row .job-item").evaluateAll((items) =>
    items.map((item) => item.getAttribute("aria-label").match(/SYNTHETIC-sort-(\w+)\./)[1]));
  const rowTimes = () => page.locator("#job-list .job-row").evaluateAll((rows) => Object.fromEntries(rows.map((row) => {
    const key = row.querySelector(".job-item").getAttribute("aria-label").match(/SYNTHETIC-sort-(\w+)\./)[1];
    const time = row.querySelector("time.job-time");
    return [key, time ? time.textContent.replace(/\s+/g, " ").trim() : ""];
  })));
  const sortPressed = () => page.locator("[data-inbox-sort]").evaluateAll((buttons) =>
    Object.fromEntries(buttons.map((button) => [button.dataset.inboxSort, button.getAttribute("aria-pressed")])));
  const batchControlState = () => page.locator("#batch-controls button").evaluateAll((buttons) =>
    buttons.map((button) => `${button.id}:${button.disabled}:${button.textContent.trim()}`));
  const checkedKeys = () => page.locator("#job-list .job-row").evaluateAll((rows) => rows
    .filter((row) => row.querySelector(".job-select").checked)
    .map((row) => row.querySelector(".job-item").getAttribute("aria-label").match(/SYNTHETIC-sort-(\w+)\./)[1])
    .sort());

  await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
  await page.locator("#app-shell").waitFor({ state: "visible" });
  await waitFor("six synthetic inbox rows", async () => (await page.locator("#job-list .job-row").count()) === 6);

  // Newest upload first by default, whatever order the server returned.
  assert.deepEqual(await rowKeys(), newestOrder, "Inbox is not sorted newest upload first");
  assert.deepEqual(await sortPressed(), { newest: "true", oldest: "false" });
  assert(await page.locator("#inbox-sort").isVisible(), "The time sort toggle is not visible");
  assert.deepEqual(await rowTimes(), expectedTimes, "Inbox rows do not show their added time");
  assert.equal(
    await page.locator("#job-list .job-row").first().locator("time.job-time").getAttribute("datetime"),
    "2026-10-05T14:32:00+00:00",
  );
  await waitFor("the newest invoice to open on load", async () =>
    (await page.locator("#job-list .job-row.active .job-item").getAttribute("aria-label")) === "Review SYNTHETIC-sort-a.png");
  const processingRow = page.locator("#job-list .job-row").nth(1);
  assert.match(await processingRow.locator(".job-meta-status").textContent(), /^Processing$/);
  assert(await processingRow.locator(".job-select").isDisabled(), "A processing invoice became selectable");

  // The toggle reverses the order; unknown upload times always stay last.
  await page.getByRole("button", { name: "Oldest" }).click();
  assert.deepEqual(await rowKeys(), oldestOrder, "Oldest-first toggle did not reverse the inbox");
  assert.deepEqual(await sortPressed(), { newest: "false", oldest: "true" });
  assert.deepEqual(await rowTimes(), expectedTimes, "Toggling the sort changed the added times");
  await page.getByRole("button", { name: "Newest" }).click();
  assert.deepEqual(await rowKeys(), newestOrder, "Newest-first toggle did not restore the inbox order");

  // Search filters within the chosen order.
  await page.locator("#job-search").fill("north");
  assert.deepEqual(await rowKeys(), ["a", "c", "d"], "Search did not keep the newest-first order");
  await page.getByRole("button", { name: "Oldest" }).click();
  assert.deepEqual(await rowKeys(), ["d", "c", "a"], "Search did not keep the oldest-first order");
  await page.locator("#job-search").fill("no such synthetic invoice");
  assert.equal(await page.locator("#job-list .table-empty").textContent(), "No invoices match this search.");
  await page.locator("#job-search").fill("");
  await page.getByRole("button", { name: "Newest" }).click();

  // Selection and bulk actions do not depend on the display order.
  await page.locator("#select-all-finished").click();
  assert.equal(await page.locator("#selected-count").textContent(), "5 selected");
  assert.deepEqual(await checkedKeys(), ["a", "c", "d", "e", "x"]);
  assert(await page.locator("#batch-delete").isEnabled(), "Delete selected did not enable");
  const controlsNewest = await batchControlState();
  await page.getByRole("button", { name: "Oldest" }).click();
  assert.deepEqual(await checkedKeys(), ["a", "c", "d", "e", "x"], "Toggling the sort lost the selection");
  assert.deepEqual(await batchControlState(), controlsNewest, "Toggling the sort changed the bulk actions");
  await page.locator("#select-all-finished").click();
  assert.equal(await page.locator("#selected-count").textContent(), "0 selected");
  await page.locator("#select-ready").click();
  assert.deepEqual(await checkedKeys(), ["a"], "Select approved did not pick the approved invoice");
  assert(await page.locator("#batch-export").isEnabled(), "Export approved did not enable");
  await page.locator("#select-ready").click();
  await page.locator("#select-processed").click();
  assert.deepEqual(await checkedKeys(), ["a", "c", "e", "x"], "Select processed changed");
  assert(await page.locator("#batch-review").isEnabled(), "Printed values only did not enable");
  assert(await page.locator("#batch-fine-rules").isEnabled(), "Run ULTA rules did not enable");
  await page.locator("#select-processed").click();
  const oldestRowC = page.locator("#job-list .job-row").filter({ has: page.locator('[aria-label="Review SYNTHETIC-sort-c.png"]') });
  await oldestRowC.locator(".job-select").check();
  await page.getByRole("button", { name: "Newest" }).click();
  assert.deepEqual(await checkedKeys(), ["c"], "A single checked row did not survive the sort toggle");
  assert.equal(await page.locator("#selected-count").textContent(), "1 selected");
  await oldestRowC.locator(".job-select").uncheck();

  // A fresh upload lands at the top while it is still queued, as runUpload does.
  const uploaded = syntheticJob("n", now.toISOString(), "queued");
  mockedState.jobs = [...jobs, uploaded];
  jobsById.set(uploaded.id, uploaded);
  await page.evaluate((job) => { replaceJobSummary(job); revealNewestJobs(); }, uploaded);
  assert.deepEqual((await rowKeys()).slice(0, 2), ["n", "a"], "A new upload did not appear first");
  const newRow = page.locator("#job-list .job-row").first();
  assert.match(await newRow.locator(".job-meta-status").textContent(), /^Queued$/);
  assert.equal((await newRow.locator("time.job-time").textContent()).replace(/\s+/g, " "), "3:00 PM");
  assert(await newRow.locator(".job-select").isDisabled(), "A queued upload became selectable");
  assert.equal(await page.locator("#job-list").evaluate((list) => list.scrollTop), 0);

  // Wide workbench: three panes side by side and filling the viewport.
  const fieldColumns = {};
  for (const [width, height] of [[1280, 720], [1366, 768], [1440, 900], [1920, 1080], [2560, 1440]]) {
    await page.setViewportSize({ width, height });
    await page.locator("#job-list .job-item[aria-label='Review SYNTHETIC-sort-a.png']").click();
    await page.locator("#review-panel").waitFor({ state: "visible" });
    await page.locator("#document-image:not([hidden])").waitFor({ state: "visible" });
    await page.locator(".workspace-grid").evaluate((grid) => grid.scrollIntoView({ block: "end" }));
    const layout = await page.evaluate(() => {
      const rect = (selector) => document.querySelector(selector).getBoundingClientRect().toJSON();
      const fieldLefts = new Set([...document.querySelectorAll("#header-fields .field-wrap")].map((field) => Math.round(field.getBoundingClientRect().left)));
      const fields = document.querySelector(".fields-card");
      return {
        viewport: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        sectionMaxWidth: getComputedStyle(document.querySelector("#workspace-section")).maxWidth,
        compactNav: document.querySelector("#app-shell").classList.contains("nav-compact"),
        grid: rect(".workspace-grid"),
        inbox: rect(".inbox-card"),
        evidence: rect(".evidence-card"),
        frame: rect(".evidence-card .document-frame-wrap"),
        details: rect(".fields-card"),
        detailsOverflowY: getComputedStyle(fields).overflowY,
        detailsOverflows: fields.scrollHeight > fields.clientHeight,
        fieldColumns: fieldLefts.size,
      };
    });
    const label = `${width}x${height}`;
    assert.equal(layout.sectionMaxWidth, "none", `${label}: workspace still has a fixed max-width`);
    assert(layout.scrollWidth <= layout.viewport, `${label}: page overflows horizontally (${layout.scrollWidth}px)`);
    assert(layout.inbox.right <= layout.evidence.left, `${label}: inbox and document overlap`);
    assert(layout.evidence.right <= layout.details.left, `${label}: document and details overlap`);
    assert(Math.abs(layout.evidence.top - layout.details.top) < 2, `${label}: document and details are not side by side`);
    assert(layout.details.width > layout.evidence.width, `${label}: details pane is not the widest review pane`);
    assert(layout.viewport - layout.details.right < 48, `${label}: details pane does not reach the right edge`);
    assert(layout.grid.height >= Math.min(height - 104, 540) - 1, `${label}: workbench does not fill the viewport height`);
    assert(Math.abs(layout.evidence.bottom - layout.details.bottom) < 2, `${label}: review panes do not share the full height`);
    assert(layout.frame.height >= layout.evidence.height - 140, `${label}: document viewer does not fill its pane`);
    assert.equal(layout.detailsOverflowY, "auto", `${label}: details pane is not its own scroll area`);
    if (width <= 1440) assert(layout.detailsOverflows, `${label}: synthetic details should overflow at this size`);
    assert.equal(layout.compactNav, width < 1600, `${label}: unexpected sidebar mode`);
    fieldColumns[width] = layout.fieldColumns;

    if (layout.detailsOverflows) {
      const scrollBefore = await page.evaluate(() => window.scrollY);
      await page.locator(".fields-card").evaluate((card) => { card.scrollTop = 160; });
      assert.equal(await page.evaluate(() => window.scrollY), scrollBefore, `${label}: scrolling details moved the page`);
      assert(await page.locator(".fields-card").evaluate((card) => card.scrollTop) > 0, `${label}: details did not scroll`);
      await page.locator(".fields-card").evaluate((card) => { card.scrollTop = 0; });
    }

    if (screenshotDir && width >= 1366) {
      mkdirSync(screenshotDir, { recursive: true });
      await page.screenshot({ path: join(screenshotDir, `workspace-${width}.png`) });
    }
  }
  assert(fieldColumns[2560] > fieldColumns[1440], `Wide screens did not add field columns: ${JSON.stringify(fieldColumns)}`);

  // Dividers resize from the keyboard and reset on double-click.
  await page.setViewportSize({ width: 1440, height: 900 });
  await waitFor("the 1440px viewport", async () => (await page.evaluate(() => innerWidth)) === 1440, 5000);
  const inboxWidth = () => page.locator(".inbox-card").evaluate((card) => Math.round(card.getBoundingClientRect().width));
  const defaultInbox = await waitFor("the default inbox width", async () => {
    const width = await inboxWidth();
    return width < 360 ? width : null;
  }, 5000);
  await page.locator('[data-pane-resizer="inbox"]').focus();
  await page.keyboard.press("ArrowRight");
  await waitFor("the inbox divider to widen the inbox", async () => (await inboxWidth()) === defaultInbox + 16, 5000);
  assert.equal(JSON.parse(await page.evaluate(() => localStorage.getItem("invoice-studio-pane-sizes"))).inbox, defaultInbox + 16);
  await page.locator('[data-pane-resizer="inbox"]').dblclick();
  await waitFor("double-click to reset the inbox width", async () => (await inboxWidth()) === defaultInbox, 5000);

  // The sidebar toggle overrides the width-based default and is remembered.
  await page.locator("#sidebar-toggle").click();
  assert(!(await page.locator("#app-shell").getAttribute("class")).includes("nav-compact"), "Sidebar did not expand");
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.locator("#app-shell").waitFor({ state: "visible" });
  assert(!(await page.locator("#app-shell").getAttribute("class")).includes("nav-compact"), "Sidebar preference was not remembered");
  await page.locator("#sidebar-toggle").click();
  assert((await page.locator("#app-shell").getAttribute("class")).includes("nav-compact"), "Sidebar did not collapse");

  assert.deepEqual(pageErrors, [], `Page errors: ${pageErrors.join("; ")}`);
  console.log(`Workspace layout and time sort browser test passed (synthetic mocked jobs; ${baseURL}).`);
} finally {
  await browser.close();
}
