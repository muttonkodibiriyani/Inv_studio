/**
 * Playwright check that a fine-rules job shows one rules result with evidence:
 * values with their sheet/printed evidence, empty-and-flagged values, the item-line
 * rate with the owner-review notice, and the banner. Every job is mocked synthetic data.
 *
 *   BASE_URL=http://127.0.0.1:8765 node tests/rules_wiring.browser.mjs
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

const sheet = (reference) => [{ kind: "sheet", source: "Item Master", reference, original: "", rule: "V-001", confidence: "Exact" }];
const printed = [{ kind: "printed", source: "Printed on invoice", reference: "page 1 box [0.1, 0.2, 0.3, 0.25]", original: "Net 100.00", rule: "", confidence: "Exact" }];
const value = (v, evidence) => ({ value: v, evidence, flagged: false, reason: "" });
const missing = { value: null, evidence: [], flagged: true, reason: "Not found" };
const field = (label, target, cell) => ({ label, target, ...cell });

const rules = {
  status: "Review",
  fields: {
    number: field("Invoice number", "Document", value("SYN-RULES-1", printed)),
    site: field("Supplier site", "Supplier Site", value("900001", sheet("Supplier Sites!4"))),
    po: field("Purchase order", "Order No", missing),
    location: field("Delivery location", "Location", value("901", sheet("Locations!7"))),
    location_type: field("Location type", "Location Type", value("Store (S)", sheet("Locations!7"))),
    date: field("Invoice date", "Document Date", value("10/4/2026", printed)),
    currency: field("Currency", "Currency", value("AED", printed)),
    market: field("Market", "Market", value("AE", [{ kind: "table", source: "Market table", reference: "SYN|v1", original: "", rule: "V-010", confidence: "Exact" }])),
    taxCode: field("Tax code", "Tax Code", value("S", sheet("Item Master!2"))),
    net: field("Net total", "Net Amount", value("100.00", printed)),
    tax: field("Tax total", "Tax Amount", value("5.00", printed)),
  },
  lines: [1, 2].map((n) => ({
    line: n,
    cells: {
      Item: n === 1 ? value("345000001", sheet("Item Master!2")) : missing,
      UPC: n === 1 ? value("00012345678905", sheet("Item Master!2")) : missing,
      "Unit Cost": value("10.00", printed),
      Quantity: value("5", printed),
      "Unit Tax Code": value("S", sheet("Item Master!2")),
    },
    status: "",
    source_row: "",
    match_method: "",
  })),
  issues: [],
  item_lines: { resolved: 1, total: 2, rate: "0.5000", threshold: "0.95", owner_review: true, definition: "Synthetic definition of a resolved line" },
  po_candidates: 3,
  revision: 1,
};

const job = {
  id: "rules-wiring-a",
  filename: "SYNTHETIC-rules-a.png",
  size: syntheticScan.length,
  created_at: "2026-10-05T10:00:00+00:00",
  status: "review",
  options: { engine: "auto", ai_fallback: false, provider: "openai", model: "", language: "en" },
  invoice: { number: "SYN-RULES-1", supplier_name: "Synthetic Supplier", seller: "SYN-S", buyer: "SYN-B", origin: "AE", site: "", po: "", location: "", date: "2026-10-04", currency: "AED", net: 100, tax: 5, lines: [{ sku: "SYN-1", qty: 5, price: 10 }, { sku: "SYN-2", qty: 5, price: 10 }] },
  revision: 1,
  reviewed: false,
  trace: [],
  provenance: [],
  completeness: 0.8,
  selected_engine: "invoice2data",
  validation: {
    ready: false,
    source: "fine_rules",
    status: "Review",
    issues: [
      { code: "Owner Review", message: "Item lines resolved 1/2: below 95%, owner review required", owner: "Owner", line: null, rule: "ITEM-95", blocking: true },
      { code: "Missing/Ambiguous PO", message: "3 orders carry every resolved item; owner review", owner: "Buyer", line: null, rule: "POG-001", check: "C-12", type: "Ambiguous PO", blocking: true },
      { code: "Missing Evidence", message: "Purchase order not found in the owner's sheets or printed on the invoice", owner: "Accounts payable", line: null, rule: "EVIDENCE", blocking: true },
      { code: "Evidence Disagreement", message: "number: OCR read differently; value kept, check the document", owner: "Accounts payable", line: null, rule: "EVID-OCR", blocking: false },
    ],
    matches: [{ line: 1, item: "345000001", gtin: "00012345678905" }, { line: 2, item: "", gtin: "" }],
    item_lines: rules.item_lines,
    location_type: "Store (S)",
  },
  rules,
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
  // One context: --single-process Chromium does not survive opening a second one.
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC", locale: "en-US", reducedMotion: "reduce" });
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const baseline = await context.request.get(`${baseURL}/api/state`);
    assert(baseline.ok(), `Baseline state failed with HTTP ${baseline.status()}`);
    const state = { ...(await baseline.json()), jobs: [job], exports: [], accounts: [], active_account: null };
    const page = await context.newPage();
    await page.setViewportSize(viewport);
    page.setDefaultTimeout(timeout);
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.route("**/api/state", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }));
    await page.route("**/api/jobs/*", (route) => (route.request().method() === "GET"
      ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) })
      : route.fallback()));
    await page.route("**/api/jobs/*/document", (route) => route.fulfill({ status: 200, contentType: "image/png", body: syntheticScan }));

    await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
    await page.locator("#app-shell").waitFor({ state: "visible" });
    await page.locator('[aria-label="Review SYNTHETIC-rules-a.png"]').click();
    await page.locator("#rules-fieldset").waitFor({ state: "visible" });

    const fields = page.locator("#rules-fields");
    assert.match(await fields.textContent(), /Supplier site900001owner sheet Supplier Sites!4 \(V-001\)/);
    assert.match(await fields.textContent(), /Net total100\.00printed on invoice · page 1 box/);
    assert.match(await fields.textContent(), /Purchase orderNot found in owner sheets or on the invoiceAmbiguous: 3 candidate orders — owner review/);
    assert.match(await fields.textContent(), /MarketAEMarket table · SYN\|v1/);
    const rate = page.locator("#rules-item-rate");
    assert.match(await rate.textContent(), /Item lines resolved 1\/2 \(50\.0%\) — below 95%: this invoice goes to owner review\./);
    assert.equal(await rate.getAttribute("title"), "Synthetic definition of a resolved line");
    assert(await rate.evaluate((node) => node.classList.contains("owner-review")));
    const secondLine = page.locator("#rules-lines tr").nth(1);
    assert.match(await secondLine.textContent(), /^2Not foundNot found10\.00/);

    for (const name of ["seller", "buyer", "origin", "site", "location", "market", "taxCode"]) {
      assert.equal(await page.locator(`#header-fields [name="${name}"]`).count(), 0, `${name} is still an editable input`);
    }
    const banner = await page.locator("#validation-summary").textContent();
    assert.match(banner, /Owner Review: Item lines resolved 1\/2/);
    assert.match(banner, /Missing\/Ambiguous PO \[C-12 · Ambiguous PO\]: 3 orders/);
    assert.match(banner, /Evidence Disagreement: number: .*\(warning\)/);
    assert.match(await page.locator("#provenance-list").textContent(), /owner sheet Supplier Sites!4/);

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflow <= 1, `Page scrolls horizontally by ${overflow}px at ${viewport.width}px`);
    assert.deepEqual(pageErrors, []);
    await page.close();
  }
  console.log(`Rules wiring browser test passed (synthetic mocked job at 1440 and 390 px; ${baseURL}).`);
} finally {
  await browser.close();
}
