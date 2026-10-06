/**
 * Playwright check of the review form on rules jobs: the Buyer name shows the owner rule BUYER-NAME value
 * the target sheet uses, with its evidence and the reader's differing printed Bill To as a hint, and a save
 * without an edit keeps the stored reader value. Every job is mocked synthetic data at 1440 and 390 px.
 * SCREENSHOT_DIR=<dir> also saves screenshots.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/rules_review.browser.mjs
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

const OWNER = "Synthetic Owner Trading LLC";
const value = (v, evidence) => ({ value: v, evidence, flagged: false, reason: "" });
const printed = (reference, original = "") => [{ kind: "printed", source: "Printed on invoice", reference, original, rule: "BUYER-NAME", confidence: "Exact" }];
const ownerRule = [{ kind: "owner_rule", source: "owner rule BUYER-NAME (2026-10-05)", reference: "owner rule BUYER-NAME (2026-10-05)", original: "", rule: "BUYER-NAME", confidence: "Owner rule" }];

function rulesJob(id, buyerEvidence, readerBuyer) {
  const rules = {
    status: "Review",
    fields: {
      number: { label: "Invoice number", target: "Document", ...value(`SYN-RR-${id}`, printed("page 1")) },
      buyer_name: { label: "Buyer name", target: "Buyer Name", ...value(OWNER, buyerEvidence) },
    },
    lines: [],
    issues: [],
    item_lines: { resolved: 0, total: 0, rate: "1.0000", threshold: "0.95", owner_review: false, definition: "" },
    po_candidates: 0,
    revision: 1,
  };
  return {
    id: `rules-review-${id}`,
    filename: `SYNTHETIC-rules-${id}.png`,
    size: syntheticScan.length,
    created_at: `2026-10-05T1${id === "a" ? 1 : 0}:00:00+00:00`,
    status: "review",
    options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
    invoice: { number: `SYN-RR-${id}`, supplier_name: "Synthetic Supplier", buyer_name: readerBuyer, date: "2026-10-04", currency: "AED", net: 100, tax: 5, lines: [] },
    revision: 1,
    reviewed: false,
    trace: [],
    provenance: [],
    completeness: 0.8,
    selected_engine: "invoice2data",
    validation: { ready: false, source: "fine_rules", status: "Review", issues: [], matches: [], item_lines: rules.item_lines },
    rules,
    extraction_status: "fields_extracted",
    export_id: null,
    text: "SYNTHETIC DEMONSTRATION DATA",
    boxes: [],
  };
}

// A: the reader left Bill To empty and the owner entity is printed on page 2.
// B: the reader read another company as Bill To; Buyer Name comes from the owner rule.
const jobs = [rulesJob("a", printed("page 2", OWNER), null), rulesJob("b", ownerRule, "Synthetic Other Buyer Co")];

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce" });
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const baseline = await context.request.get(`${baseURL}/api/state`);
    assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
    const state = { ...(await baseline.json()), jobs, exports: [], accounts: [], active_account: null };
    const page = await context.newPage();
    await page.setViewportSize(viewport);
    page.setDefaultTimeout(timeout);
    const pageErrors = [];
    const consoleErrors = [];
    const saved = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error" && !/Failed to load resource/.test(message.text())) consoleErrors.push(message.text()); });
    await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
    await page.route("**/api/jobs/*", (route) => (route.request().method() === "GET"
      ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(jobs.find((job) => route.request().url().endsWith(job.id)) || jobs[0]) })
      : route.fallback()));
    await page.route("**/api/jobs/*/review", (route) => {
      const body = route.request().postDataJSON();
      saved.push(body);
      const job = jobs.find((item) => route.request().url().includes(`/${item.id}/`));
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...job, invoice: { ...job.invoice, ...body.invoice } }) });
    });
    await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));
    const tag = viewport.width;
    const buyer = page.locator('#header-fields [name="buyer_name"]');
    const buyerWrap = page.locator("#header-fields .field-wrap", { has: page.locator('[name="buyer_name"]') });
    const save = async () => {
      const count = saved.length;
      await page.locator("#save-review").click();
      await waitFor(async () => saved.length > count && !(await page.locator("#save-review").isDisabled()));
      return saved.at(-1);
    };

    await page.goto(baseURL, { waitUntil: "domcontentloaded" });
    await page.locator('[aria-label="Review SYNTHETIC-rules-a.png"]').click();
    await waitFor(async () => (await buyer.inputValue()) === OWNER);
    assert.equal((await buyerWrap.locator(".buyer-evidence").textContent()).trim(), "printed, page 2");
    assert.equal(await buyerWrap.locator(".buyer-printed").count(), 0, "a hint shows although the reader printed nothing");
    assert.equal((await save()).invoice.buyer_name, null, "saving without an edit wrote the rules buyer into the stored reader value");
    if (shots) await buyerWrap.screenshot({ path: `${shots}/buyer-printed-${tag}.png` });

    await page.locator('[aria-label="Review SYNTHETIC-rules-b.png"]').click();
    await waitFor(async () => (await buyerWrap.locator(".buyer-evidence").textContent())?.trim() === "owner rule BUYER-NAME");
    assert.equal(await buyer.inputValue(), OWNER);
    assert.equal((await buyerWrap.locator(".buyer-printed").textContent()).trim(), "printed: Synthetic Other Buyer Co");
    const panel = page.locator("#rules-fields dt", { hasText: "Buyer name" }).locator("xpath=following-sibling::dd[1]");
    assert.equal((await panel.locator("small").first().textContent()).trim(), "owner rule BUYER-NAME (2026-10-05)", "the rules panel repeats the owner rule");
    assert.equal((await save()).invoice.buyer_name, "Synthetic Other Buyer Co", "saving without an edit replaced the reader's printed Bill To");
    await buyer.fill("Synthetic Edited Buyer");
    assert.equal((await save()).invoice.buyer_name, "Synthetic Edited Buyer", "the reviewer's edit was not saved");
    if (shots) await buyerWrap.screenshot({ path: `${shots}/buyer-owner-rule-${tag}.png` });

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `Page scrolls horizontally by ${overflow}px at ${tag}px`);
    assert.deepEqual(pageErrors, []);
    assert.deepEqual(consoleErrors, []);
    await page.close();
  }
  console.log(`Rules review browser test passed (buyer display; synthetic mocked jobs at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}

async function waitFor(check) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((done) => setTimeout(done, 50));
  }
  throw new Error("Timed out waiting for the review form");
}
