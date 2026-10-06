/**
 * Playwright check of the review form on rules jobs: the Buyer name shows the owner rule BUYER-NAME value
 * the target sheet uses, with its evidence and the reader's differing printed Bill To as a hint, and a save
 * without an edit keeps the stored reader value. When the rules leave several supplier codes, the reviewer picks
 * one (decision 41): the pick posts supplier_code with confirm false, the candidates stay listed, the site the rules
 * fill from it is not a typed entry, and "Clear pick" posts "". Every job is mocked synthetic data at 1440 and 390 px.
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

// C: R-006 left three supplier codes for the printed supplier name.
const PICK_ID = "rules-review-c";
const site = (supplier_site, site_name) => ({ supplier_site, site_name, entity: "SYN-ENTITY", reference: `${supplier_site}|2026-10-05` });
const candidate = (code, ...sites) => ({ supplier_code: code, rule: "R-006", reason: "3 synthetic supplier codes serve this location",
  location: "SYN-LOC", location_reference: "LOCATIONS SYN-LOC|2026-10-05", sites });
const CANDIDATES = [
  candidate("99001", site("SYN-SITE-99001", "Synthetic North Site")),
  candidate("99002", site("SYN-SITE-99002", "Synthetic Harbour Site"), site("SYN-SITE-99002B", "Synthetic Harbour Annex")),
  candidate("99003", site("SYN-SITE-99003", "Synthetic Very Long Regional Distribution Centre Site Name For Wrapping Checks")),
];

function pickerJob(picked) {
  const job = rulesJob("c", printed("page 1"), null);
  const siteField = picked
    ? { label: "Supplier site", target: "Supplier Site", value: `SYN-SITE-${picked}`, flagged: false, reason: "",
      evidence: [{ kind: "owner_entry", source: `Item Master SUPPLIER via SUPPLIER SITES of the owner-picked Supplier CODE ${picked}`,
        reference: `SYN-SITE-${picked}|2026-10-05`, original: `SYN-SITE-${picked}`, rule: "OWNER-PICK", confidence: "Owner entry" }] }
    : { label: "Supplier site", target: "Supplier Site", value: null, evidence: [], flagged: true, reason: "Not found" };
  job.rules.fields = { number: job.rules.fields.number, site: siteField, buyer_name: job.rules.fields.buyer_name };
  job.rules.supplier_site_candidates = CANDIDATES;
  return picked ? { ...job, owner_supplier_code: picked } : job;
}

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce" });
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const baseline = await context.request.get(`${baseURL}/api/state`);
    assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
    const state = { ...(await baseline.json()), jobs: [...jobs, pickerJob("")], exports: [], accounts: [], active_account: null };
    const page = await context.newPage();
    await page.setViewportSize(viewport);
    page.setDefaultTimeout(timeout);
    const pageErrors = [];
    const consoleErrors = [];
    const saved = [];
    let picked = "";
    let rejectPick = true;
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error" && !/Failed to load resource/.test(message.text())) consoleErrors.push(message.text()); });
    await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
    await page.route("**/api/jobs/*", (route) => (route.request().method() === "GET"
      ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(route.request().url().endsWith(PICK_ID)
        ? pickerJob(picked) : jobs.find((job) => route.request().url().endsWith(job.id)) || jobs[0]) })
      : route.fallback()));
    await page.route("**/api/jobs/*/review", (route) => {
      const body = route.request().postDataJSON();
      saved.push(body);
      if (route.request().url().includes(`/${PICK_ID}/`)) {
        // The first 99003 pick is refused the way the server refuses a code the rules no longer offer.
        if (body.supplier_code === "99003" && rejectPick) {
          rejectPick = false;
          return route.fulfill({ status: 400, contentType: "application/json", body: JSON.stringify({ detail: "Supplier code is not one of the candidates the rules offered" }) });
        }
        if (typeof body.supplier_code === "string") picked = body.supplier_code;
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(pickerJob(picked)) });
      }
      const job = jobs.find((item) => route.request().url().includes(`/${item.id}/`));
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...job, invoice: { ...job.invoice, ...body.invoice } }) });
    });
    await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));
    const tag = viewport.width;
    const buyer = page.locator('#header-fields [name="buyer_name"]');
    const buyerWrap = page.locator("#header-fields .field-wrap", { has: page.locator('[name="buyer_name"]') });
    const post = async (target) => {
      const count = saved.length;
      await target.click();
      await waitFor(async () => saved.length > count && !(await page.locator("#save-review").isDisabled()));
      return saved.at(-1);
    };
    const save = () => post(page.locator("#save-review"));

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

    const sitePanel = page.locator("#rules-fields dt", { hasText: "Supplier site" }).locator("xpath=following-sibling::dd[1]");
    const options = sitePanel.locator(".supplier-pick-option");
    const pressed = () => options.evaluateAll((buttons) => buttons.map((button) => button.getAttribute("aria-pressed")));
    const hint = async () => (await sitePanel.locator(".supplier-pick > small").textContent()).trim();
    await page.locator('[aria-label="Review SYNTHETIC-rules-c.png"]').click();
    await waitFor(async () => (await options.count()) === 3);
    assert.equal(await hint(), "3 supplier codes match this supplier · pick the right one");
    assert.deepEqual(await pressed(), ["false", "false", "false"]);
    assert.equal((await options.nth(1).textContent()).trim(), "99002Synthetic Harbour Site +1 more");
    assert.equal(await sitePanel.locator(".supplier-pick-clear").count(), 0, "Clear pick shows before any pick");
    await page.evaluate(() => { document.querySelector("#confirm-review").checked = true; });

    let body = await post(options.nth(2));
    assert.equal(body.supplier_code, "99003");
    await waitFor(async () => /not one of the candidates/.test(await page.locator("#global-message").textContent()));
    await waitFor(async () => !(await options.nth(2).isDisabled()));
    assert.deepEqual(await pressed(), ["false", "false", "false"], "a refused pick shows as picked");

    body = await post(options.nth(1));
    assert.equal(body.supplier_code, "99002");
    assert.equal(body.confirm, false, "a pick confirmed the review");
    assert.equal(body.revision, 1);
    assert.equal(body.invoice.number, "SYN-RR-c");
    await waitFor(async () => (await pressed()).join() === "false,true,false");
    assert(await options.nth(1).evaluate((button) => button.classList.contains("picked")));
    assert.equal(await hint(), "Supplier code 99002 picked at review · the rules filled the site from it");
    assert.equal((await sitePanel.locator("xpath=./span[1]").textContent()).trim(), "SYN-SITE-99002");
    assert.equal((await sitePanel.locator("xpath=./small[1]").textContent()).trim(), "from the reviewer's supplier-code pick (OWNER-PICK)");
    assert.equal(await sitePanel.locator(".rules-entry").count(), 0, "the picked site shows as a typed reviewer entry");
    if (shots) await sitePanel.screenshot({ path: `${shots}/supplier-pick-${tag}.png` });
    const count = saved.length;
    await options.nth(1).click();
    await page.waitForTimeout(300);
    assert.equal(saved.length, count, "clicking the current pick posted again");

    body = await save();
    assert.equal("supplier_code" in body, false, "a plain save sent a supplier code");
    assert.equal(body.entries.header.site, undefined, "a plain save sent the picked site as a typed entry");
    await waitFor(async () => (await pressed()).join() === "false,true,false");

    body = await post(sitePanel.locator(".supplier-pick-clear"));
    assert.equal(body.supplier_code, "");
    assert.equal(body.entries.header.site, undefined, "Clear pick sent the picked site as a typed entry");
    await waitFor(async () => (await sitePanel.locator(".supplier-pick-clear").count()) === 0);
    assert.deepEqual(await pressed(), ["false", "false", "false"]);
    assert.equal(await hint(), "3 supplier codes match this supplier · pick the right one");
    assert.equal((await sitePanel.locator("xpath=./span[1]").textContent()).trim(), "Not found in owner sheets or on the invoice");

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `Page scrolls horizontally by ${overflow}px at ${tag}px`);
    assert.deepEqual(pageErrors, []);
    assert.deepEqual(consoleErrors, []);
    await page.close();
  }
  console.log(`Rules review browser test passed (buyer display, supplier-code pick; synthetic mocked jobs at 1440 and 390 px; ${baseURL}).`);
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
