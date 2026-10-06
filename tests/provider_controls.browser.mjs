/**
 * Focused Playwright regression for provider model controls and permanent
 * invoice deletion.
 *
 * Run against the isolated development server:
 *   BASE_URL=http://127.0.0.1:8770 node tests/provider_controls.browser.mjs
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const baseURL = (process.env.BASE_URL || "http://127.0.0.1:8765").replace(/\/$/, "");
const timeout = Number(process.env.BROWSER_TIMEOUT_MS || 120_000);
const runId = `${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
const mutationHeaders = { "X-Studio-Request": "1" };

async function bodyText(response) {
  try {
    return await response.text();
  } catch {
    return "<unavailable>";
  }
}

async function assertOk(response, label) {
  assert(response, `${label} did not return a response`);
  if (!response.ok()) {
    assert.fail(`${label} failed with HTTP ${response.status()}: ${await bodyText(response)}`);
  }
}

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
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Timed out waiting for ${label}${lastError ? `: ${lastError.message}` : ""}`);
}

const emptyInvoice = {
  number: "SYNTHETIC-CONTROLS",
  supplier_name: "Synthetic supplier",
  buyer_name: null,
  seller: null,
  site: null,
  buyer: null,
  po: null,
  location: null,
  date: null,
  currency: null,
  origin: null,
  market: null,
  taxCode: null,
  net: null,
  tax: null,
  lines: [],
};

const syntheticJob = {
  id: "provider-control-synthetic",
  filename: "SYNTHETIC-provider-controls.txt",
  size: 128,
  created_at: "2026-10-05T00:00:00+00:00",
  status: "review",
  options: {
    engine: "auto",
    ai_fallback: true,
    provider: "anthropic",
    model: "claude-saved",
    language: "en",
  },
  invoice: emptyInvoice,
  revision: 7,
  reviewed: false,
  trace: [],
  provenance: [],
  completeness: 0.05,
  selected_engine: "invoice2data",
  validation: { ready: false, issues: [], matches: [] },
  extraction_status: "fields_extracted",
  text: "Synthetic invoice text for browser controls.",
  boxes: [],
};

const browser = await chromium.launch({
  headless: process.env.HEADFUL !== "1",
  args: ["--single-process", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer"],
});

let createdJob;
let actualContext;
let providerContext;
try {
  providerContext = await browser.newContext();
  const baselineResponse = await providerContext.request.get(`${baseURL}/api/state`);
  await assertOk(baselineResponse, "Baseline state");
  const baselineState = await baselineResponse.json();
  const page = await providerContext.newPage();
  page.setDefaultTimeout(timeout);
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  const mockedState = {
    ...baselineState,
    jobs: [syntheticJob],
    exports: [],
    accounts: [],
    active_account: null,
    connections: {
      openai: true,
      anthropic: true,
      chatgpt: false,
      claude_local: true,
      vertex: false,
    },
    settings: { provider: "anthropic", model: "claude-saved", ai_fallback: true },
  };

  let releaseOpenAI;
  let markOpenAIStarted;
  const openAIStarted = new Promise((resolve) => { markOpenAIStarted = resolve; });
  let modelRequests = 0;
  let deleteRequests = 0;
  let rejectedDeleteBody;

  await page.route("**/api/state", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(mockedState),
  }));
  await page.route(`**/api/jobs/${syntheticJob.id}`, (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(syntheticJob),
  }));
  await page.route(`**/api/jobs/${syntheticJob.id}/document`, (route) => route.fulfill({
    status: 200,
    contentType: "text/plain",
    body: "Synthetic invoice text for browser controls.",
  }));
  await page.route("**/api/connections/*/models", async (route) => {
    modelRequests += 1;
    const provider = new URL(route.request().url()).pathname.split("/").at(-2);
    if (provider === "anthropic") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ models: [
          { id: "claude-first", name: "Claude First" },
          { id: "claude-saved", name: "Claude Saved" },
        ] }),
      });
      return;
    }
    if (provider === "openai") {
      markOpenAIStarted();
      await new Promise((resolve) => { releaseOpenAI = resolve; });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ models: [{ id: "gpt-late", name: "Late OpenAI result" }] }),
      });
      return;
    }
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Synthetic provider model failure" }),
    });
  });
  await page.route("**/api/jobs/delete", async (route) => {
    deleteRequests += 1;
    rejectedDeleteBody = route.request().postDataJSON();
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: {
        code: "PARTIAL_EXPORTED_BATCH",
        message: "Select every invoice in the exported batch to delete it.",
        batch_id: "synthetic-batch",
        required_job_ids: [syntheticJob.id, "synthetic-batch-peer"],
      } }),
    });
  });

  await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
  await page.locator("#app-shell").waitFor({ state: "visible" });
  await page.locator("#retry-job").waitFor({ state: "visible" });

  const anthropicBadge = page.locator('[data-connection-state="anthropic"]');
  assert.equal((await anthropicBadge.textContent())?.trim(), "Key saved");

  await page.locator("[data-open-upload]").first().click();
  await page.locator("#upload-model-status").getByText("2 models available", { exact: false }).waitFor();
  assert.deepEqual(await page.locator("#upload-model-choices option").evaluateAll(
    (options) => options.map((option) => option.value),
  ), ["", "claude-first", "claude-saved", "__other_model__"]);
  assert.equal(await page.locator("#upload-model-choices").inputValue(), "claude-saved");
  assert.equal(await page.locator("#upload-model").inputValue(), "claude-saved");
  assert.equal((await anthropicBadge.textContent())?.trim(), "Verified");
  // A listed model shows once, in the list; "Other model ID…" opens the model ID box.
  assert.equal(await page.locator("#upload-model").isVisible(), false, "A listed model also shows in the model ID box");
  await page.locator("#upload-model-choices").selectOption("__other_model__");
  assert.equal(await page.locator("#upload-model").isVisible(), true, "Other model ID did not open the model ID box");
  assert.equal(await page.locator("#upload-model").evaluate((input) => input === document.activeElement), true);
  await page.locator("#upload-model").fill("synthetic-manual-model");
  await page.locator("#upload-model-choices").selectOption("claude-first");
  assert.equal(await page.locator("#upload-model").inputValue(), "claude-first");
  assert.equal(await page.locator("#upload-model").isVisible(), false);
  await page.setViewportSize({ width: 390, height: 844 });
  const uploadOverflow = await page.locator("#upload-dialog").evaluate((dialog) => dialog.scrollWidth - dialog.clientWidth);
  assert(uploadOverflow <= 1, `The upload dialog scrolls horizontally by ${uploadOverflow}px at 390px`);
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.keyboard.press("Escape");
  await page.locator("#upload-dialog").waitFor({ state: "hidden" });

  await page.locator("#retry-job").click();
  await page.locator("#retry-model-status").getByText("2 models available", { exact: false }).waitFor();
  assert.equal(await page.locator("#retry-model-choices").inputValue(), "claude-saved");
  assert.equal(await page.locator("#retry-model").inputValue(), "claude-saved");

  await page.locator("#retry-provider").selectOption("openai");
  await openAIStarted;
  assert.equal(await page.locator("#retry-model").inputValue(), "", "Provider change retained a stale model ID");
  assert.match(await page.locator("#retry-model-status").textContent(), /Loading models from Openai/i);

  await page.locator("#retry-provider").selectOption("anthropic");
  await page.locator("#retry-model-status").getByText("2 models available", { exact: false }).waitFor();
  assert.equal(await page.locator("#retry-model").inputValue(), "claude-saved");
  releaseOpenAI();
  await page.waitForTimeout(150);
  assert.equal(await page.locator("#retry-provider").inputValue(), "anthropic");
  assert.equal(await page.locator("#retry-model").inputValue(), "claude-saved", "Late model response won the provider race");
  assert.equal(await page.locator("#retry-model-choices").inputValue(), "claude-saved");

  await page.locator("#retry-provider").selectOption("claude_local");
  await page.locator("#retry-model-status").getByText("Synthetic provider model failure", { exact: true }).waitFor();
  assert.equal(await page.locator("#retry-model").inputValue(), "");
  assert.equal(await page.locator("#retry-model").isVisible(), true, "The model ID box is hidden although no model list loaded");
  assert.equal((await page.locator('[data-connection-state="openai"]').textContent())?.trim(), "Key saved");
  await page.keyboard.press("Escape");
  await page.locator("#retry-dialog").waitFor({ state: "hidden" });

  const mockedSelect = page.getByLabel(`Select ${syntheticJob.filename} for download or deletion`);
  await mockedSelect.check();
  assert.equal(deleteRequests, 0, "Selecting an invoice sent a deletion request");
  await page.locator("#batch-delete").click();
  assert.equal(await page.locator("#confirm-delete-invoices").isDisabled(), true);
  assert.deepEqual(await page.locator("#delete-invoice-list li").allTextContents(), [syntheticJob.filename]);
  await page.locator('#delete-invoices-dialog button[value="cancel"]').click();
  assert.equal(deleteRequests, 0, "Cancelling deletion sent a deletion request");

  await page.locator("#batch-delete").click();
  await page.locator("#delete-invoice-acknowledge").check();
  await page.locator("#confirm-delete-invoices").click();
  await page.locator("#delete-invoice-error").getByText(
    "Select every invoice in the exported batch to delete it.",
    { exact: true },
  ).waitFor();
  assert.equal(deleteRequests, 1);
  assert.deepEqual(rejectedDeleteBody, {
    jobs: [{ id: syntheticJob.id, revision: syntheticJob.revision }],
    confirm_permanent: true,
  });
  assert.equal(await page.locator("#delete-invoices-dialog").getAttribute("open"), "");
  assert(modelRequests >= 3, "Success, failure, and race model requests were not all exercised");
  assert.deepEqual(pageErrors, [], `Provider-control page errors: ${pageErrors.join("; ")}`);
  // Create one real synthetic invoice through the normal preflight/upload API.
  // AI fallback is disabled, and only this test-owned job is ever deleted.
  await page.unrouteAll({ behavior: "wait" });
  actualContext = providerContext;
  const api = actualContext.request;
  const filename = `SYNTHETIC-delete-${runId}.txt`;
  const content = Buffer.from(
    `INVOICE\nInvoice Number: SYNTHETIC-${runId}\nInvoice Date: 2026-10-05\n`,
    "utf8",
  );
  const options = {
    engine: "invoice2data",
    ai_fallback: false,
    provider: "openai",
    model: "",
    language: "en",
  };
  const preflightResponse = await api.post(`${baseURL}/api/preflight`, {
    headers: mutationHeaders,
    data: { options, files: [{ name: filename, size: content.length }] },
  });
  await assertOk(preflightResponse, "Synthetic deletion preflight");
  const preflight = await preflightResponse.json();
  const confirmationResponse = await api.post(`${baseURL}/api/preflight/confirm`, {
    headers: mutationHeaders,
    data: { token: preflight.token },
  });
  await assertOk(confirmationResponse, "Synthetic deletion preflight confirmation");
  const uploadResponse = await api.post(`${baseURL}/api/invoices`, {
    headers: mutationHeaders,
    multipart: {
      file: { name: filename, mimeType: "text/plain", buffer: content },
      options: JSON.stringify(options),
      preflight_token: preflight.token,
    },
  });
  await assertOk(uploadResponse, "Synthetic invoice upload");
  createdJob = await uploadResponse.json();

  createdJob = await waitFor("synthetic invoice processing to finish", async () => {
    const response = await api.get(`${baseURL}/api/jobs/${createdJob.id}`);
    await assertOk(response, "Synthetic invoice poll");
    const job = await response.json();
    return ["queued", "processing"].includes(job.status) ? null : job;
  });

  const actualPage = page;
  actualPage.setDefaultTimeout(timeout);
  const actualPageErrors = [];
  actualPage.on("pageerror", (error) => actualPageErrors.push(error.message));
  const deletionBodies = [];
  actualPage.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/jobs/delete") {
      deletionBodies.push(request.postDataJSON());
    }
  });
  await actualPage.goto(baseURL, { waitUntil: "domcontentloaded", timeout });
  await actualPage.locator("#app-shell").waitFor({ state: "visible" });

  const stateResponse = await api.get(`${baseURL}/api/state`);
  await assertOk(stateResponse, "State containing synthetic deletion job");
  const state = await stateResponse.json();
  assert(state.jobs.some((job) => job.id === createdJob.id), "Synthetic deletion job is absent from state");
  const actualSelect = actualPage.getByLabel(`Select ${filename} for download or deletion`);
  await actualSelect.check();
  assert.deepEqual(deletionBodies, [], "Ordinary selection sent a deletion request");
  await actualPage.locator("#batch-delete").click();
  await actualPage.locator("#delete-invoice-acknowledge").check();
  const deletionResponsePromise = actualPage.waitForResponse((response) =>
    response.request().method() === "POST" && new URL(response.url()).pathname === "/api/jobs/delete",
  );
  await actualPage.locator("#confirm-delete-invoices").click();
  const deletionResponse = await deletionResponsePromise;
  await assertOk(deletionResponse, "Permanent synthetic invoice deletion");
  const deletionResult = await deletionResponse.json();
  assert.deepEqual(deletionResult.deleted_ids, [createdJob.id]);
  assert.deepEqual(deletionBodies, [{
    jobs: [{ id: createdJob.id, revision: createdJob.revision }],
    confirm_permanent: true,
  }]);
  createdJob = null;
  await actualPage.locator("#delete-invoices-dialog").waitFor({ state: "hidden" });
  assert.equal(await actualPage.locator(".job-row").filter({ hasText: filename }).count(), 0);
  assert.deepEqual(actualPageErrors, [], `Deletion page errors: ${actualPageErrors.join("; ")}`);

  console.log(
    `Provider controls and deletion browser test passed (mocked model success/failure/race; one synthetic job deleted; ${baseURL}).`,
  );
} finally {
  if (createdJob && actualContext && !["queued", "processing"].includes(createdJob.status)) {
    await actualContext.request.post(`${baseURL}/api/jobs/delete`, {
      headers: mutationHeaders,
      data: {
        jobs: [{ id: createdJob.id, revision: createdJob.revision }],
        confirm_permanent: true,
      },
    }).catch(() => {});
  }
  await actualContext?.close().catch(() => {});
  if (providerContext !== actualContext) await providerContext?.close().catch(() => {});
  await browser.close();
}
