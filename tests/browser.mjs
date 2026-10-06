/**
 * Browser smoke test for the real Invoice Studio FastAPI application.
 *
 * Dependencies:
 *   npm install --save-dev playwright@^1.62.0
 *   npx playwright install chromium
 *   # Linux CI/container hosts can install the browser libraries at the same time:
 *   npx playwright install --with-deps chromium
 *
 * Run the app on http://127.0.0.1:8765, then run:
 *   node tests/browser.mjs
 *
 * Override the defaults with BASE_URL, BROWSER_TIMEOUT_MS, or HEADFUL=1.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require("playwright"));
} catch (error) {
  throw new Error(
    "Playwright is required. Run `npm install --save-dev playwright@^1.62.0` " +
      "and `npx playwright install chromium`.",
    { cause: error },
  );
}

const baseURL = (process.env.BASE_URL || "http://127.0.0.1:8765").replace(/\/$/, "");
const timeout = Number(process.env.BROWSER_TIMEOUT_MS || 120_000);
const headless = process.env.HEADFUL !== "1";
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

async function responseBody(response) {
  try {
    return await response.text();
  } catch {
    return "<unavailable>";
  }
}

async function assertOk(response, label) {
  assert(response, `${label} did not produce an HTTP response`);
  if (!response.ok()) {
    assert.fail(`${label} failed with HTTP ${response.status()}: ${await responseBody(response)}`);
  }
}

async function waitFor(description, check, waitMs = timeout) {
  const deadline = Date.now() + waitMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const result = await check();
      if (result) return result;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 350));
  }
  const suffix = lastError ? ` Last error: ${lastError.message}` : "";
  throw new Error(`Timed out waiting for ${description}.${suffix}`);
}

const browser = await chromium.launch({
  headless,
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});
const context = await browser.newContext({ acceptDownloads: true });
const page = await context.newPage();
page.setDefaultTimeout(timeout);

const pageErrors = [];
const serverErrors = [];
const requestLog = [];
page.on("pageerror", (error) => pageErrors.push(error.message));
page.on("request", (request) => {
  requestLog.push({ method: request.method(), path: new URL(request.url()).pathname });
});
page.on("response", (response) => {
  if (response.status() >= 500) {
    serverErrors.push(`${response.request().method()} ${response.url()} -> ${response.status()}`);
  }
});

const responseMatches = (method, path) => (response) =>
  response.request().method() === method && new URL(response.url()).pathname === path;

async function runPreflight(start, processPath, label) {
  const processCountBefore = requestLog.filter(
    (entry) => entry.method === "POST" && entry.path === processPath,
  ).length;
  const sequenceStart = requestLog.length;
  const planPromise = page.waitForResponse(responseMatches("POST", "/api/preflight"), { timeout });
  await start();
  const planResponse = await planPromise;
  await assertOk(planResponse, `${label} preflight`);

  const dialog = page.locator("#preflight-dialog");
  await dialog.waitFor({ state: "visible", timeout });
  assert((await page.locator("#preflight-summary").textContent())?.trim(), `${label} plan is empty`);
  assert(
    (await page.locator("#preflight-warnings li").count()) > 0,
    `${label} preflight did not render its warnings`,
  );
  assert.equal(
    requestLog.filter((entry) => entry.method === "POST" && entry.path === processPath).length,
    processCountBefore,
    `${label} processing started before explicit confirmation`,
  );

  const confirmPromise = page.waitForResponse(
    responseMatches("POST", "/api/preflight/confirm"),
    { timeout },
  );
  const processPromise = page.waitForResponse(responseMatches("POST", processPath), { timeout });
  await page.locator("#confirm-preflight").click();
  const confirmResponse = await confirmPromise;
  await assertOk(confirmResponse, `${label} preflight confirmation`);
  const processResponse = await processPromise;
  await assertOk(processResponse, label);

  const sequence = requestLog.slice(sequenceStart);
  const confirmIndex = sequence.findIndex(
    (entry) => entry.method === "POST" && entry.path === "/api/preflight/confirm",
  );
  const processIndex = sequence.findIndex(
    (entry) => entry.method === "POST" && entry.path === processPath,
  );
  assert(confirmIndex >= 0, `${label} did not confirm its preflight plan`);
  assert(
    processIndex > confirmIndex,
    `${label} processing request did not follow preflight confirmation`,
  );
  await dialog.waitFor({ state: "hidden", timeout });
  return processResponse;
}

async function waitForCompletedJob(jobId) {
  return waitFor(`job ${jobId} to finish extracting`, async () => {
    const response = await context.request.get(`${baseURL}/api/jobs/${jobId}`);
    await assertOk(response, `Polling job ${jobId}`);
    const job = await response.json();
    if (job.status === "error") throw new Error(job.error || `Job ${jobId} failed`);
    return ["review", "ready"].includes(job.status) ? job : null;
  });
}

async function waitForSelectedReview(expectedFilename) {
  await page.locator("#review-panel").waitFor({ state: "visible", timeout });
  await waitFor(`${expectedFilename} to render for review`, async () => {
    const filename = (await page.locator("#selected-filename").textContent())?.trim();
    const status = (await page.locator("#selected-status").textContent())?.trim().toLowerCase();
    const complete = status && !["queued", "processing", "reading", "waiting"].some(
      (word) => status.includes(word),
    );
    return filename === expectedFilename && complete &&
      (await page.locator("#header-fields input").count()) > 0 &&
      (await page.locator("#line-items tr").count()) > 0;
  });
}

async function saveConfirmedReview(jobId, invoiceNumber) {
  await page.locator('#header-fields input[name="number"]').fill(invoiceNumber);
  await page.locator("#confirm-review").check();
  const responsePromise = page.waitForResponse(
    responseMatches("POST", `/api/jobs/${jobId}/review`),
    { timeout },
  );
  await page.getByRole("button", { name: "Save & revalidate" }).click();
  const response = await responsePromise;
  await assertOk(response, `Confirmed review for ${invoiceNumber}`);
  const job = await response.json();
  assert.equal(job.reviewed, true, `${invoiceNumber} was not recorded as reviewed`);
  assert.equal(
    job.validation.ready,
    true,
    `${invoiceNumber} did not validate: ${JSON.stringify(job.validation.issues)}`,
  );
  await waitFor(`${invoiceNumber} export readiness`, async () =>
    page.locator("#export-job").isEnabled(),
  );
  return job;
}

try {
  let releaseConfig;
  const configGate = new Promise((resolve) => { releaseConfig = resolve; });
  await page.route("**/api/public-config", async (route) => { await configGate; await route.continue(); });
  const home = await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
  await page.locator("#auth-gate").waitFor({ state: "visible", timeout });
  assert(await page.locator("#app-shell").isHidden(), "Workspace appeared before access configuration was verified");
  releaseConfig();
  await page.locator("#app-shell").waitFor({ state: "visible", timeout });
  await page.unroute("**/api/public-config");
  await assertOk(home, "Invoice Studio page load");
  await page.getByRole("heading", { level: 1, name: "Review workspace" }).waitFor();

  // The shell exposes the three major semantic regions expected by keyboard and
  // assistive-technology users.
  assert(await page.locator("header").isVisible(), "The application header is not visible");
  assert(
    await page.locator('aside[aria-label="Primary navigation"]').isVisible(),
    "Primary navigation is not visible",
  );
  assert(await page.getByRole("main").isVisible(), "The main content region is not visible");
  assert(await page.locator("#job-list").isVisible() || await page.locator("#job-empty").isVisible(), "Neither the invoice list nor its empty state is visible");
  assert(
    await page.getByRole("region", { name: "Invoice review" }).isVisible(),
    "The invoice review region is not visible",
  );
  const initialStateResponse = await context.request.get(`${baseURL}/api/state`);
  await assertOk(initialStateResponse, "Initial workspace state");
  const initialJobCount = (await initialStateResponse.json()).jobs.length;

  // The isolated browser workspace starts without matching data after a clean
  // backend restart. Load only the bundled synthetic set used by this test.
  const demoReferences = await context.request.post(`${baseURL}/api/references/demo`, {
    data: {},
    headers: { "x-studio-request": "1" },
  });
  await assertOk(demoReferences, "Synthetic reference setup");

  const demoResponse = await runPreflight(
    () => page.getByRole("button", { name: "Try synthetic demo" }).click(),
    "/api/demo",
    "Synthetic demo creation",
  );
  const demoJob = await demoResponse.json();
  assert.match(demoJob.id, /^[a-f0-9]{32}$/, "The demo response did not include a job id");
  await waitForCompletedJob(demoJob.id);
  await waitForSelectedReview(demoJob.filename);
  await page.getByRole("heading", { name: "Invoice details" }).waitFor();

  const demoDocumentPath = `/api/jobs/${demoJob.id}/document`;
  const initialDocumentSource = await waitFor("the source preview to load", async () => {
    const source = await page.locator("#document-frame").getAttribute("src");
    return source?.startsWith("blob:") ? source : null;
  });
  const documentRequestsBeforeRetry = requestLog.filter(
    (entry) => entry.method === "GET" && entry.path === demoDocumentPath,
  ).length;

  // A reader may recover text without structured invoice fields. Present that
  // as a clear next-step state and keep the complete issue list collapsed.
  await page.evaluate(({ id, filename }) => renderSelectedJob({
    id,
    filename,
    size: 100,
    created_at: new Date().toISOString(),
    status: "review",
    selected_engine: "paddleocr",
    completeness: 0,
    invoice: { lines: [] },
    text: "Readable document text without mapped invoice fields",
    validation: { ready: false, matches: [], issues: Array.from({ length: 20 }, (_, index) => ({
      code: "FIELD",
      message: `Missing field ${index + 1}`,
      owner: "Invoice reviewer",
    })) },
    trace: [],
    provenance: [],
  }), { id: demoJob.id, filename: demoJob.filename });
  await page.locator("#empty-extraction").waitFor({ state: "visible", timeout });
  assert.equal(await page.locator("#empty-extraction-title").textContent(), "Text read, invoice fields not extracted");
  assert(await page.locator("#completeness").isHidden(), "Empty extraction still displayed a misleading 0% completeness");
  assert.equal(await page.locator("#selected-status").textContent(), "Needs review");
  assert(await page.locator("#selected-status").evaluate((element) => element.classList.contains("warning")), "Empty extraction was styled as successful");
  assert.equal(await page.locator(".validation-details").getAttribute("open"), null, "Validation issue wall was expanded by default");
  assert.match(await page.locator(".validation-details summary").textContent(), /20 issues to resolve/);
  assert.equal(await page.locator(".validation-details li").count(), 20, "Collapsed validation omitted issue details");
  await page.locator("#empty-show-text").click();
  assert(await page.locator("#extracted-text-wrap").evaluate((element) => element.open), "Raw-text action did not reveal extracted text");
  await page.evaluate((id) => selectJob(id), demoJob.id);
  await waitForSelectedReview(demoJob.filename);

  // Ambiguous printed dates remain visible source evidence while the canonical
  // HTML date control stays blank until a reviewer chooses the calendar date.
  await page.evaluate(() => {
    const printedDateJob = structuredClone(app.currentJob);
    printedDateJob.invoice.date_printed = "03-08-2026";
    printedDateJob.invoice.date = null;
    renderSelectedJob(printedDateJob);
  });
  const printedDate = page.locator('#header-fields input[name="date_printed"]');
  const canonicalDate = page.locator('#header-fields input[name="date"]');
  assert.equal(await printedDate.inputValue(), "03-08-2026");
  assert.equal(await printedDate.getAttribute("readonly"), "");
  assert.equal(await canonicalDate.inputValue(), "");
  assert.match(
    await canonicalDate.locator("xpath=..").locator("small").textContent(),
    /day\/month ambiguous.*choose the intended calendar date/i,
  );
  await canonicalDate.fill("2026-08-03");
  assert.equal(await printedDate.inputValue(), "03-08-2026", "Choosing a date changed the printed source value");
  await page.evaluate((id) => selectJob(id), demoJob.id);
  await waitForSelectedReview(demoJob.filename);

  // Reprocessing is separately gated, even when the document is already local.
  await page.locator("#retry-job").click();
  await page.locator("#retry-dialog").waitFor({ state: "visible" });
  assert(await page.locator("#retry-prefer-native-text").isChecked(), "Native PDF text was not enabled by default");
  await page.locator("#retry-prefer-native-text").locator("..").click();
  assert.equal(await page.locator("#retry-prefer-native-text").isChecked(), false);
  const retryPreflightsBeforeCancel = requestLog.filter(
    (entry) => entry.method === "POST" && entry.path === "/api/preflight",
  ).length;
  await page.locator("#retry-model").fill("synthetic-cancel-model");
  await page.locator("#retry-dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  await page.locator("#retry-dialog").waitFor({ state: "hidden" });
  await page.waitForTimeout(100);
  assert.equal(
    requestLog.filter((entry) => entry.method === "POST" && entry.path === "/api/preflight").length,
    retryPreflightsBeforeCancel,
    "Retry Cancel submitted the retry form",
  );

  await page.locator("#retry-job").click();
  await page.locator("#retry-dialog").waitFor({ state: "visible" });
  assert.equal(await page.locator("#retry-prefer-native-text").isChecked(), false, "Retry lost the native-text preference");
  await page.locator("#retry-model").fill("synthetic-close-model");
  await page.locator("#retry-dialog").getByRole("button", { name: "Close", exact: true }).click();
  await page.locator("#retry-dialog").waitFor({ state: "hidden" });
  await page.waitForTimeout(100);
  assert.equal(
    requestLog.filter((entry) => entry.method === "POST" && entry.path === "/api/preflight").length,
    retryPreflightsBeforeCancel,
    "Retry Close submitted the retry form",
  );

  await page.locator("#retry-job").click();
  await page.locator("#retry-dialog").waitFor({ state: "visible" });
  assert.equal(await page.locator("#retry-prefer-native-text").isChecked(), false, "Retry preference changed before preflight");
  const retryPlanRequestPromise = page.waitForRequest((request) =>
    request.method() === "POST" && new URL(request.url()).pathname === "/api/preflight",
  );
  const retryResponse = await runPreflight(
    () => page.locator("#retry-dialog").getByRole("button", { name: "Reprocess" }).click(),
    `/api/jobs/${demoJob.id}/retry`,
    "Invoice retry",
  );
  const retryPlanRequest = await retryPlanRequestPromise;
  assert.equal(retryPlanRequest.postDataJSON().options.prefer_native_text, false);
  const retriedJob = await retryResponse.json();
  assert.equal(retriedJob.id, demoJob.id, "Retry returned a different invoice job");
  assert.equal(retriedJob.options.prefer_native_text, false, "Retry did not retain its confirmed native-text rule");
  await waitForCompletedJob(demoJob.id);
  await waitForSelectedReview(demoJob.filename);
  assert.equal(
    await page.locator("#document-frame").getAttribute("src"),
    initialDocumentSource,
    "Status polling replaced the source preview and can make it blink",
  );
  assert.equal(
    requestLog.filter((entry) => entry.method === "GET" && entry.path === demoDocumentPath).length,
    documentRequestsBeforeRetry,
    "Reprocessing or status polling downloaded the unchanged source document again",
  );

  // A description-only line can search staged evidence. Ambiguous identifiers
  // require an explicit selection and copying them does not approve or save it.
  const firstReviewLine = page.locator("#line-items tr").first();
  const lookupDescription = await firstReviewLine.locator('[name="description"]').inputValue();
  const originalLineIdentity = {
    item_id: await firstReviewLine.locator('[name="item_id"]').inputValue(),
    sku: await firstReviewLine.locator('[name="sku"]').inputValue(),
    gtin: await firstReviewLine.locator('[name="gtin"]').inputValue(),
    uom: await firstReviewLine.locator('[name="uom"]').inputValue(),
    price: await firstReviewLine.locator('[name="price"]').inputValue(),
    qty: await firstReviewLine.locator('[name="qty"]').inputValue(),
  };
  const currentInvoicePO = await page.locator('#header-fields [name="po"]').inputValue();
  assert(lookupDescription.length >= 2, "Demo line has no description for item lookup");
  const lookupRoute = /\/api\/reference-lookup\/(search|products)\?/;
  const lookupRequests = [];
  await page.route(lookupRoute, async (route) => {
    const requestURL = new URL(route.request().url());
    lookupRequests.push(requestURL);
    const isPO = requestURL.searchParams.get("kind") === "po";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        records: isPO ? [] : [{
          kind: "item",
          source_hash: "browser-test-source",
          source_sheet: "ItemMaster",
          source_row: 8,
          data: { Description: lookupDescription, VPN: "ITEM-LOOKUP", ITEM_PARENT: "INTERNAL-100", UPC: "629000000002" },
          flags: [],
          candidate_fields: {
            sku: ["ITEM-LOOKUP"],
            internal_item: ["INTERNAL-100"],
            gtin: ["629000000001", "629000000002"],
            uom: ["EA"],
            pack: ["12"],
            description: [lookupDescription],
            supplier: ["Browser supplier"],
            site: ["DXB"],
            po: [],
          },
          candidate_field_sources: {
            sku: [{ column: "VPN", value: "ITEM-LOOKUP" }],
            internal_item: [{ column: "ITEM_PARENT", value: "INTERNAL-100" }],
            gtin: [{ column: "UPC", value: "629000000001" }, { column: "UPC", value: "629000000002" }],
            uom: [{ column: "UOM", value: "EA" }],
          },
          match: { basis: ["description_tokens"], score: 0.98, matched_terms: [lookupDescription] },
          approved_for_matching: false,
          requires_confirmation: true,
        }],
        next_cursor: null,
        notice: "Source evidence only.",
      }),
    });
  });
  await firstReviewLine.getByRole("button", { name: "Find item" }).click();
  await page.locator("#references-section:not([hidden])").waitFor({ state: "visible", timeout });
  assert.equal(await page.locator("#reference-lookup-query").inputValue(), lookupDescription);
  const candidate = page.locator(".reference-result").first();
  await candidate.locator("summary").click();
  assert.equal(await candidate.locator('[data-candidate-field="sku"]').inputValue(), "ITEM-LOOKUP");
  assert.equal(await candidate.locator('[data-candidate-field="gtin"]').inputValue(), "", "Ambiguous GTIN was chosen automatically");
  await candidate.locator('[data-candidate-field="gtin"]').selectOption("629000000002");
  assert(await candidate.getByRole("button", { name: "Use selected item" }).isDisabled(), "Candidate applied without human confirmation");
  await candidate.locator('.candidate-confirm').check();
  await candidate.getByRole("button", { name: "Use selected item" }).click();
  await page.locator("#workspace-section:not([hidden])").waitFor({ state: "visible", timeout });
  assert.equal(await firstReviewLine.locator('[name="sku"]').inputValue(), "ITEM-LOOKUP");
  assert.equal(await firstReviewLine.locator('[name="item_id"]').inputValue(), "INTERNAL-100");
  assert.equal(await firstReviewLine.locator('[name="gtin"]').inputValue(), "629000000002");
  assert.equal(await firstReviewLine.locator('[name="uom"]').inputValue(), "EA");
  assert.equal(await firstReviewLine.locator('[name="price"]').inputValue(), originalLineIdentity.price);
  assert.equal(await firstReviewLine.locator('[name="qty"]').inputValue(), originalLineIdentity.qty);
  assert((await firstReviewLine.locator('[name="evidence"]').inputValue()).includes("Human-confirmed reference:"));
  await firstReviewLine.locator('[name="sku"]').fill(originalLineIdentity.sku);
  await firstReviewLine.locator('[name="item_id"]').fill(originalLineIdentity.item_id);
  await firstReviewLine.locator('[name="gtin"]').fill(originalLineIdentity.gtin);
  await firstReviewLine.locator('[name="uom"]').fill(originalLineIdentity.uom);

  // A selected, source-proven GTIN can lead to PO/GRN evidence with the
  // invoice PO as an exact filter. This is a new search, not an automatic join.
  await firstReviewLine.getByRole("button", { name: "Find item" }).click();
  const itemCandidate = page.locator(".reference-result").first();
  await itemCandidate.locator("summary").click();
  await itemCandidate.locator('[data-candidate-field="gtin"]').selectOption("629000000001");
  const poSearchResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET" && url.pathname === "/api/reference-lookup/search" && url.searchParams.get("kind") === "po";
  }, { timeout });
  await itemCandidate.getByRole("button", { name: "Find PO/GRN rows" }).click();
  await assertOk(await poSearchResponse, "PO/GRN evidence search");
  const poSearch = lookupRequests.findLast((url) => url.searchParams.get("kind") === "po");
  assert.equal(poSearch?.searchParams.get("q"), "629000000001");
  assert.equal(poSearch?.searchParams.get("po"), currentInvoicePO);
  await page.getByRole("button", { name: "Workspace", exact: true }).click();

  // A manual draft is an explicit, separate workbook path. It may copy the
  // visible values, but must not revise, approve or export the active job.
  const beforeDraft = await (await context.request.get(`${baseURL}/api/jobs/${demoJob.id}`)).json();
  await page.getByRole("button", { name: "Create unvalidated draft" }).click();
  const draftDialog = page.locator("#manual-draft-dialog");
  await draftDialog.waitFor({ state: "visible", timeout });
  assert(
    (await draftDialog.textContent())?.includes("DRAFT · UNVALIDATED"),
    "Manual draft did not display its unvalidated warning",
  );
  await draftDialog.locator("#manual-draft-lines tr").first().getByRole("button", { name: "Find item" }).click();
  const draftCandidate = page.locator(".reference-result").first();
  await draftCandidate.locator("summary").click();
  assert.equal(await draftCandidate.locator('[data-candidate-field="internal_item"]').inputValue(), "INTERNAL-100");
  assert.equal(await draftCandidate.locator('[data-candidate-field="sku"]').count(), 0, "Draft offered supplier SKU as the internal Item value");
  await draftCandidate.locator('[data-candidate-field="gtin"]').selectOption("629000000002");
  await draftCandidate.locator('.candidate-confirm').check();
  await draftCandidate.getByRole("button", { name: "Use selected item" }).click();
  await draftDialog.waitFor({ state: "visible", timeout });
  assert.equal(await draftDialog.locator('#manual-draft-lines tr').first().locator('[name="Item"]').inputValue(), "INTERNAL-100");
  assert.equal(await draftDialog.locator('#manual-draft-lines tr').first().locator('[name="UPC"]').inputValue(), "629000000002");
  await page.unroute(lookupRoute);
  const draftHeader = draftDialog.locator('[data-draft-section="Header"]');
  await draftHeader.locator('[name="Document"]').fill("DRAFT-SMOKE");
  await draftHeader.locator('[name="Supplier Site"]').fill("TEST-SITE");
  await draftHeader.locator('[name="Order No"]').fill("TEST-PO");
  await draftHeader.locator('[name="Location"]').fill("TEST-LOCATION");
  await draftHeader.locator('[name="Location Type"]').selectOption("Warehouse (W)");
  await draftHeader.locator('[name="Document Date"]').fill("2026-10-04");
  await draftHeader.locator('[name="Total Cost Ex Tax"]').fill("40");
  await draftHeader.locator('[name="Tax Amount"]').fill("2");
  const draftTax = draftDialog.locator('[data-draft-section="Tax_Breakdown"]');
  await draftTax.locator('[name="Tax Code"]').fill("VAT");
  await draftTax.locator('[name="Tax Basis"]').fill("40");
  while (await draftDialog.locator("#manual-draft-lines tr").count() > 1) {
    await draftDialog.locator("#manual-draft-lines tr").last().getByRole("button", { name: "Remove draft detail row" }).click();
  }
  const draftLine = draftDialog.locator("#manual-draft-lines tr").first();
  await draftLine.locator('[name="Item"]').fill("TEST-ITEM");
  await draftLine.locator('[name="UPC"]').fill("629000000001");
  await draftLine.locator('[name="Unit Cost"]').fill("40");
  await draftLine.locator('[name="Quantity"]').fill("1");
  await draftLine.locator('[name="Unit Tax Code"]').fill("VAT");
  await page.locator('#manual-draft-acknowledge').check();
  const invalidDraftFields = await draftDialog.locator("form").evaluate((form) => [...form.elements]
    .filter((element) => typeof element.checkValidity === "function" && !element.checkValidity())
    .map((element) => element.name || element.id));
  assert.deepEqual(invalidDraftFields, [], `Manual draft form remained invalid: ${invalidDraftFields.join(", ")}`);
  const draftPostPromise = page.waitForResponse(
    responseMatches("POST", `/api/jobs/${demoJob.id}/draft`),
    { timeout },
  );
  await page.locator("#download-manual-draft").click();
  const draftPost = await draftPostPromise;
  await assertOk(draftPost, "Unvalidated draft creation");
  assert.match(draftPost.headers()["content-disposition"] || "", /_DRAFT_UNVALIDATED\.xlsx"?$/);
  assert.match(draftPost.headers()["content-type"] || "", /application\/vnd\.openxmlformats-officedocument\.spreadsheetml\.sheet/);
  await draftDialog.waitFor({ state: "hidden", timeout });
  const afterDraft = await (await context.request.get(`${baseURL}/api/jobs/${demoJob.id}`)).json();
  assert.equal(afterDraft.revision, beforeDraft.revision, "Draft creation revised the active invoice job");
  assert.equal(afterDraft.export_id, beforeDraft.export_id, "Draft creation marked the invoice as exported");

  // A direct extracted-facts workbook is a separate, explicitly unvalidated
  // download and must not revise, approve or allocate the active job.
  await page.locator("#export-extraction-draft").click();
  const extractionDraftDialog = page.locator("#extraction-draft-dialog");
  await extractionDraftDialog.waitFor({ state: "visible", timeout });
  assert.match(await extractionDraftDialog.textContent(), /Unknown internal or reference fields stay blank/);
  const extractionDraftPromise = page.waitForResponse(
    responseMatches("POST", `/api/jobs/${demoJob.id}/extraction-draft`),
    { timeout },
  );
  await page.locator("#confirm-extraction-draft").click();
  const extractionDraftResponse = await extractionDraftPromise;
  await assertOk(extractionDraftResponse, "Extracted review-only workbook");
  assert.match(
    extractionDraftResponse.headers()["content-disposition"] || "",
    /EXTRACTION_REVIEW_ONLY[^";]*\.xlsx"?$/,
  );
  await extractionDraftDialog.waitFor({ state: "hidden", timeout });
  const afterExtractionDraft = await (await context.request.get(`${baseURL}/api/jobs/${demoJob.id}`)).json();
  assert.equal(afterExtractionDraft.revision, afterDraft.revision, "Extracted workbook revised the active invoice job");
  assert.equal(afterExtractionDraft.export_id, afterDraft.export_id, "Extracted workbook wrote the approved export ledger");

  // Optional printed line totals render blank when absent and survive an
  // ordinary review save without changing the printed unit price.
  const reviewLineAmounts = page.locator("#line-items tr").first();
  const originalPrintedUnitPrice = await reviewLineAmounts.locator('[name="price"]').inputValue();
  assert.equal(await reviewLineAmounts.locator('[name="net_amount"]').inputValue(), "");
  assert.equal(await reviewLineAmounts.locator('[name="tax_amount"]').inputValue(), "");
  await reviewLineAmounts.locator(".line-amounts summary").click();
  await reviewLineAmounts.locator('[name="net_amount"]').fill("600.00");
  await reviewLineAmounts.locator('[name="tax_amount"]').fill("30.00");

  const confirmReview = page.locator("#confirm-review");
  const exportButton = page.locator("#export-job");
  const runId = Date.now().toString(36).toUpperCase();
  const firstNumber = `SMOKE-${runId}-1`;
  const secondNumber = `SMOKE-${runId}-2`;
  const thirdNumber = `SMOKE-${runId}-3`;
  const tinyInvoice = (number) => Buffer.from(JSON.stringify({
    number,
    supplier_name: "Lumena Beauty Trading LLC",
    date: "2026-10-04",
    po: "70002",
    currency: "AED",
    net: "40",
    tax: "2",
    lines: [{
      sku: "LUM-CRM50",
      gtin: "00012345678912",
      description: "Lumena cream 50 ml",
      uom: "EA",
      qty: "1",
      price: "40",
    }],
  }));
  await page.locator('#header-fields input[name="number"]').fill(firstNumber);
  await confirmReview.uncheck();
  assert(await exportButton.isDisabled(), "Export must be disabled before evidence review is confirmed");

  const unconfirmedReviewPromise = page.waitForResponse(
    responseMatches("POST", `/api/jobs/${demoJob.id}/review`),
    { timeout },
  );
  await page.getByRole("button", { name: "Save & revalidate" }).click();
  const unconfirmedReviewResponse = await unconfirmedReviewPromise;
  await assertOk(unconfirmedReviewResponse, "Unconfirmed review save");
  const unconfirmedJob = await unconfirmedReviewResponse.json();
  assert.equal(unconfirmedJob.invoice.lines[0].price, originalPrintedUnitPrice);
  assert.equal(unconfirmedJob.invoice.lines[0].net_amount, "600.00");
  assert.equal(unconfirmedJob.invoice.lines[0].tax_amount, "30.00");
  assert.equal(await page.locator('#line-items tr').first().locator('[name="net_amount"]').inputValue(), "600.00");
  assert.equal(unconfirmedJob.reviewed, false, "The unconfirmed save was recorded as reviewed");
  assert.equal(
    unconfirmedJob.validation.ready,
    false,
    "Validation must hold an invoice whose evidence review is unconfirmed",
  );
  assert(await exportButton.isDisabled(), "Export became available without review confirmation");

  // Verify the server-side guard as well as the disabled UI control. This is a
  // rejected request and does not create an export or otherwise mutate the job.
  const guardedExport = await context.request.post(`${baseURL}/api/jobs/${demoJob.id}/export`, {
    data: { revision: unconfirmedJob.revision },
    headers: { "x-studio-request": "1" },
  });
  assert.equal(guardedExport.status(), 409, "The API allowed export before review confirmation");

  const firstReadyJob = await saveConfirmedReview(demoJob.id, firstNumber);

  // Add a second, distinct synthetic invoice through the normal file-upload UI.
  await page.locator("[data-open-upload]").first().click();
  await page.locator("#upload-dialog").waitFor({ state: "visible" });
  assert.equal(await page.locator("#upload-prefer-native-text").isChecked(), false, "Upload did not share the native-text preference");
  const secondFilename = `smoke-${runId.toLowerCase()}-2.json`;
  await page.locator("#invoice-files").setInputFiles({
    name: secondFilename,
    mimeType: "application/json",
    buffer: tinyInvoice(secondNumber),
  });
  await page.locator("#start-upload").waitFor({ state: "visible" });

  const uploadPreflightsBeforeDialogCancel = requestLog.filter(
    (entry) => entry.method === "POST" && entry.path === "/api/preflight",
  ).length;
  await page.locator("#upload-dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  await page.locator("#upload-dialog").waitFor({ state: "hidden" });
  await page.waitForTimeout(100);
  assert.equal(
    requestLog.filter((entry) => entry.method === "POST" && entry.path === "/api/preflight").length,
    uploadPreflightsBeforeDialogCancel,
    "Upload Cancel submitted the upload form",
  );
  await page.locator("[data-open-upload]").first().click();
  await page.locator("#upload-dialog").waitFor({ state: "visible" });
  assert(
    (await page.locator("#upload-queue").textContent())?.includes(secondFilename),
    "Closing the upload dialog discarded the selected file",
  );

  const uploadsBeforeCancel = requestLog.filter(
    (entry) => entry.method === "POST" && entry.path === "/api/invoices",
  ).length;
  const cancelledPlanPromise = page.waitForResponse(
    responseMatches("POST", "/api/preflight"),
    { timeout },
  );
  await page.locator("#start-upload").click();
  const cancelledPlanResponse = await cancelledPlanPromise;
  await assertOk(cancelledPlanResponse, "Cancelled upload preflight");
  assert.equal(cancelledPlanResponse.request().postDataJSON().options.prefer_native_text, false);
  await page.locator("#preflight-dialog").waitFor({ state: "visible" });
  assert.match(await page.locator("#preflight-summary").textContent(), /PDF text\s*Disabled/i);
  assert.equal(
    requestLog.filter((entry) => entry.method === "POST" && entry.path === "/api/invoices")
      .length,
    uploadsBeforeCancel,
    "The upload began before its cancelled preflight was confirmed",
  );
  await page.locator("#cancel-preflight").click();
  await page.locator("#preflight-dialog").waitFor({ state: "hidden" });
  await page.locator("#upload-dialog").waitFor({ state: "visible" });
  await page.waitForTimeout(250);
  assert(
    (await page.locator("#upload-queue").textContent())?.includes(secondFilename),
    "Cancelling preflight did not restore the original upload queue",
  );
  assert.equal(await page.locator("#upload-prefer-native-text").isChecked(), false, "Cancelled preflight changed the native-text rule");
  assert.equal(
    requestLog.filter((entry) => entry.method === "POST" && entry.path === "/api/invoices")
      .length,
    uploadsBeforeCancel,
    "Cancelling preflight sent an invoice to the server",
  );

  const uploadResponse = await runPreflight(
    () => page.locator("#start-upload").click(),
    "/api/invoices",
    "Invoice upload",
  );
  const secondJob = await uploadResponse.json();
  assert.match(secondJob.id, /^[a-f0-9]{32}$/, "Upload did not return an invoice job id");
  assert.equal(secondJob.options.prefer_native_text, false, "Upload did not retain its confirmed native-text rule");
  await waitForCompletedJob(secondJob.id);
  await waitForSelectedReview(secondJob.filename);
  const secondReadyJob = await saveConfirmedReview(secondJob.id, secondNumber);

  const thirdFilename = `smoke-${runId.toLowerCase()}-3.json`;
  await page.locator("[data-open-upload]").first().click();
  await page.locator("#upload-dialog").waitFor({ state: "visible" });
  await page.locator("#invoice-files").setInputFiles({
    name: thirdFilename,
    mimeType: "application/json",
    buffer: tinyInvoice(thirdNumber),
  });
  const thirdUploadResponse = await runPreflight(
    () => page.locator("#start-upload").click(),
    "/api/invoices",
    "Second batch invoice upload",
  );
  const thirdJob = await thirdUploadResponse.json();
  assert.match(thirdJob.id, /^[a-f0-9]{32}$/, "Second upload did not return an invoice job id");
  await waitForCompletedJob(thirdJob.id);
  await waitForSelectedReview(thirdJob.filename);
  const thirdReadyJob = await saveConfirmedReview(thirdJob.id, thirdNumber);

  // The inbox lists invoices by upload time, newest first, with the time shown.
  const newestRows = page.locator("#job-list .job-row");
  await waitFor("the newest uploads to lead the inbox", async () =>
    (await newestRows.nth(0).textContent())?.includes(thirdNumber) && (await newestRows.nth(1).textContent())?.includes(secondNumber),
  );
  assert(
    !Number.isNaN(Date.parse(await newestRows.nth(0).locator("time.job-time").getAttribute("datetime"))),
    "The newest upload does not show when it was added",
  );

  assert.equal(await page.locator("#workflow-upload-count").textContent(), `${initialJobCount + 3} invoices`);
  assert.match(await page.locator("#workflow-process-count").textContent(), /^\d+ active$/);
  assert.match(await page.locator("#workflow-review-count").textContent(), /^\d+ processed$/);
  const processedSelectionLabel = await page.locator("#select-processed").textContent();
  const processedSelectionCount = Number(processedSelectionLabel?.match(/\((\d+)\)/)?.[1]);
  assert(processedSelectionCount >= 3, "Select processed did not report the completed test invoices");
  await page.locator("#select-processed").click();
  assert.equal(await page.locator("#selected-count").textContent(), `${processedSelectionCount} selected`, "Select processed did not include every completed invoice");
  await page.locator("#select-processed").click();
  assert.equal(await page.locator("#selected-count").textContent(), "0 selected", "Select processed did not clear the full selection");

  // Select the two ready invoices and exercise the UI's atomic batch export,
  // including the browser's follow-up workbook download.
  const secondRow = page.locator("#job-list .job-row").filter({ hasText: secondNumber });
  const thirdRow = page.locator("#job-list .job-row").filter({ hasText: thirdNumber });
  await secondRow.locator(".job-select").check();
  await thirdRow.locator(".job-select").check();
  await waitFor("two invoices to be selected for batch export", async () =>
    (await page.locator("#selected-count").textContent())?.trim() === "2 selected",
  );
  assert(await secondRow.locator(".job-select").isChecked(), "The second ready invoice was not selected");
  assert(await thirdRow.locator(".job-select").isChecked(), "The third ready invoice was not selected");
  assert(await page.locator("#batch-export").isEnabled(), "Batch export did not enable");

  // Unsaved per-invoice edits remain in the form and block any batch download
  // until the reviewer explicitly saves them.
  const currentNumber = page.locator('#header-fields input[name="number"]');
  await currentNumber.fill(`${thirdNumber}-TEMP`);
  await currentNumber.fill(thirdNumber);
  assert.match(await page.locator("#review-save-state").textContent(), /Unsaved changes/);
  const reviewBatchPostsBeforeSave = requestLog.filter(
    (entry) => entry.method === "POST" && entry.path === "/api/exports/extraction-batch",
  ).length;
  await page.locator("#batch-review").click();
  assert(await page.locator("#batch-review-dialog").isHidden(), "Unsaved edits opened the batch download confirmation");
  assert.match(await page.locator("#global-message").textContent(), /Save the current invoice/);
  assert.equal(
    requestLog.filter((entry) => entry.method === "POST" && entry.path === "/api/exports/extraction-batch").length,
    reviewBatchPostsBeforeSave,
    "Unsaved edits were omitted from a hidden batch request",
  );
  const resavePromise = page.waitForResponse(responseMatches("POST", `/api/jobs/${thirdJob.id}/review`), { timeout });
  await page.locator("#save-review").click();
  await assertOk(await resavePromise, "Explicit save before review batch");
  // The response resolves on its headers, before the app reads the body and re-renders the save state.
  await waitFor("the explicit save to render Saved", async () => await page.locator("#review-save-state").textContent() === "Saved");

  const beforeReviewBatchSecond = await (await context.request.get(`${baseURL}/api/jobs/${secondJob.id}`)).json();
  const beforeReviewBatchThird = await (await context.request.get(`${baseURL}/api/jobs/${thirdJob.id}`)).json();
  await page.locator("#batch-review").click();
  await page.locator("#batch-review-dialog").waitFor({ state: "visible" });
  assert.equal(await page.locator("#batch-review-count").textContent(), "2 invoices");
  const reviewBatchResponsePromise = page.waitForResponse(responseMatches("POST", "/api/exports/extraction-batch"), { timeout });
  const reviewBatchDownloadPromise = page.waitForEvent("download", { timeout });
  await page.locator("#confirm-batch-review").click();
  const reviewBatchResponse = await reviewBatchResponsePromise;
  await assertOk(reviewBatchResponse, "Combined extraction review workbook");
  const reviewBatchBody = reviewBatchResponse.request().postDataJSON();
  assert.equal(reviewBatchBody.acknowledge_unvalidated, true);
  assert.deepEqual(
    reviewBatchBody.jobs.map((job) => job.id).sort(),
    [secondJob.id, thirdJob.id].sort(),
  );
  const reviewBatchDownload = await reviewBatchDownloadPromise;
  assert.match(reviewBatchDownload.suggestedFilename(), /REVIEW_ONLY.*\.xlsx$/i);
  assert(await reviewBatchDownload.path(), "Playwright did not retain the combined review workbook");
  const afterReviewBatchSecond = await (await context.request.get(`${baseURL}/api/jobs/${secondJob.id}`)).json();
  const afterReviewBatchThird = await (await context.request.get(`${baseURL}/api/jobs/${thirdJob.id}`)).json();
  assert.equal(afterReviewBatchSecond.revision, beforeReviewBatchSecond.revision, "Review batch changed the first job revision");
  assert.equal(afterReviewBatchThird.revision, beforeReviewBatchThird.revision, "Review batch changed the second job revision");
  assert.equal(afterReviewBatchSecond.export_id, beforeReviewBatchSecond.export_id, "Review batch wrote the first approved export ledger");
  assert.equal(afterReviewBatchThird.export_id, beforeReviewBatchThird.export_id, "Review batch wrote the second approved export ledger");

  const batchPostPromise = page.waitForResponse(responseMatches("POST", "/api/exports/batch"), {
    timeout,
  });
  const batchGetPromise = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      new URL(response.url()).pathname.startsWith("/api/batches/"),
    { timeout },
  );
  const downloadPromise = page.waitForEvent("download", { timeout });
  await page.locator("#batch-export").click();
  const batchPost = await batchPostPromise;
  await assertOk(batchPost, "Batch export creation");
  const batch = await batchPost.json();
  assert.equal(batch.count, 2, "Batch export did not contain exactly two invoices");
  assert.match(batch.id, /^[a-f0-9]{32}$/, "Batch export did not return a batch id");
  const batchGet = await batchGetPromise;
  await assertOk(batchGet, "Batch workbook download");
  assert.equal(new URL(batchGet.url()).pathname, `/api/batches/${batch.id}`);
  const download = await downloadPromise;
  assert.match(download.suggestedFilename(), /^Merch_Inv_Batch_[a-f0-9]{8}\.xlsx$/);
  const downloadedWorkbook = await download.path();
  assert(downloadedWorkbook, "Playwright did not retain the downloaded batch workbook");

  const workbookCheck = spawnSync(
    process.env.PYTHON || resolve(repoRoot, ".venv/bin/python"),
    [
      "-c",
      `
import json
import io
import sys
from pathlib import Path
from openpyxl import load_workbook

# Playwright stores downloads under an extensionless temporary name, so pass
# bytes to openpyxl rather than relying on its filename-extension check.
book = load_workbook(io.BytesIO(Path(sys.argv[1]).read_bytes()), data_only=False)
required = {"Header", "Tax_Breakdown", "Details"}
assert required.issubset(book.sheetnames), f"Missing sheets: {sorted(required - set(book.sheetnames))}"

details = book["Details"]
expected_details = ["Transaction Number", "Item", "UPC", "Unit Cost", "Quantity", "Unit Tax Code"]
details_column_count = details.max_column
details_headers = [details.cell(1, column).value for column in range(1, details_column_count + 1)]
assert details_column_count == 6, f"Details has {details_column_count} columns instead of 6: {details_headers}"
assert details_headers == expected_details, f"Unexpected Details columns: {details_headers}"
helper_cells = [cell.coordinate for cell in details._cells.values() if cell.column > 6 and cell.value is not None]
assert not helper_cells, f"Details contains helper columns: {helper_cells}"
formulas = [cell.coordinate for row in details.iter_rows() for cell in row if cell.data_type == "f"]
assert not formulas, f"Details contains formula/helper cells: {formulas}"

def transactions(sheet_name):
    sheet = book[sheet_name]
    return sorted({sheet.cell(row, 1).value for row in range(2, sheet.max_row + 1) if sheet.cell(row, 1).value is not None})

header_transactions = transactions("Header")
tax_transactions = transactions("Tax_Breakdown")
detail_transactions = transactions("Details")
assert header_transactions == [1, 2], f"Header transaction numbers are {header_transactions}"
assert tax_transactions == header_transactions, f"Tax_Breakdown does not join to Header: {tax_transactions}"
assert detail_transactions == header_transactions, f"Details does not join to Header: {detail_transactions}"
print(json.dumps({
    "details_columns": details_column_count,
    "details_helper_cells": helper_cells,
    "details_formulas": formulas,
    "header_transactions": header_transactions,
    "tax_transactions": tax_transactions,
    "detail_transactions": detail_transactions,
}))
`,
      downloadedWorkbook,
    ],
    { encoding: "utf8" },
  );
  assert.equal(
    workbookCheck.status,
    0,
    `Downloaded workbook validation failed:\n${workbookCheck.stderr || workbookCheck.stdout}`,
  );
  const workbookReport = JSON.parse(workbookCheck.stdout);
  assert.equal(workbookReport.details_columns, 6);
  assert.deepEqual(workbookReport.details_helper_cells, []);
  assert.deepEqual(workbookReport.details_formulas, []);
  assert.deepEqual(workbookReport.header_transactions, [1, 2]);
  assert.deepEqual(workbookReport.tax_transactions, [1, 2]);
  assert.deepEqual(workbookReport.detail_transactions, [1, 2]);

  await page.getByRole("button", { name: "References", exact: true }).click();
  await page.locator("#references-section").waitFor({ state: "visible" });
  assert.notEqual(await page.locator("#reference-source-summary").textContent(), "Loading staged source summary…");
  await page.getByRole("heading", { level: 1, name: "Reference data & rules" }).waitFor();
  assert(
    await page.getByRole("heading", { name: "Approved matching snapshot" }).isVisible(),
    "The reference controls are not visible",
  );
  assert(
    await page.getByRole("heading", { name: "Source evidence lookup" }).isVisible(),
    "The unapproved source-evidence lookup is not visible",
  );
  assert(
    (await page.locator(".source-evidence-warning").textContent())?.includes("not approved matching data"),
    "Staged source evidence is not clearly separated from approved matching data",
  );

  await page.getByRole("button", { name: "Engines & AI", exact: true }).click();
  await page.locator("#engines-section").waitFor({ state: "visible" });
  await page.getByRole("heading", { level: 1, name: "Engines & AI" }).waitFor();
  assert(
    await page.getByRole("heading", { name: "Extraction engines" }).isVisible(),
    "The extraction engine controls are not visible",
  );
  const engineHelp = await page.locator("#engine-list .engine-help").allTextContents();
  assert(engineHelp.some((text) => text.includes("PaddleOCR") && text.includes("layout rules")), "PaddleOCR conversion help is missing");
  assert(engineHelp.some((text) => text.includes("Docling") && text.includes("table cells")), "Docling table conversion help is missing");
  const engineTargets = await page.locator("#engine-list .engine-quality-target").allTextContents();
  assert.equal(engineTargets.length, 2, "PaddleOCR and Docling quality targets are not both visible");
  assert(engineTargets.every((text) => text.includes("at least 90%") && text.includes("not a measured result") && text.includes("line recall")), "Local engine targets could be mistaken for achieved accuracy");

  await page.evaluate(() => renderTrace({
    trace: [
      {
        engine: "paddleocr",
        method: "text_only",
        status: "text_only",
        seconds: 1.93,
        queue_seconds: 4.5,
        text_characters: 1222,
        extracted_fields: 0,
        line_items: 0,
        completeness: 0,
      },
      {
        engine: "docling",
        method: "docling_table",
        status: "extracted",
        seconds: 7.25,
        text_characters: 31717,
        extracted_fields: 6,
        line_items: 5,
        completeness: 0.8,
      },
    ],
    provenance: [],
  }));
  const traceEvidence = await page.locator("#trace-list li").allTextContents();
  assert.match(traceEvidence[0], /PaddleOCR: Text Only · Text only · 1\.93 s reading · 4\.50 s waiting for a reader · 1,222 text characters · 0 fields · 0 items$/, "A reader that found nothing still showed a fields-found percentage");
  assert.match(traceEvidence[1], /Docling: Extracted · Table and word geometry conversion · 7\.25 s reading · 31,717 text characters · 6 fields · 5 items · 80% fields found/);
  await page.evaluate(() => renderTrace({selected_engine: "paddleocr", trace: [
    {engine: "paddleocr", status: "extracted"}, {engine: "docling", status: "extracted"},
  ]}));
  assert.equal(await page.locator("#trace-summary").textContent(), "Chosen: PaddleOCR", "Trace must name the retained reader, not simply the last attempt");

  // Exercise a real password input event with a unique marker, then prove that
  // neither browser storage area contains the credential value.
  const secretMarker = `sk-browser-storage-${runId}-do-not-persist`;
  const apiKeyInput = page.locator('form.api-key-form[data-provider="openai"] input[name="api_key"]');
  await apiKeyInput.fill(secretMarker);
  await page.locator("#settings-provider").focus();
  const browserStorage = await page.evaluate(() => ({
    local: Object.fromEntries(Object.entries(localStorage)),
    session: Object.fromEntries(Object.entries(sessionStorage)),
  }));
  assert(
    !JSON.stringify(browserStorage).includes(secretMarker),
    "An API key value entered browser storage",
  );
  await apiKeyInput.fill("");

  // Exercise the cloud-only browser boundary without using a real account. The
  // Firebase module is replaced before page load; application requests remain
  // real and must all carry the module's ID token after /api/session verifies
  // the allowlisted identity.
  const cloudPage = page;
  const cloudErrors = [];
  const cloudApiHeaders = [];
  let offerManagedVertex = false;
  let vertexModelRequests = 0;
  cloudPage.on("pageerror", (error) => cloudErrors.push(error.message));
  await cloudPage.route("**/static/firebase-auth.bundle.js", (route) => route.fulfill({
    status: 200,
    contentType: "text/javascript",
    body: `window.InvoiceStudioAuth={
      bootstrap:async()=>({cloud:true,providers:["password"],user:{}}),
      getToken:async()=>"BROWSER_TEST_ID_TOKEN",
      signOut:async()=>{}
    };`,
  }));
  await cloudPage.route("**/api/session", async (route) => {
    cloudApiHeaders.push(route.request().headers().authorization);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ email: "owner@example.test", uid: "browser-test-owner" }),
    });
  });
  await cloudPage.route("**/api/state", async (route) => {
    const response = await route.fetch();
    const state = await response.json();
    state.connections = { ...state.connections, vertex: true, openai: !offerManagedVertex };
    state.settings = offerManagedVertex
      ? { ...state.settings, provider: "openai", model: "" }
      : { ...state.settings, provider: "openai", model: "gpt-user-selected" };
    await route.fulfill({ response, json: state });
  });
  await cloudPage.route("**/api/connections/vertex/models", async (route) => {
    vertexModelRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: [{ id: "gemini-3.7-flash", name: "Gemini 3.7 Flash" }] }),
    });
  });
  cloudPage.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path === "/api/state" || /\/api\/jobs\/[^/]+\/document$/.test(path)) {
      cloudApiHeaders.push(request.headers().authorization);
    }
  });
  await cloudPage.setViewportSize({ width: 390, height: 844 });
  await cloudPage.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
  await cloudPage.locator("#app-shell").waitFor({ state: "visible", timeout });
  assert.equal(await cloudPage.locator("#settings-provider").inputValue(), "openai");
  assert.equal(await cloudPage.locator("#settings-model").inputValue(), "gpt-user-selected");
  assert.equal(vertexModelRequests, 0, "A connected user-selected model was silently replaced");
  offerManagedVertex = true;
  await cloudPage.evaluate(() => loadState());
  assert.equal(await cloudPage.locator("#settings-provider").inputValue(), "vertex");
  assert.equal(await cloudPage.locator("#settings-model").inputValue(), "gemini-3.7-flash");
  assert.equal(vertexModelRequests, 1, "Managed Vertex models were not loaded once for the fallback offer");
  assert.equal(
    (await cloudPage.locator("#cloud-user-email").textContent())?.trim(),
    "owner@example.test",
    "The cloud shell appeared without the server-verified identity",
  );
  await waitFor("cloud API calls to carry a Firebase ID token", () =>
    cloudApiHeaders.length >= 2 && cloudApiHeaders.every(
      (value) => value === "Bearer BROWSER_TEST_ID_TOKEN",
    ),
  );
  await cloudPage.getByRole("button", { name: "Engines & AI", exact: true }).click();
  await cloudPage.locator('[data-provider-card="vertex"]').waitFor({ state: "visible", timeout });
  assert.equal(
    (await cloudPage.locator('[data-connection-state="vertex"]').textContent())?.trim(),
    "Connected",
  );
  await cloudPage.getByRole("button", { name: "Workspace", exact: true }).click();
  await cloudPage.locator("#workspace-section").waitFor({ state: "visible", timeout });
  const mobileLayout = await cloudPage.evaluate(() => ({
    viewport: window.innerWidth,
    root: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  assert(
    mobileLayout.root <= mobileLayout.viewport && mobileLayout.body <= mobileLayout.viewport,
    `Cloud workspace overflows the mobile viewport: ${JSON.stringify(mobileLayout)}`,
  );
  await cloudPage.locator("#cloud-sign-out").click();
  await cloudPage.locator("#app-shell").waitFor({ state: "hidden", timeout });
  assert(
    await cloudPage.locator("#password-sign-in").isVisible(),
    "Cloud sign-out did not return to the configured password sign-in gate",
  );
  assert.deepEqual(cloudErrors, [], `Cloud-mode browser errors: ${cloudErrors.join("; ")}`);
  assert.deepEqual(pageErrors, [], `Browser page errors: ${pageErrors.join("; ")}`);
  assert.deepEqual(serverErrors, [], `Server errors: ${serverErrors.join("; ")}`);
  console.log(
    `Browser smoke test passed (jobs ${secondReadyJob.id} + ${thirdReadyJob.id}; batch ${batch.id}; Chromium; ${baseURL}).`,
  );
} catch (error) {
  await page
    .screenshot({ path: "/tmp/inv-studio-browser-failure.png", fullPage: true })
    .catch(() => {});
  throw error;
} finally {
  await browser.close();
}
