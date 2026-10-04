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

  // Reprocessing is separately gated, even when the document is already local.
  await page.locator("#retry-job").click();
  await page.locator("#retry-dialog").waitFor({ state: "visible" });
  const retryResponse = await runPreflight(
    () => page.locator("#retry-dialog").getByRole("button", { name: "Reprocess" }).click(),
    `/api/jobs/${demoJob.id}/retry`,
    "Invoice retry",
  );
  const retriedJob = await retryResponse.json();
  assert.equal(retriedJob.id, demoJob.id, "Retry returned a different invoice job");
  await waitForCompletedJob(demoJob.id);
  await waitForSelectedReview(demoJob.filename);

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
  const secondFilename = `smoke-${runId.toLowerCase()}-2.json`;
  await page.locator("#invoice-files").setInputFiles({
    name: secondFilename,
    mimeType: "application/json",
    buffer: tinyInvoice(secondNumber),
  });
  await page.locator("#start-upload").waitFor({ state: "visible" });

  const uploadsBeforeCancel = requestLog.filter(
    (entry) => entry.method === "POST" && entry.path === "/api/invoices",
  ).length;
  const cancelledPlanPromise = page.waitForResponse(
    responseMatches("POST", "/api/preflight"),
    { timeout },
  );
  await page.locator("#start-upload").click();
  await assertOk(await cancelledPlanPromise, "Cancelled upload preflight");
  await page.locator("#preflight-dialog").waitFor({ state: "visible" });
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
  await page.getByRole("heading", { level: 1, name: "Reference data & rules" }).waitFor();
  assert(
    await page.getByRole("heading", { name: "Reference workbook" }).isVisible(),
    "The reference controls are not visible",
  );

  await page.getByRole("button", { name: "Engines & AI", exact: true }).click();
  await page.locator("#engines-section").waitFor({ state: "visible" });
  await page.getByRole("heading", { level: 1, name: "Engines & AI" }).waitFor();
  assert(
    await page.getByRole("heading", { name: "Extraction engines" }).isVisible(),
    "The extraction engine controls are not visible",
  );

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
  cloudPage.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path === "/api/state" || /\/api\/jobs\/[^/]+\/document$/.test(path)) {
      cloudApiHeaders.push(request.headers().authorization);
    }
  });
  await cloudPage.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
  await cloudPage.locator("#app-shell").waitFor({ state: "visible", timeout });
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
