"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const make = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const HEADER_FIELDS = [
  ["number", "Invoice number", "text"],
  ["supplier_name", "Supplier name", "text"],
  ["seller", "Seller entity", "text"],
  ["site", "Supplier site", "text"],
  ["buyer", "Buyer", "text"],
  ["po", "Purchase order", "text"],
  ["location", "Delivery location", "text"],
  ["date", "Invoice date", "date"],
  ["currency", "Currency", "text"],
  ["origin", "Origin", "text"],
  ["market", "Market", "text"],
  ["taxCode", "Tax code", "text"],
  ["net", "Net total", "number"],
  ["tax", "Tax total", "number"],
];

const STATUS_LABELS = {
  queued: "Queued",
  processing: "Processing",
  review: "Needs review",
  ready: "Ready to export",
  exported: "Exported",
  error: "Needs attention",
};

const app = {
  state: null,
  currentJob: null,
  selectedJobId: null,
  selectedForBatch: new Set(),
  uploadFiles: [],
  pollTimer: null,
  messageTimer: null,
  selectionToken: 0,
  preflight: null,
  authMode: "unknown",
  authProviders: [],
  sessionUser: null,
  sessionVerified: false,
  workspaceStarted: false,
  documentUrl: null,
  documentToken: 0,
};

async function studioFetch(path, options = {}) {
  const config = { ...options, headers: { ...(options.headers || {}) } };
  if (config.method && config.method !== "GET") config.headers["X-Studio-Request"] = "1";
  if (config.body && !(config.body instanceof FormData) && typeof config.body !== "string") {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(config.body);
  }
  if (app.authMode === "cloud" && path.startsWith("/api/")) {
    const token = await window.InvoiceStudioAuth.getToken();
    config.headers.Authorization = `Bearer ${token}`;
  }
  try {
    return await fetch(path, config);
  } catch (_) {
    throw new Error("Invoice Studio could not reach the server.");
  }
}

async function responseError(response) {
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  const detail = body && typeof body === "object" ? body.detail : body;
  return new Error(detail || `Request failed (${response.status})`);
}

async function api(path, options = {}) {
  const response = await studioFetch(path, options);
  if (!response.ok) throw await responseError(response);
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("json") ? await response.json() : await response.text();
  return body;
}

async function apiBlob(path) {
  const response = await studioFetch(path, { method: "GET" });
  if (!response.ok) throw await responseError(response);
  return { blob: await response.blob(), disposition: response.headers.get("content-disposition") || "" };
}

function dispositionFilename(disposition, fallback) {
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try { return decodeURIComponent(encoded); } catch (_) { return fallback; }
  }
  return disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallback;
}

async function downloadApi(path, fallbackName = "download") {
  const { blob, disposition } = await apiBlob(path);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = dispositionFilename(disposition, fallbackName);
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function notify(message, type = "success", duration = 4600) {
  const banner = $("#global-message");
  banner.textContent = message;
  banner.className = `global-message ${type}`;
  banner.hidden = false;
  clearTimeout(app.messageTimer);
  app.messageTimer = setTimeout(() => { banner.hidden = true; }, duration);
}

function formatDate(value) {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? String(value) : parsed.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function formatSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(0.1, bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function humanize(value) {
  const names = {
    invoice2data: "invoice2data",
    paddleocr: "PaddleOCR",
    docling: "Docling",
    openai: "OpenAI",
    anthropic: "Anthropic",
    chatgpt: "ChatGPT",
    claude_local: "Claude Code (local)",
  };
  if (names[value]) return names[value];
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function statusClass(job) {
  if (job.status === "ready" || job.status === "exported") return "success";
  if (job.status === "error") return "error";
  if (job.status === "review" || job.status === "processing" || job.status === "queued") return "warning";
  return "neutral";
}

function isBatchReady(job) {
  return job.status === "ready" && Boolean(job.validation?.ready) && !job.export_id;
}

function navigate(sectionName) {
  $$(".page-section").forEach((section) => {
    const active = section.id === `${sectionName}-section`;
    section.hidden = !active;
    section.classList.toggle("active", active);
  });
  $$('[data-nav]').forEach((button) => {
    const active = button.dataset.nav === sectionName;
    button.classList.toggle("active", active);
    if (button.classList.contains("nav-item")) {
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
  });
  history.replaceState(null, "", `#${sectionName}`);
  $("#main-content").focus({ preventScroll: true });
}

async function loadState({ preserveSelection = true } = {}) {
  const data = await api("/api/state");
  app.state = data;
  const knownIds = new Set(data.jobs.map((job) => job.id));
  app.selectedForBatch.forEach((id) => { if (!knownIds.has(id)) app.selectedForBatch.delete(id); });
  renderJobs();
  renderReferences();
  renderEngines();
  renderPolicy();
  applySettings();
  renderConnections();
  if (preserveSelection && app.selectedJobId) {
    const summary = data.jobs.find((job) => job.id === app.selectedJobId);
    if (!summary) clearSelectedJob();
  }
  return data;
}

function renderJobs() {
  const query = $("#job-search").value.trim().toLowerCase();
  const jobs = [...(app.state?.jobs || [])];
  const filtered = jobs.filter((job) => {
    const invoice = job.invoice || {};
    return [job.filename, invoice.supplier_name, invoice.seller, invoice.number].some((value) => String(value || "").toLowerCase().includes(query));
  });
  $("#job-count").textContent = `${jobs.length} invoice${jobs.length === 1 ? "" : "s"}`;
  $("#job-empty").hidden = jobs.length > 0;
  $("#job-list").hidden = jobs.length === 0;
  $("#batch-controls").hidden = jobs.length === 0;
  const list = $("#job-list");
  list.replaceChildren();

  filtered.forEach((job) => {
    const row = make("div", `job-row${job.id === app.selectedJobId ? " active" : ""}`);
    const select = make("input", "job-select");
    select.type = "checkbox";
    select.checked = app.selectedForBatch.has(job.id);
    select.disabled = !isBatchReady(job);
    select.setAttribute("aria-label", `Select ${job.filename} for batch export`);
    select.addEventListener("change", () => {
      if (select.checked) app.selectedForBatch.add(job.id);
      else app.selectedForBatch.delete(job.id);
      updateBatchControls();
    });

    const button = make("button", "job-item");
    button.type = "button";
    button.setAttribute("aria-label", `Review ${job.filename}`);
    const extension = (job.filename.split(".").pop() || "DOC").slice(0, 4).toUpperCase();
    button.append(make("span", "job-icon", extension));
    const copy = make("span", "job-copy");
    copy.append(make("strong", "", job.invoice?.supplier_name || job.invoice?.seller || job.filename));
    const secondary = job.invoice?.number ? `${job.invoice.number} · ${STATUS_LABELS[job.status] || humanize(job.status)}` : STATUS_LABELS[job.status] || humanize(job.status);
    copy.append(make("span", "", secondary));
    button.append(copy);
    const dot = make("span", `status-dot ${job.status === "error" ? "error" : ["processing", "queued"].includes(job.status) ? "processing" : ""}`);
    dot.setAttribute("aria-hidden", "true");
    button.append(dot);
    button.addEventListener("click", () => selectJob(job.id));
    row.append(select, button);
    list.append(row);
  });
  if (jobs.length && !filtered.length) list.append(make("p", "table-empty", "No invoices match this search."));
  updateBatchControls();
}

function updateBatchControls() {
  const valid = [...app.selectedForBatch].filter((id) => isBatchReady(app.state.jobs.find((job) => job.id === id) || {}));
  if (valid.length !== app.selectedForBatch.size) app.selectedForBatch = new Set(valid);
  $("#selected-count").textContent = `${valid.length} selected`;
  $("#batch-export").disabled = valid.length === 0;
  const ready = app.state?.jobs?.filter(isBatchReady) || [];
  const allSelected = ready.length > 0 && ready.every((job) => app.selectedForBatch.has(job.id));
  $("#select-ready").textContent = allSelected ? "Clear selection" : `Select ready${ready.length ? ` (${ready.length})` : ""}`;
  $("#select-ready").disabled = ready.length === 0;
}

async function selectJob(id) {
  const token = ++app.selectionToken;
  app.selectedJobId = id;
  renderJobs();
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (token !== app.selectionToken) return;
    app.currentJob = job;
    renderSelectedJob(job);
    startPollingIfNeeded();
  } catch (error) {
    notify(error.message, "error");
  }
}

function clearSelectedJob() {
  app.selectedJobId = null;
  app.currentJob = null;
  $("#workspace-empty").hidden = false;
  $("#review-panel").hidden = true;
  if (app.documentUrl) URL.revokeObjectURL(app.documentUrl);
  app.documentUrl = null;
  app.documentToken += 1;
  clearInterval(app.pollTimer);
}

function renderSelectedJob(job) {
  $("#workspace-empty").hidden = true;
  $("#review-panel").hidden = false;
  $("#selected-filename").textContent = job.filename;
  $("#selected-file-icon").textContent = (job.filename.split(".").pop() || "DOC").slice(0, 4).toUpperCase();
  const engine = job.selected_engine && job.selected_engine !== "pending" ? humanize(job.selected_engine) : "Engine pending";
  $("#selected-meta").textContent = `${engine}${job.created_at ? ` · Added ${formatDate(job.created_at)}` : ""}`;
  const status = $("#selected-status");
  status.textContent = STATUS_LABELS[job.status] || humanize(job.status);
  status.className = `status-pill ${statusClass(job)}`;
  $("#retry-job").disabled = ["queued", "processing", "exported"].includes(job.status);

  const processing = ["queued", "processing"].includes(job.status);
  $("#processing-banner").hidden = !processing;
  if (processing) {
    $("#processing-title").textContent = job.status === "queued" ? "Waiting for an extraction slot…" : `${humanize(job.progress?.engine || "engine")} is reading the invoice…`;
    $("#processing-detail").textContent = job.progress?.message || "The result will appear here when it is ready.";
  }
  $("#job-error").hidden = !job.error;
  $("#job-error").textContent = job.error || "";

  renderDocument(job).catch((error) => notify(error.message, "error"));
  renderInvoiceForm(job);
  renderValidation(job);
  renderTrace(job);
  renderReviewActions(job);
}

async function renderDocument(job) {
  const token = ++app.documentToken;
  const endpoint = `/api/jobs/${encodeURIComponent(job.id)}/document`;
  $("#open-document").removeAttribute("href");
  $("#download-original").removeAttribute("href");
  const ext = (job.filename.split(".").pop() || "").toLowerCase();
  const canPreview = ["pdf", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff"].includes(ext);
  $("#document-frame").hidden = !canPreview;
  $("#document-download-card").hidden = canPreview;
  $("#document-frame").removeAttribute("src");
  const text = String(job.text || "").trim();
  $("#extracted-text").textContent = text;
  $("#extracted-text-wrap").hidden = !text;
  const { blob } = await apiBlob(endpoint);
  if (token !== app.documentToken || app.currentJob?.id !== job.id) return;
  if (app.documentUrl) URL.revokeObjectURL(app.documentUrl);
  app.documentUrl = URL.createObjectURL(blob);
  $("#open-document").href = app.documentUrl;
  $("#download-original").href = app.documentUrl;
  if (canPreview) $("#document-frame").src = app.documentUrl;
}

function renderInvoiceForm(job) {
  const invoice = job.invoice || {};
  const fields = $("#header-fields");
  fields.replaceChildren();
  HEADER_FIELDS.forEach(([name, label, type]) => {
    const wrapper = make("label", "field-wrap");
    wrapper.append(make("span", "", label));
    const input = make("input");
    input.name = name;
    input.type = type;
    if (type === "number") input.step = "any";
    if (name === "currency") { input.maxLength = 3; input.autocapitalize = "characters"; }
    input.value = invoice[name] ?? "";
    input.disabled = ["queued", "processing", "exported"].includes(job.status);
    wrapper.append(input);
    fields.append(wrapper);
  });
  const lines = $("#line-items");
  lines.replaceChildren();
  (invoice.lines || []).forEach((line) => addLine(line, ["queued", "processing", "exported"].includes(job.status)));
  $("#no-lines").hidden = Boolean(invoice.lines?.length);
  $("#add-line").disabled = ["queued", "processing", "exported"].includes(job.status);
  const raw = Number(job.completeness);
  const percent = Number.isFinite(raw) ? Math.round(Math.max(0, Math.min(1, raw)) * 100) : null;
  $("#completeness strong").textContent = percent === null ? "—" : `${percent}%`;
}

function lineInput(name, value, label, type = "text") {
  const input = make("input");
  input.name = name;
  input.value = value ?? "";
  input.type = type;
  if (type === "number") input.step = "any";
  input.setAttribute("aria-label", label);
  return input;
}

function addLine(line = {}, disabled = false) {
  const row = make("tr");
  const identity = make("td");
  identity.append(lineInput("sku", line.sku, "Supplier SKU"), lineInput("gtin", line.gtin, "GTIN or barcode"));
  const description = make("td");
  description.append(lineInput("description", line.description, "Description"));
  const qty = make("td"); qty.append(lineInput("qty", line.qty, "Quantity", "number"));
  const uom = make("td"); uom.append(lineInput("uom", line.uom, "Unit of measure"));
  const price = make("td"); price.append(lineInput("price", line.price, "Unit price", "number"));
  const evidence = make("td");
  evidence.append(lineInput("evidence", line.evidence, "Evidence text"), lineInput("page", line.page, "Evidence page", "number"));
  const action = make("td");
  const remove = make("button", "remove-line", "×");
  remove.type = "button";
  remove.setAttribute("aria-label", "Remove line item");
  remove.addEventListener("click", () => { row.remove(); $("#no-lines").hidden = Boolean($("#line-items").children.length); });
  action.append(remove);
  $$('input', row).forEach((input) => { input.disabled = disabled; });
  remove.disabled = disabled;
  row.append(identity, description, qty, uom, price, evidence, action);
  $("#line-items").append(row);
  $("#no-lines").hidden = true;
}

function renderValidation(job) {
  const issues = job.validation?.issues || [];
  const summary = $("#validation-summary");
  summary.replaceChildren();
  if (!issues.length) {
    if (job.status === "ready" || job.status === "exported") {
      summary.hidden = false;
      summary.className = "validation-summary";
      summary.append(make("strong", "", "Validation passed"), make("span", "", "Reference checks, totals and evidence review are complete."));
    } else summary.hidden = true;
    return;
  }
  summary.hidden = false;
  summary.className = `validation-summary${issues.some((issue) => issue.code !== "REVIEW") ? " error" : ""}`;
  summary.append(make("strong", "", `${issues.length} issue${issues.length === 1 ? "" : "s"} to resolve`));
  const list = make("ul");
  issues.forEach((issue) => {
    const line = issue.line ? `Line ${issue.line}: ` : "";
    const owner = issue.owner ? ` · ${issue.owner}` : "";
    list.append(make("li", "", `${line}${issue.message}${owner}`));
  });
  summary.append(list);
}

function renderTrace(job) {
  const trace = job.trace || [];
  const traceList = $("#trace-list");
  traceList.replaceChildren();
  if (!trace.length) traceList.append(make("li", "", "No extraction attempts recorded yet."));
  trace.forEach((entry) => {
    if (typeof entry === "string") traceList.append(make("li", "", entry));
    else {
      const engine = humanize(entry.engine || "engine");
      const model = entry.model ? ` · ${entry.model}` : "";
      const status = humanize(entry.status || "attempted");
      const completeness = entry.completeness === undefined ? "" : ` · ${Math.round(Number(entry.completeness) * 100)}% completeness`;
      const reason = entry.reason ? ` — ${entry.reason}` : "";
      traceList.append(make("li", "", `${engine}${model}: ${status}${completeness}${reason}`));
    }
  });
  const chosen = trace.findLast?.((entry) => entry && typeof entry === "object" && entry.status === "extracted") || trace.find((entry) => entry && typeof entry === "object" && entry.status === "extracted");
  $("#trace-summary").textContent = chosen ? `Chosen: ${humanize(chosen.engine)}${chosen.model ? ` · ${chosen.model}` : ""}` : "How this result was produced";

  const provenance = $("#provenance-list");
  provenance.replaceChildren();
  const entries = Array.isArray(job.provenance) ? job.provenance : Object.entries(job.provenance || {}).map(([field, source]) => ({ field, source }));
  if (!entries.length) {
    provenance.append(make("dt", "", "None"), make("dd", "", "No reference-derived fields recorded."));
  } else entries.forEach((entry) => {
    provenance.append(make("dt", "", humanize(entry.field)), make("dd", "", `${entry.source || "Extracted"}${entry.value ? ` → ${entry.value}` : ""}`));
  });
}

function renderReviewActions(job) {
  const busy = ["queued", "processing"].includes(job.status);
  const exported = job.status === "exported" || Boolean(job.export_id);
  $("#confirm-review").checked = Boolean(job.reviewed);
  $("#confirm-review").disabled = busy || exported;
  $("#save-review").disabled = busy || exported || job.status === "error";
  $("#export-job").disabled = !job.validation?.ready && !exported;
  $("#export-job").textContent = exported ? "Download Excel" : "Export Excel";
}

function collectInvoice() {
  const invoice = {};
  HEADER_FIELDS.forEach(([name, , type]) => {
    const value = $(`#header-fields [name="${name}"]`).value.trim();
    invoice[name] = value === "" ? null : value;
    if (type === "number" && value !== "") invoice[name] = value;
  });
  invoice.lines = $$("#line-items tr").map((row) => {
    const value = (name) => {
      const raw = $(`[name="${name}"]`, row).value.trim();
      return raw === "" ? null : raw;
    };
    const page = value("page");
    return { sku: value("sku"), gtin: value("gtin"), description: value("description"), qty: value("qty"), uom: value("uom"), price: value("price"), evidence: value("evidence"), page: page === null ? null : Number(page) };
  });
  return invoice;
}

async function saveReview() {
  if (!app.currentJob) return;
  const button = $("#save-review");
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(app.currentJob.id)}/review`, {
      method: "POST",
      body: { invoice: collectInvoice(), revision: app.currentJob.revision, confirm: $("#confirm-review").checked },
    });
    app.currentJob = job;
    replaceJobSummary(job);
    renderSelectedJob(job);
    notify(job.validation?.ready ? "Review confirmed. This invoice is ready to export." : "Saved and revalidated. Resolve the listed issues before export.", job.validation?.ready ? "success" : "error");
  } catch (error) {
    notify(error.message, "error", 7000);
    if (error.message.includes("changed")) await selectJob(app.currentJob.id);
  } finally {
    button.textContent = "Save & revalidate";
    button.disabled = false;
  }
}

function replaceJobSummary(job) {
  const index = app.state.jobs.findIndex((item) => item.id === job.id);
  if (index >= 0) app.state.jobs[index] = { ...app.state.jobs[index], ...job };
  else app.state.jobs.unshift(job);
  renderJobs();
}

async function exportCurrentJob() {
  if (!app.currentJob) return;
  const button = $("#export-job");
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(app.currentJob.id)}/export`, { method: "POST", body: { revision: app.currentJob.revision } });
    await downloadApi(result.url, `Merch_Inv_${result.id.slice(0, 8)}.xlsx`);
    await loadState();
    await selectJob(app.currentJob.id);
    notify("Excel workbook prepared.");
  } catch (error) {
    notify(error.message, "error", 7000);
  } finally {
    button.textContent = "Export Excel";
    button.disabled = false;
  }
}

async function exportBatch() {
  const jobs = [...app.selectedForBatch].map((id) => app.state.jobs.find((job) => job.id === id)).filter(isBatchReady);
  if (!jobs.length) return;
  const button = $("#batch-export");
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const result = await api("/api/exports/batch", { method: "POST", body: { jobs: jobs.map((job) => ({ id: job.id, revision: job.revision })) } });
    await downloadApi(result.url, `Merch_Inv_Batch_${result.id.slice(0, 8)}.xlsx`);
    app.selectedForBatch.clear();
    await loadState();
    notify(`${result.count} invoices exported in one workbook.`);
  } catch (error) {
    notify(error.message, "error", 8000);
    await loadState();
  } finally {
    button.textContent = "Export selected";
    button.disabled = false;
  }
}

function startPollingIfNeeded() {
  clearInterval(app.pollTimer);
  if (!app.currentJob || !["queued", "processing"].includes(app.currentJob.status)) return;
  app.pollTimer = setInterval(async () => {
    if (!app.currentJob) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(app.currentJob.id)}`);
      app.currentJob = job;
      replaceJobSummary(job);
      renderSelectedJob(job);
      if (!["queued", "processing"].includes(job.status)) {
        clearInterval(app.pollTimer);
        notify(job.status === "error" ? "Invoice processing needs attention." : "Invoice extraction finished.", job.status === "error" ? "error" : "success");
      }
    } catch (_) {
      clearInterval(app.pollTimer);
    }
  }, 1400);
}

function queueFiles(fileList) {
  const allowed = new Set(["pdf", "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "docx", "xlsx", "csv", "txt", "json"]);
  const existing = new Set(app.uploadFiles.map((file) => `${file.name}:${file.size}:${file.lastModified}`));
  [...fileList].forEach((file) => {
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (!allowed.has(ext)) notify(`${file.name}: unsupported file type.`, "error");
    else if (file.size > 12_000_000) notify(`${file.name}: file exceeds 12 MB.`, "error");
    else if (!existing.has(key)) { app.uploadFiles.push(file); existing.add(key); }
  });
  renderUploadQueue();
}

function renderUploadQueue() {
  const queue = $("#upload-queue");
  queue.replaceChildren();
  queue.hidden = !app.uploadFiles.length;
  app.uploadFiles.forEach((file, index) => {
    const row = make("div", "queue-item");
    row.append(make("span", "", `${file.name} · ${formatSize(file.size)}`));
    const remove = make("button", "", "Remove");
    remove.type = "button";
    remove.addEventListener("click", () => { app.uploadFiles.splice(index, 1); renderUploadQueue(); });
    row.append(remove);
    queue.append(row);
  });
  updateUploadReadiness();
}

function providerReady(provider, model) {
  return Boolean(app.state?.connections?.[provider]) && Boolean(String(model || "").trim());
}

function updateUploadReadiness() {
  const needsAI = $("#upload-ai-fallback").checked || $("#upload-engine").value === "ai";
  const provider = $("#upload-provider").value;
  const model = $("#upload-model").value.trim();
  $("#upload-ai-options").hidden = !needsAI;
  const prompt = $("#upload-ai-readiness");
  if (needsAI && !providerReady(provider, model)) {
    const connection = app.state?.connections?.[provider];
    prompt.textContent = !connection ? `${humanize(provider)} is not connected. Local extraction can still run; if it cannot finish, the invoice will wait for a connection or manual review.` : "No model is selected. Local extraction can still run; choose a model to make AI fallback available.";
    prompt.hidden = false;
  } else prompt.hidden = true;
  $("#start-upload").disabled = !app.uploadFiles.length;
}

function openUpload() {
  const settings = app.state?.settings || { provider: "openai", model: "", ai_fallback: true };
  $("#upload-provider").value = settings.provider || "openai";
  $("#upload-model").value = settings.model || "";
  $("#upload-ai-fallback").checked = settings.ai_fallback !== false;
  $("#upload-engine").value = "auto";
  $("#upload-language").value = "en";
  $("#upload-progress").hidden = true;
  updateUploadReadiness();
  $("#upload-dialog").showModal();
}

function processingOptions(prefix = "upload") {
  return {
    engine: $(`#${prefix}-engine`).value,
    ai_fallback: $(`#${prefix}-ai-fallback`).checked,
    provider: $(`#${prefix}-provider`).value,
    model: $(`#${prefix}-model`).value.trim(),
    language: $(`#${prefix}-language`).value,
  };
}

async function beginUploadPreflight(event) {
  event.preventDefault();
  if (!app.uploadFiles.length) return;
  const options = processingOptions();
  try {
    await requestPreflight({
      kind: "upload",
      options,
      files: app.uploadFiles.map((file) => ({ name: file.name, size: file.size })),
    });
  } catch (error) {
    notify(error.message, "error", 7000);
  }
}

async function runUpload(preflightToken, options) {
  const progress = $("#upload-progress");
  const button = $("#start-upload");
  $("#upload-dialog").showModal();
  progress.hidden = false;
  button.disabled = true;
  const completed = [];
  const failed = [];
  for (let index = 0; index < app.uploadFiles.length; index += 1) {
    const file = app.uploadFiles[index];
    progress.textContent = `Uploading ${index + 1} of ${app.uploadFiles.length}: ${file.name}`;
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("options", JSON.stringify(options));
    form.append("preflight_token", preflightToken);
    try {
      completed.push(await api("/api/invoices", { method: "POST", body: form }));
    } catch (error) {
      failed.push(`${file.name}: ${error.message}`);
    }
  }
  app.uploadFiles = [];
  renderUploadQueue();
  await loadState({ preserveSelection: false });
  if (completed.length) await selectJob(completed[0].id);
  if (!failed.length) {
    progress.textContent = `${completed.length} invoice${completed.length === 1 ? "" : "s"} added. Processing continues in the workspace.`;
    notify(`${completed.length} invoice${completed.length === 1 ? "" : "s"} added for sequential processing.`);
    setTimeout(() => $("#upload-dialog").close(), 500);
  } else {
    progress.textContent = failed.join(" ");
    notify(`${failed.length} file${failed.length === 1 ? "" : "s"} could not be added.`, "error", 8000);
  }
}

async function prepareDemo() {
  const buttons = [$("#load-demo")];
  buttons.forEach((button) => { button.disabled = true; button.textContent = "Preparing demo…"; });
  try {
    await api("/api/references/demo", { method: "POST", body: {} });
    await loadState();
    const sample = app.state.samples?.demo;
    if (!sample) throw new Error("Demo invoice metadata is unavailable.");
    await requestPreflight({
      kind: "demo",
      options: { engine: "auto", ai_fallback: true, provider: "openai", model: "", language: "en" },
      files: [{ name: sample.name, size: sample.size }],
    });
  } catch (error) {
    notify(error.message, "error", 7000);
  } finally {
    buttons.forEach((button) => { button.disabled = false; button.textContent = "Try synthetic demo"; });
  }
}

async function runDemo(preflightToken) {
  const job = await api("/api/demo", { method: "POST", body: { preflight_token: preflightToken } });
  await loadState({ preserveSelection: false });
  await selectJob(job.id);
  notify("Synthetic reference data and invoice loaded. No real business data was used.");
}

function renderPreflight(plan, request) {
  const files = $("#preflight-files");
  files.replaceChildren();
  request.files.forEach((file) => files.append(make("span", "preflight-file", `${file.name} · ${formatSize(file.size)}`)));

  const summary = $("#preflight-summary");
  summary.replaceChildren();
  const details = [
    ["Files", String(plan.summary.file_count)],
    ["Engine", humanize(plan.summary.engine)],
    ["AI fallback", plan.summary.ai_fallback ? "Allowed" : "Disabled (local only)"],
    ["Provider", plan.summary.ai_fallback || plan.summary.engine === "ai" ? humanize(plan.summary.provider) : "Not used"],
    ["Model", plan.summary.ai_fallback || plan.summary.engine === "ai" ? (plan.summary.model || "Not selected") : "Not used"],
    ["References", plan.summary.reference_version ? `Version ${String(plan.summary.reference_version).slice(0, 12)}` : "Not loaded — export will be held"],
    ["Price tolerance", String(plan.summary.policy?.price_tolerance ?? "—")],
    ["Total tolerance", String(plan.summary.policy?.total_tolerance ?? "—")],
  ];
  details.forEach(([term, description]) => {
    const row = make("div");
    row.append(make("dt", "", term), make("dd", "", description));
    summary.append(row);
  });

  const populateNotes = (selector, notes) => {
    const block = $(selector);
    block.hidden = !notes?.length;
    const list = $("ul", block);
    list.replaceChildren();
    (notes || []).forEach((note) => list.append(make("li", "", note)));
  };
  populateNotes("#preflight-warnings", plan.warnings);
  populateNotes("#preflight-blocking", plan.blocking);
  $("#confirm-preflight").disabled = Boolean(plan.blocking?.length);
}

async function requestPreflight(request) {
  const plan = await api("/api/preflight", { method: "POST", body: { options: request.options, files: request.files } });
  app.preflight = { ...request, token: plan.token };
  renderPreflight(plan, request);
  if ($("#upload-dialog").open) $("#upload-dialog").close();
  if ($("#retry-dialog").open) $("#retry-dialog").close();
  $("#preflight-dialog").showModal();
}

async function confirmPreflight(event) {
  event.preventDefault();
  const pending = app.preflight;
  if (!pending) return;
  const button = $("#confirm-preflight");
  button.disabled = true;
  button.textContent = "Confirming…";
  try {
    await api("/api/preflight/confirm", { method: "POST", body: { token: pending.token } });
    $("#preflight-dialog").close();
    app.preflight = null;
    if (pending.kind === "upload") await runUpload(pending.token, pending.options);
    else if (pending.kind === "demo") await runDemo(pending.token);
    else if (pending.kind === "retry") await runRetry(pending.token, pending.options);
  } catch (error) {
    notify(error.message, "error", 8000);
  } finally {
    button.textContent = "Confirm rules and process";
    button.disabled = false;
  }
}

function cancelPreflight() {
  const pending = app.preflight;
  app.preflight = null;
  if (pending?.kind === "upload" && !$("#upload-dialog").open) $("#upload-dialog").showModal();
  if (pending?.kind === "retry" && !$("#retry-dialog").open) $("#retry-dialog").showModal();
}

function renderReferences() {
  const refs = app.state?.references;
  $("#reference-status").textContent = refs ? `Version ${String(refs.version || "loaded").slice(0, 10)}` : "Not loaded";
  $("#reference-status").className = `status-pill ${refs ? "success" : "neutral"}`;
  const summary = $("#reference-summary");
  summary.replaceChildren();
  summary.hidden = !refs;
  if (refs) Object.entries(refs.counts || {}).forEach(([name, count]) => {
    const stat = make("div", "reference-stat");
    stat.append(make("strong", "", String(count)), make("span", "", humanize(name)));
    summary.append(stat);
  });
}

function renderPolicy() {
  if (!app.state?.policy) return;
  const form = $("#policy-form");
  form.elements.price_tolerance.value = app.state.policy.price_tolerance ?? "0";
  form.elements.total_tolerance.value = app.state.policy.total_tolerance ?? "0.01";
  form.elements.currency_decimals.value = JSON.stringify(app.state.policy.currency_decimals || {}, null, 2);
}

function renderEngines() {
  const list = $("#engine-list");
  list.replaceChildren();
  (app.state?.engines || []).forEach((engine) => {
    const item = make("div", "engine-item");
    const head = make("div", "engine-item-head");
    head.append(make("strong", "", humanize(engine.id)));
    const dot = make("span", `availability${engine.installed ? " yes" : ""}`);
    dot.title = engine.installed ? "Installed" : "Not installed";
    head.append(dot);
    item.append(head, make("p", "", engine.note || (engine.installed ? "Available" : "Not installed")));
    if (engine.version) item.append(make("small", "", `Version ${engine.version}`));
    list.append(item);
  });
}

function renderConnections() {
  const connections = app.state?.connections || {};
  Object.entries(connections).forEach(([provider, connected]) => {
    const badge = $(`[data-connection-state="${provider}"]`);
    if (!badge) return;
    badge.textContent = connected ? (provider === "claude_local" ? "Detected" : "Connected") : (provider === "claude_local" ? "Not detected" : "Not connected");
    badge.classList.toggle("connected", connected);
  });
  const account = $("#chatgpt-account");
  account.replaceChildren(new Option("No connected accounts", ""));
  (app.state?.accounts || []).forEach((entry) => {
    const option = new Option(`${entry.email}${entry.connected ? "" : " (disconnected)"}`, entry.id);
    option.disabled = !entry.connected;
    option.selected = entry.id === app.state.active_account;
    account.add(option);
  });
  $("#remove-chatgpt").disabled = !account.value;
  const cloud = app.authMode === "cloud";
  if (cloud) {
    const badge = $('[data-connection-state="chatgpt"]');
    badge.textContent = "Local mode only";
    badge.classList.remove("connected");
    $("#connect-chatgpt").disabled = true;
    $("#connect-chatgpt").textContent = "Local mode only";
    $("#remove-chatgpt").disabled = true;
    $("#chatgpt-account").disabled = true;
    const claude = $('[data-connection-state="claude_local"]');
    claude.textContent = "Local mode only";
    claude.classList.remove("connected");
  } else {
    $("#connect-chatgpt").disabled = false;
    $("#connect-chatgpt").textContent = "Connect ChatGPT";
    $("#chatgpt-account").disabled = false;
  }
  ["settings-provider", "upload-provider", "retry-provider"].forEach((id) => {
    const select = $(`#${id}`);
    ["chatgpt", "claude_local"].forEach((value) => {
      const option = $(`option[value="${value}"]`, select);
      if (option) option.disabled = cloud;
    });
    if (cloud && ["chatgpt", "claude_local"].includes(select.value)) select.value = "openai";
  });
}

function applySettings() {
  const settings = app.state?.settings || {};
  $("#settings-provider").value = settings.provider || "openai";
  $("#settings-model").value = settings.model || "";
  $("#settings-ai-fallback").checked = settings.ai_fallback !== false;
  if (!$("#upload-dialog").open) {
    $("#upload-provider").value = settings.provider || "openai";
    $("#upload-model").value = settings.model || "";
    $("#upload-ai-fallback").checked = settings.ai_fallback !== false;
  }
}

async function importFile(input, endpoint, successMessage) {
  if (!input.files[0]) return;
  const form = new FormData();
  form.append("file", input.files[0], input.files[0].name);
  await api(endpoint, { method: "POST", body: form });
  input.value = "";
  await loadState();
  notify(successMessage);
}

async function savePolicy(event) {
  event.preventDefault();
  const form = event.currentTarget;
  let decimals;
  try { decimals = JSON.parse(form.elements.currency_decimals.value); }
  catch (_) { notify("Currency decimals must be a valid JSON object.", "error"); return; }
  try {
    await api("/api/policy", { method: "POST", body: { price_tolerance: form.elements.price_tolerance.value, total_tolerance: form.elements.total_tolerance.value, currency_decimals: decimals } });
    await loadState();
    notify("Validation policy saved. Unexported invoices require a new evidence review.");
  } catch (error) { notify(error.message, "error", 7000); }
}

async function saveProviderSettings(event) {
  event.preventDefault();
  try {
    const body = { provider: $("#settings-provider").value, model: $("#settings-model").value.trim(), ai_fallback: $("#settings-ai-fallback").checked };
    await api("/api/settings", { method: "POST", body });
    await loadState();
    notify("Processing defaults saved.");
  } catch (error) { notify(error.message, "error"); }
}

async function fetchModels() {
  const provider = $("#settings-provider").value;
  const button = $("#fetch-models");
  button.disabled = true;
  button.textContent = "Loading…";
  try {
    const result = await api(`/api/connections/${encodeURIComponent(provider)}/models`);
    const list = $("#model-list");
    list.replaceChildren(new Option("Select a model", ""));
    (result.models || []).forEach((model) => list.add(new Option(model.name || model.id, model.id)));
    list.hidden = false;
    notify(result.models?.length ? "Available models loaded." : "The connection returned no model choices.", result.models?.length ? "success" : "error");
  } catch (error) { notify(error.message, "error", 7000); }
  finally { button.disabled = false; button.textContent = "Load available models"; }
}

async function saveApiKey(form) {
  const provider = form.dataset.provider;
  const input = form.elements.api_key;
  const apiKey = input.value;
  input.value = "";
  try {
    await api(`/api/connections/${provider}`, { method: "POST", body: { api_key: apiKey } });
    await loadState();
    notify(`${provider === "openai" ? "OpenAI" : "Anthropic"} API connection saved on the server.`);
  } catch (error) { notify(error.message, "error", 7000); }
}

async function removeApiConnection(provider) {
  try {
    await api(`/api/connections/${provider}`, { method: "DELETE" });
    await loadState();
    notify(`${provider === "openai" ? "OpenAI" : "Anthropic"} API connection removed.`);
  } catch (error) { notify(error.message, "error"); }
}

async function connectChatGPT() {
  try {
    const result = await api("/api/chatgpt/start", { method: "POST", body: { account_id: $("#chatgpt-account").value || null } });
    window.location.assign(result.url);
  } catch (error) { notify(error.message, "error", 7000); }
}

async function selectChatGPTAccount() {
  const id = $("#chatgpt-account").value;
  $("#remove-chatgpt").disabled = !id;
  if (!id) return;
  try { await api("/api/chatgpt/select", { method: "POST", body: { id } }); await loadState(); notify("Active ChatGPT account changed."); }
  catch (error) { notify(error.message, "error"); }
}

async function removeChatGPT() {
  const id = $("#chatgpt-account").value;
  if (!id) return;
  try { await api(`/api/chatgpt/${encodeURIComponent(id)}`, { method: "DELETE" }); await loadState(); notify("ChatGPT account removed."); }
  catch (error) { notify(error.message, "error"); }
}

async function loadAudit() {
  if (!app.currentJob) return;
  const list = $("#audit-list");
  list.replaceChildren(make("li", "", "Loading audit entries…"));
  try {
    const entries = await api(`/api/jobs/${encodeURIComponent(app.currentJob.id)}/audit`);
    list.replaceChildren();
    if (!entries.length) list.append(make("li", "", "No audit entries yet."));
    entries.forEach((entry) => list.append(make("li", "", `${formatDate(entry.at)} · ${humanize(entry.event)}`)));
  } catch (error) { list.replaceChildren(make("li", "", error.message)); }
}

async function submitRetry(event) {
  event.preventDefault();
  if (!app.currentJob) return;
  try {
    await requestPreflight({
      kind: "retry",
      options: processingOptions("retry"),
      files: [{ name: app.currentJob.filename, size: app.currentJob.size }],
    });
  } catch (error) { notify(error.message, "error", 7000); }
}

async function runRetry(preflightToken, options) {
  const job = await api(`/api/jobs/${encodeURIComponent(app.currentJob.id)}/retry`, { method: "POST", body: { options, preflight_token: preflightToken } });
  app.currentJob = job;
  replaceJobSummary(job);
  renderSelectedJob(job);
  startPollingIfNeeded();
  notify("Invoice queued for reprocessing.");
}

function bindEvents() {
  $$('[data-nav]').forEach((control) => control.addEventListener("click", (event) => { event.preventDefault(); navigate(control.dataset.nav); }));
  $$('[data-open-upload]').forEach((button) => button.addEventListener("click", openUpload));
  $("#job-search").addEventListener("input", renderJobs);
  $("#load-demo").addEventListener("click", prepareDemo);
  $("#add-line").addEventListener("click", () => addLine());
  $("#save-review").addEventListener("click", saveReview);
  $("#export-job").addEventListener("click", exportCurrentJob);
  $("#batch-export").addEventListener("click", exportBatch);
  $("#select-ready").addEventListener("click", () => {
    const ready = app.state.jobs.filter(isBatchReady);
    const all = ready.length && ready.every((job) => app.selectedForBatch.has(job.id));
    app.selectedForBatch = all ? new Set() : new Set(ready.map((job) => job.id));
    renderJobs();
  });

  const fileInput = $("#invoice-files");
  fileInput.addEventListener("change", () => { queueFiles(fileInput.files); fileInput.value = ""; });
  const dropZone = $("#drop-zone");
  ["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove("dragover"); }));
  dropZone.addEventListener("drop", (event) => queueFiles(event.dataTransfer.files));
  ["#upload-ai-fallback", "#upload-engine", "#upload-provider", "#upload-model"].forEach((selector) => $(selector).addEventListener("input", updateUploadReadiness));
  $("#upload-form").addEventListener("submit", beginUploadPreflight);

  $("#retry-job").addEventListener("click", () => {
    const options = app.currentJob?.options || app.state.settings;
    $("#retry-engine").value = options.engine || "auto";
    $("#retry-language").value = options.language || "en";
    $("#retry-ai-fallback").checked = options.ai_fallback !== false;
    $("#retry-provider").value = options.provider || "openai";
    $("#retry-model").value = options.model || "";
    $("#retry-dialog").showModal();
  });
  $("#retry-form").addEventListener("submit", submitRetry);
  $("#preflight-form").addEventListener("submit", confirmPreflight);
  const closePreflight = () => { $("#preflight-dialog").close("cancel"); cancelPreflight(); };
  $("#preflight-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closePreflight(); });
  $("#cancel-preflight").addEventListener("click", closePreflight);
  $("#cancel-preflight-x").addEventListener("click", closePreflight);

  $(".trace-card").addEventListener("toggle", (event) => { if (event.currentTarget.open) loadAudit(); });
  $("#reference-file").addEventListener("change", (event) => { $("#reference-file-name").textContent = event.target.files[0]?.name || "No file chosen"; $("#upload-reference").disabled = !event.target.files[0]; });
  $("#upload-reference").addEventListener("click", async () => {
    try { await importFile($("#reference-file"), "/api/references", "References imported. Unexported invoices require a new review."); $("#reference-file-name").textContent = "No file chosen"; $("#upload-reference").disabled = true; }
    catch (error) { notify(error.message, "error", 7000); }
  });
  $("#load-demo-references").addEventListener("click", async () => {
    try { await api("/api/references/demo", { method: "POST", body: {} }); await loadState(); notify("Synthetic reference data loaded. It is for demonstration only."); }
    catch (error) { notify(error.message, "error"); }
  });
  $("#template-file").addEventListener("change", (event) => { $("#template-file-name").textContent = event.target.files[0]?.name || "No file chosen"; $("#upload-template").disabled = !event.target.files[0]; });
  $("#upload-template").addEventListener("click", async () => {
    try { await importFile($("#template-file"), "/api/templates", "Supplier template registered."); $("#template-file-name").textContent = "No file chosen"; $("#upload-template").disabled = true; }
    catch (error) { notify(error.message, "error", 7000); }
  });
  $("#policy-form").addEventListener("submit", savePolicy);
  $("#provider-settings-form").addEventListener("submit", saveProviderSettings);
  $("#fetch-models").addEventListener("click", fetchModels);
  $("#model-list").addEventListener("change", (event) => { if (event.target.value) $("#settings-model").value = event.target.value; });
  $$(".api-key-form").forEach((form) => {
    form.addEventListener("submit", (event) => { event.preventDefault(); saveApiKey(form); });
    $(".remove-connection", form).addEventListener("click", () => removeApiConnection(form.dataset.provider));
  });
  $("#connect-chatgpt").addEventListener("click", connectChatGPT);
  $("#chatgpt-account").addEventListener("change", selectChatGPTAccount);
  $("#remove-chatgpt").addEventListener("click", removeChatGPT);
  $$('[data-api-download]').forEach((link) => link.addEventListener("click", async (event) => {
    event.preventDefault();
    try { await downloadApi(new URL(link.href).pathname, link.dataset.apiDownload || "download"); }
    catch (error) { notify(error.message, "error", 7000); }
  }));
  $("#google-sign-in").addEventListener("click", signInGoogle);
  $("#password-sign-in").addEventListener("submit", signInPassword);
  $("#password-reset").addEventListener("click", resetPassword);
  $("#auth-retry").addEventListener("click", initializeAccess);
  $("#auth-sign-out").addEventListener("click", signOutCloud);
  $("#cloud-sign-out").addEventListener("click", signOutCloud);
}

function showAuthGate({ message, detail = "", busy = false, google = false, password = false, retry = false, signOut = false }) {
  $("#app-shell").hidden = true;
  $("#auth-gate").hidden = false;
  $("#auth-message").textContent = message;
  $("#auth-spinner").hidden = !busy;
  $("#google-sign-in").hidden = !google;
  $("#password-sign-in").hidden = !password;
  $("#auth-retry").hidden = !retry;
  $("#auth-sign-out").hidden = !signOut;
  $("#auth-detail").hidden = !detail;
  $("#auth-detail").textContent = detail;
}

function cloudIdentity(session) {
  const email = String(session.email || "Verified user");
  $("#cloud-user-email").textContent = email;
  $("#cloud-user-avatar").textContent = email.slice(0, 1).toUpperCase();
  $("#cloud-user").hidden = false;
  $("#runtime-badge > span:last-child").textContent = "Cloud workspace";
}

async function startWorkspace() {
  const params = new URLSearchParams(location.search);
  if (params.get("connected") === "chatgpt") notify("ChatGPT account connected.");
  if (params.get("connection_error")) notify(params.get("connection_error"), "error", 8000);
  if (params.size) history.replaceState(null, "", location.pathname + (location.hash || ""));
  try {
    await loadState();
    const route = location.hash.slice(1);
    navigate(["workspace", "references", "engines"].includes(route) ? route : "workspace");
    if (app.state.jobs.length) await selectJob(app.state.jobs[0].id);
    $("#auth-gate").hidden = true;
    $("#app-shell").hidden = false;
    app.workspaceStarted = true;
  } catch (error) {
    showAuthGate({ message: "The workspace could not be loaded.", detail: error.message, retry: true, signOut: app.authMode === "cloud" });
  }
}

async function verifyCloudSession() {
  showAuthGate({ message: "Verifying workspace access…", busy: true });
  try {
    const session = await api("/api/session");
    if (!session?.email || !session?.uid) throw new Error("The session response was incomplete.");
    app.sessionUser = session;
    app.sessionVerified = true;
    cloudIdentity(session);
    await startWorkspace();
  } catch (_) {
    app.sessionVerified = false;
    showAuthGate({ message: "This account does not have workspace access.", detail: "Ask the workspace owner to confirm the allowlisted email, then try again.", signOut: true });
  }
}

async function initializeAccess() {
  showAuthGate({ message: "Checking workspace access…", busy: true });
  try {
    if (!window.InvoiceStudioAuth) throw new Error("The sign-in module did not load.");
    const runtime = await window.InvoiceStudioAuth.bootstrap();
    if (!runtime.cloud) {
      app.authMode = "local";
      app.authProviders = [];
      app.sessionVerified = true;
      $("#runtime-badge > span:last-child").textContent = "Local workspace";
      $("#cloud-user").hidden = true;
      await startWorkspace();
      return;
    }
    app.authMode = "cloud";
    app.authProviders = runtime.providers || [];
    if (runtime.user) {
      await verifyCloudSession();
      return;
    }
    const google = app.authProviders.includes("google");
    const password = app.authProviders.includes("password");
    showAuthGate({
      message: google || password ? "Sign in with an approved account to continue." : "No supported sign-in provider is enabled.",
      detail: google || password ? "" : "Ask the workspace administrator to enable an authentication provider.",
      google,
      password,
      retry: !google && !password,
    });
  } catch (_) {
    app.authMode = "unknown";
    app.sessionVerified = false;
    showAuthGate({ message: "Workspace access could not be verified.", detail: "Check the connection and retry. Local mode is used only when the server explicitly enables it.", retry: true });
  }
}

async function signInGoogle() {
  const button = $("#google-sign-in");
  button.disabled = true;
  button.textContent = "Opening Google sign-in…";
  try {
    await window.InvoiceStudioAuth.signInGoogle();
    await verifyCloudSession();
  } catch (error) {
    const cancelled = String(error?.code || error?.message || "").includes("popup-closed");
    showAuthGate({ message: "Sign in with an approved account to continue.", detail: cancelled ? "Sign-in was cancelled." : "Google sign-in could not be completed. Try again.", google: true, password: app.authProviders.includes("password") });
  } finally {
    button.disabled = false;
    button.textContent = "Continue with Google";
  }
}

async function signInPassword(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const email = $("#auth-email").value.trim();
  const password = $("#auth-password").value;
  const submit = $("button[type='submit']", form);
  submit.disabled = true;
  submit.textContent = "Signing in…";
  try {
    await window.InvoiceStudioAuth.signInPassword(email, password);
    $("#auth-password").value = "";
    await verifyCloudSession();
  } catch (_) {
    $("#auth-password").value = "";
    showAuthGate({ message: "Sign in with an approved account to continue.", detail: "The email or password was not accepted, or this account is not enabled.", google: app.authProviders.includes("google"), password: true });
  } finally {
    submit.disabled = false;
    submit.textContent = "Sign in";
  }
}

async function resetPassword() {
  const email = $("#auth-email").value.trim();
  if (!email) {
    showAuthGate({ message: "Enter the workspace email first.", detail: "Then choose “Send password reset email”.", google: app.authProviders.includes("google"), password: true });
    $("#auth-email").focus();
    return;
  }
  const button = $("#password-reset");
  button.disabled = true;
  try {
    await window.InvoiceStudioAuth.resetPassword(email);
    showAuthGate({ message: "Check your email for a password reset link.", detail: "For privacy, the same message is shown whether or not the address exists.", google: app.authProviders.includes("google"), password: true });
  } catch (error) {
    const network = String(error?.code || error?.message || "").includes("network-request-failed");
    showAuthGate({
      message: network ? "The reset service could not be reached." : "If the address is eligible, a reset email will arrive shortly.",
      detail: network ? "Check the connection and try again." : "For privacy, account availability is not shown here.",
      google: app.authProviders.includes("google"),
      password: true,
    });
  } finally { button.disabled = false; }
}

async function signOutCloud() {
  try { await window.InvoiceStudioAuth.signOut(); } catch (_) { /* The local UI still locks. */ }
  app.sessionVerified = false;
  app.sessionUser = null;
  clearInterval(app.pollTimer);
  clearSelectedJob();
  $("#cloud-user").hidden = true;
  showAuthGate({ message: "Signed out. Use an approved account to continue.", google: app.authProviders.includes("google"), password: app.authProviders.includes("password") });
}

async function init() {
  bindEvents();
  await initializeAccess();
}

document.addEventListener("DOMContentLoaded", init);
