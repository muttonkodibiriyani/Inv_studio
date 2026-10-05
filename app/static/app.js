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
  ["buyer_name", "Buyer name · Bill To", "text"],
  ["seller", "Seller entity", "text"],
  ["site", "Supplier site", "text"],
  ["buyer", "Buyer code · reference", "text"],
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

const DRAFT_HEADER_COLUMNS = ["Transaction Number", "Document", "Supplier Site", "Order No", "Location", "Location Type", "Document Date", "Total Cost Ex Tax", "Tax Amount", "Ref No. 1", "Ref No. 2", "Ref No. 3", "Comment"];
const DRAFT_TAX_COLUMNS = ["Transaction Number", "Tax Code", "Tax Basis"];
const DRAFT_DETAIL_COLUMNS = ["Transaction Number", "Item", "UPC", "Unit Cost", "Quantity", "Unit Tax Code"];

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
  reviewDirty: false,
  uploadFiles: [],
  pollTimer: null,
  pollBusy: false,
  pollCursor: 0,
  messageTimer: null,
  selectionToken: 0,
  preflight: null,
  authMode: "unknown",
  authProviders: [],
  sessionUser: null,
  sessionVerified: false,
  workspaceStarted: false,
  documentUrl: null,
  documentJobId: null,
  documentToken: 0,
  manualDraftJobId: null,
  referenceLookupLoaded: false,
  referenceLookupCursor: null,
  referenceLookupQuery: "",
  referenceLookupKind: "item",
  referenceLookupFilters: {},
  referenceLookupTarget: null,
  managedVertexModel: "",
  managedVertexModelsLoaded: false,
  providerModels: {},
  providerVerified: {},
  modelLoadTokens: {},
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
  const message = typeof detail === "string" ? detail : detail?.message
    || (Array.isArray(detail) ? detail.map((issue) => issue.msg || "Invalid value").join("; ") : "");
  const error = new Error(message || `Request failed (${response.status})`);
  error.detail = detail;
  return error;
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
  saveBlob(blob, dispositionFilename(disposition, fallbackName));
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function setDraftField(sectionName, column, value) {
  const section = $(`[data-draft-section="${sectionName}"]`, $("#manual-draft-form"));
  const input = [...section.elements].find((element) => element.name === column);
  if (input) input.value = value ?? "";
}

function addManualDraftLine(line = {}) {
  const row = make("tr");
  row.dataset.lookupDescription = line._description || "";
  const values = {
    "Transaction Number": line["Transaction Number"] ?? 1,
    Item: line.Item ?? "",
    UPC: line.UPC ?? "",
    "Unit Cost": line["Unit Cost"] ?? "",
    Quantity: line.Quantity ?? "",
    "Unit Tax Code": line["Unit Tax Code"] ?? "",
  };
  DRAFT_DETAIL_COLUMNS.forEach((column) => {
    const cell = make("td");
    const input = make("input");
    input.name = column;
    input.value = values[column];
    input.required = true;
    if (column === "Transaction Number") { input.type = "number"; input.min = "1"; input.max = "1"; input.step = "1"; }
    if (["Unit Cost", "Quantity"].includes(column)) {
      input.type = "number";
      input.step = "0.0001";
      input.min = column === "Unit Cost" ? "0" : "0.0001";
    }
    input.addEventListener("input", updateManualDraftTotal);
    cell.append(input);
    row.append(cell);
  });
  const find = make("button", "line-lookup", "Find item");
  find.type = "button";
  find.addEventListener("click", () => beginLineReferenceLookup("draft", row));
  row.children[1].append(find);
  const action = make("td");
  const remove = make("button", "remove-line", "×");
  remove.type = "button";
  remove.setAttribute("aria-label", "Remove draft detail row");
  remove.addEventListener("click", () => {
    if ($$("tr", $("#manual-draft-lines")).length === 1) return;
    row.remove();
    updateManualDraftTotal();
  });
  action.append(remove);
  row.append(action);
  $("#manual-draft-lines").append(row);
  updateManualDraftTotal();
}

function updateManualDraftTotal() {
  const total = $$("tr", $("#manual-draft-lines")).reduce((sum, row) => {
    const cost = Number($('[name="Unit Cost"]', row).value);
    const quantity = Number($('[name="Quantity"]', row).value);
    return sum + (Number.isFinite(cost) && Number.isFinite(quantity) ? cost * quantity : 0);
  }, 0);
  $("#manual-draft-total").textContent = `Detail total: ${total.toFixed(4)}`;
}

function initializeManualDraft(job) {
  const invoice = structuredClone(job.invoice || {});
  const matches = job.validation?.matches || [];
  setDraftField("Header", "Transaction Number", 1);
  setDraftField("Header", "Document", invoice.number);
  setDraftField("Header", "Supplier Site", invoice.site);
  setDraftField("Header", "Order No", invoice.po);
  setDraftField("Header", "Location", invoice.location);
  setDraftField("Header", "Location Type", job.validation?.location_type);
  setDraftField("Header", "Document Date", invoice.date);
  setDraftField("Header", "Total Cost Ex Tax", invoice.net);
  setDraftField("Header", "Tax Amount", invoice.tax);
  setDraftField("Header", "Ref No. 1", "");
  setDraftField("Header", "Ref No. 2", "");
  setDraftField("Header", "Ref No. 3", "");
  setDraftField("Header", "Comment", "DRAFT UNVALIDATED — manual entry");
  setDraftField("Tax_Breakdown", "Transaction Number", 1);
  setDraftField("Tax_Breakdown", "Tax Code", invoice.taxCode);
  setDraftField("Tax_Breakdown", "Tax Basis", invoice.net);
  $("#manual-draft-lines").replaceChildren();
  (invoice.lines?.length ? invoice.lines : [{}]).forEach((line, index) => addManualDraftLine({
    "Transaction Number": 1,
    Item: matches[index]?.item || line.item_id || "",
    UPC: matches[index]?.gtin || line.gtin || "",
    "Unit Cost": line.price ?? "",
    Quantity: line.qty ?? "",
    "Unit Tax Code": invoice.taxCode || "",
    _description: line.description || "",
  }));
  $("#manual-draft-acknowledge").checked = false;
  $("#manual-draft-error").hidden = true;
}

function openManualDraft() {
  const job = app.currentJob;
  if (!job) return;
  if (app.manualDraftJobId !== job.id) {
    app.manualDraftJobId = job.id;
    initializeManualDraft(job);
  }
  $("#manual-draft-source-name").textContent = job.filename;
  const source = $("#manual-draft-source-link");
  if (app.documentJobId === job.id && app.documentUrl) {
    source.href = app.documentUrl;
    source.removeAttribute("aria-disabled");
  } else {
    source.removeAttribute("href");
    source.setAttribute("aria-disabled", "true");
  }
  $("#manual-draft-dialog").showModal();
}

function draftSection(sectionName, columns) {
  const section = $(`[data-draft-section="${sectionName}"]`, $("#manual-draft-form"));
  return Object.fromEntries(columns.map((column) => {
    const input = [...section.elements].find((element) => element.name === column);
    return [column, column === "Transaction Number" ? Number(input.value) : input.value.trim()];
  }));
}

function draftDetails() {
  return $$("tr", $("#manual-draft-lines")).map((row) => Object.fromEntries(DRAFT_DETAIL_COLUMNS.map((column) => {
    const input = [...row.querySelectorAll("input")].find((element) => element.name === column);
    return [column, column === "Transaction Number" ? Number(input.value) : input.value.trim()];
  })));
}

async function downloadManualDraft(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const error = $("#manual-draft-error");
  error.hidden = true;
  if (!form.checkValidity()) {
    error.textContent = "Complete every required workbook field. The first missing or invalid value is highlighted.";
    error.hidden = false;
    form.reportValidity();
    return;
  }
  const body = {
    acknowledge_unvalidated: $("#manual-draft-acknowledge").checked,
    Header: draftSection("Header", DRAFT_HEADER_COLUMNS),
    Tax_Breakdown: draftSection("Tax_Breakdown", DRAFT_TAX_COLUMNS),
    Details: draftDetails(),
  };
  const net = Number(body.Header["Total Cost Ex Tax"]);
  const basis = Number(body.Tax_Breakdown["Tax Basis"]);
  const detailTotal = body.Details.reduce((sum, row) => sum + Number(row["Unit Cost"]) * Number(row.Quantity), 0);
  if (!Number.isFinite(net) || !Number.isFinite(basis) || !Number.isFinite(detailTotal) || net.toFixed(4) !== basis.toFixed(4) || net.toFixed(4) !== detailTotal.toFixed(4)) {
    error.textContent = "Tax Basis and the detail Unit Cost × Quantity total must both equal Total Cost Ex Tax.";
    error.hidden = false;
    return;
  }

  const button = $("#download-manual-draft");
  button.disabled = true;
  button.textContent = "Preparing draft…";
  try {
    const response = await studioFetch(`/api/jobs/${encodeURIComponent(app.manualDraftJobId)}/draft`, { method: "POST", body });
    if (!response.ok) throw await responseError(response);
    const disposition = response.headers.get("content-disposition") || "";
    saveBlob(await response.blob(), dispositionFilename(disposition, "Invoice_DRAFT_UNVALIDATED.xlsx"));
    $("#manual-draft-dialog").close();
    notify("Unvalidated draft downloaded. The invoice job and allocations were not changed.");
  } catch (requestError) {
    error.textContent = requestError.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "Download DRAFT_UNVALIDATED.xlsx";
  }
}

function openExtractionDraft() {
  if (!app.currentJob || ["queued", "processing"].includes(app.currentJob.status) || !hasStructuredInvoiceData(app.currentJob)) return;
  $("#extraction-draft-dialog").showModal();
}

async function downloadExtractionDraft() {
  if (!app.currentJob) return;
  const job = app.currentJob;
  const button = $("#confirm-extraction-draft");
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const response = await studioFetch(`/api/jobs/${encodeURIComponent(job.id)}/extraction-draft`, {
      method: "POST",
      body: { revision: job.revision, acknowledge_unvalidated: true },
    });
    if (!response.ok) throw await responseError(response);
    const disposition = response.headers.get("content-disposition") || "";
    saveBlob(await response.blob(), dispositionFilename(disposition, "Invoice_EXTRACTION_REVIEW_ONLY.xlsx"));
    $("#extraction-draft-dialog").close();
    notify("Extracted review-only workbook downloaded. Approval and receipt allocations were not changed.");
  } catch (error) {
    notify(error.message, "error", 7000);
  } finally {
    button.disabled = false;
    button.textContent = "Download EXTRACTION_REVIEW_ONLY.xlsx";
  }
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
    claude_local: "Claude subscription",
    vertex: "Google Cloud AI",
  };
  if (names[value]) return names[value];
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function auditEventLabel(event) {
  const labels = {
    extracted: "Reader run finished (legacy event)",
    text_read: "Text read",
    fields_extracted: "Invoice fields extracted",
    extraction_failed: "Extraction failed",
  };
  return labels[event] || humanize(event);
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

function isBatchReviewable(job) {
  return ["review", "ready", "exported"].includes(job.status) && hasStructuredInvoiceData(job);
}

function isSelectableInvoice(job) {
  return Boolean(job.id) && !["queued", "processing", "deleting"].includes(job.status);
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
  if (sectionName === "references") ensureReferenceLookup().catch((error) => notify(error.message, "error"));
  if (sectionName === "engines" && app.state) loadProviderModels("settings");
}

async function loadState({ preserveSelection = true } = {}) {
  const data = await api("/api/state");
  app.state = data;
  await ensureManagedVertexModel();
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
    select.disabled = !isSelectableInvoice(job);
    select.setAttribute("aria-label", `Select ${job.filename} for download or deletion`);
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
  const jobs = app.state?.jobs || [];
  const valid = [...app.selectedForBatch].filter((id) => isSelectableInvoice(jobs.find((job) => job.id === id) || {}));
  if (valid.length !== app.selectedForBatch.size) app.selectedForBatch = new Set(valid);
  const selectedJobs = valid.map((id) => jobs.find((job) => job.id === id)).filter(Boolean);
  const approvedSelected = selectedJobs.filter(isBatchReady).length;
  const reviewableSelected = selectedJobs.filter(isBatchReviewable).length;
  $("#selected-count").textContent = `${valid.length} selected`;
  $("#selected-detail").textContent = valid.length
    ? `${reviewableSelected} have extracted data; ${approvedSelected} approved. Delete removes only the selected invoices.`
    : "Select invoices to download together or delete.";
  $("#batch-review").disabled = valid.length === 0 || reviewableSelected !== valid.length;
  $("#batch-export").disabled = valid.length === 0 || approvedSelected !== valid.length || app.reviewDirty;
  $("#batch-delete").disabled = valid.length === 0;
  $("#select-all-finished").disabled = !jobs.some(isSelectableInvoice);
  const processed = jobs.filter(isBatchReviewable);
  const allProcessedSelected = processed.length > 0 && processed.every((job) => app.selectedForBatch.has(job.id));
  $("#select-processed").textContent = allProcessedSelected ? "Clear selection" : `Select processed${processed.length ? ` (${processed.length})` : ""}`;
  $("#select-processed").disabled = processed.length === 0;
  const ready = jobs.filter(isBatchReady);
  const allSelected = ready.length > 0 && ready.every((job) => app.selectedForBatch.has(job.id));
  $("#select-ready").textContent = allSelected && valid.length === ready.length ? "Clear selection" : `Select approved${ready.length ? ` (${ready.length})` : ""}`;
  $("#select-ready").disabled = ready.length === 0;
  renderBatchWorkflow(jobs);
}

function renderBatchWorkflow(jobs) {
  const active = jobs.filter((job) => ["queued", "processing"].includes(job.status)).length;
  const processed = jobs.filter(isBatchReviewable).length;
  $("#workflow-upload-count").textContent = `${jobs.length} invoice${jobs.length === 1 ? "" : "s"}`;
  $("#workflow-process-count").textContent = `${active} active`;
  $("#workflow-review-count").textContent = `${processed} processed`;
  $("#workflow-download-count").textContent = `${app.selectedForBatch.size} selected`;
}

async function selectJob(id) {
  if (id !== app.selectedJobId && app.reviewDirty) {
    notify("Save this invoice before opening another one. Your edits are still here.", "error", 7000);
    return;
  }
  const token = ++app.selectionToken;
  app.selectedJobId = id;
  renderJobs();
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (token !== app.selectionToken) return;
    app.currentJob = job;
    app.reviewDirty = false;
    renderSelectedJob(job);
    startPollingIfNeeded();
  } catch (error) {
    notify(error.message, "error");
  }
}

function clearSelectedJob() {
  app.selectionToken += 1;
  app.selectedJobId = null;
  app.currentJob = null;
  app.reviewDirty = false;
  $("#workspace-empty").hidden = false;
  $("#review-panel").hidden = true;
  if (app.documentUrl) URL.revokeObjectURL(app.documentUrl);
  app.documentUrl = null;
  app.documentJobId = null;
  app.documentToken += 1;
  $("#document-frame").removeAttribute("src");
  $("#document-image").removeAttribute("src");
}

function plannedProcessingStages(job) {
  const options = job.options || {};
  const current = job.progress?.engine;
  let stages;
  if (options.engine === "auto" || !options.engine) stages = ["invoice2data", "paddleocr", "docling"];
  else if (options.engine === "ai") stages = [];
  else stages = [options.engine];

  if (options.engine === "ai" || options.ai_fallback) stages.push(options.provider || "openai");
  if (current && !stages.includes(current)) {
    const aiIndex = stages.findIndex((stage) => ["openai", "anthropic", "chatgpt", "claude_local", "vertex"].includes(stage));
    stages.splice(aiIndex < 0 ? stages.length : aiIndex, 0, current);
  }
  return [...new Set(stages.filter(Boolean))];
}

function renderProcessingStages(job) {
  const list = $("#processing-stages");
  list.replaceChildren();
  const stages = plannedProcessingStages(job);
  const current = job.status === "processing" ? job.progress?.engine : null;
  const currentIndex = current ? stages.indexOf(current) : -1;
  stages.forEach((engine, index) => {
    const isAI = ["openai", "anthropic", "chatgpt", "claude_local", "vertex"].includes(engine);
    let state = "next";
    let stateLabel = index === 0 && job.status === "queued" ? "Starts first" : "Next if needed";
    if (index === currentIndex) {
      state = "active";
      stateLabel = "Working now";
    } else if (currentIndex > index) {
      state = "complete";
      stateLabel = "Checked";
    }
    if (isAI && !String(job.options?.model || "").trim()) stateLabel = "Needs a model to run";
    const item = make("li", `processing-stage ${state}`);
    const marker = make("span", "stage-marker", state === "complete" ? "✓" : String(index + 1));
    marker.setAttribute("aria-hidden", "true");
    const copy = make("span", "stage-copy");
    const model = isAI && job.options?.model ? ` · ${job.options.model}` : "";
    copy.append(make("strong", "", `${humanize(engine)}${model}`), make("small", "", stateLabel));
    item.append(marker, copy);
    list.append(item);
  });
}

function hasStructuredInvoiceData(job) {
  const invoice = job.invoice || {};
  return Boolean(invoice.lines?.length) || HEADER_FIELDS.some(([name]) => {
    const value = invoice[name];
    return value !== null && value !== undefined && String(value).trim() !== "";
  });
}

function renderSelectedJob(job) {
  $("#workspace-empty").hidden = true;
  $("#review-panel").hidden = false;
  $("#selected-filename").textContent = job.filename;
  $("#selected-file-icon").textContent = (job.filename.split(".").pop() || "DOC").slice(0, 4).toUpperCase();
  const engine = job.selected_engine && job.selected_engine !== "pending" ? humanize(job.selected_engine) : "Engine pending";
  $("#selected-meta").textContent = `${engine}${job.created_at ? ` · Added ${formatDate(job.created_at)}` : ""}`;
  const processing = ["queued", "processing"].includes(job.status);
  const emptyExtraction = !processing && !hasStructuredInvoiceData(job);
  const status = $("#selected-status");
  status.textContent = emptyExtraction ? "Needs review" : (STATUS_LABELS[job.status] || humanize(job.status));
  status.className = `status-pill ${emptyExtraction ? "warning" : statusClass(job)}`;
  $("#retry-job").disabled = ["queued", "processing", "exported"].includes(job.status);

  $("#processing-banner").hidden = !processing;
  $("#processing-result").hidden = !processing;
  $("#empty-extraction").hidden = !emptyExtraction;
  $("#invoice-form").hidden = processing;
  $(".review-controls").hidden = processing;
  $("#completeness").hidden = processing || emptyExtraction;
  $(".trace-card").hidden = processing;
  $("#empty-retry").disabled = $("#retry-job").disabled;
  const hasText = Boolean(String(job.text || "").trim());
  $("#empty-show-text").hidden = !hasText;
  $("#empty-extraction-title").textContent = hasText ? "Text read, invoice fields not extracted" : "Invoice fields were not extracted";
  $("#empty-extraction-detail").textContent = hasText
    ? "The document text is available, but Invoice Studio could not turn it into invoice fields. Nothing is approved and export remains on hold."
    : "No structured invoice fields are available yet. Retry with another extraction path, configure AI fallback or enter the workbook values manually.";
  if (processing) {
    const title = job.status === "queued" ? "Waiting for an extraction slot…" : `${humanize(job.progress?.engine || "engine")} is reading the invoice…`;
    const detail = job.progress?.message || "The result will appear here when it is ready.";
    $("#processing-title").textContent = title;
    $("#processing-detail").textContent = detail;
    $("#processing-result-title").textContent = title;
    $("#processing-result-detail").textContent = detail;
    renderProcessingStages(job);
  }
  const possiblePurchaseOrder = job.document_type_hint === "possible_purchase_order";
  const extractionNote = String(job.extraction_note || (possiblePurchaseOrder
    ? "The source appears to be a purchase order rather than an invoice. Check the document and enter a draft manually if you still need a workbook."
    : "")).trim();
  $("#extraction-note").hidden = processing || !extractionNote;
  if (extractionNote) {
    $("#extraction-note-title").textContent = possiblePurchaseOrder ? "This may be a purchase order" : "Extraction needs review";
    $("#extraction-note-detail").textContent = extractionNote;
  }
  $("#job-error").hidden = !job.error;
  $("#job-error").textContent = job.error || "";

  renderDocument(job).catch((error) => notify(error.message, "error"));
  if (processing) {
    renderValidation(job);
    return;
  }
  renderInvoiceForm(job);
  renderValidation(job);
  renderTrace(job);
  renderReviewActions(job);
}

async function renderDocument(job) {
  const text = String(job.text || "").trim();
  $("#extracted-text").textContent = text;
  $("#extracted-text-wrap").hidden = !text;
  if (app.documentJobId === job.id) return;

  const token = ++app.documentToken;
  const endpoint = `/api/jobs/${encodeURIComponent(job.id)}/document`;
  const ext = (job.filename.split(".").pop() || "").toLowerCase();
  const isImage = ["png", "jpg", "jpeg", "webp", "bmp"].includes(ext);
  const canPreview = ext === "pdf" || isImage;
  app.documentJobId = job.id;
  if (app.documentUrl) URL.revokeObjectURL(app.documentUrl);
  app.documentUrl = null;
  $("#open-document").removeAttribute("href");
  $("#download-original").removeAttribute("href");
  $("#document-frame").hidden = ext !== "pdf";
  $("#document-image").hidden = !isImage;
  $("#document-download-card").hidden = canPreview;
  $("#document-frame").removeAttribute("src");
  $("#document-image").removeAttribute("src");
  try {
    const { blob } = await apiBlob(endpoint);
    if (token !== app.documentToken || app.currentJob?.id !== job.id) return;
    app.documentUrl = URL.createObjectURL(blob);
    $("#open-document").href = app.documentUrl;
    $("#download-original").href = app.documentUrl;
    if (ext === "pdf") $("#document-frame").src = app.documentUrl;
    if (isImage) $("#document-image").src = app.documentUrl;
  } catch (error) {
    if (token === app.documentToken && app.documentJobId === job.id) app.documentJobId = null;
    throw error;
  }
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
  app.reviewDirty = false;
  renderReviewSaveState();
}

function renderReviewSaveState() {
  const state = $("#review-save-state");
  state.textContent = app.reviewDirty
    ? "Unsaved changes — save before batch download or opening another invoice."
    : "Saved";
  state.classList.toggle("unsaved", app.reviewDirty);
  updateBatchControls();
}

function markReviewDirty() {
  if (!app.currentJob || ["queued", "processing", "exported"].includes(app.currentJob.status)) return;
  app.reviewDirty = true;
  renderReviewSaveState();
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

function printedLineAmounts(line) {
  const details = make("details", "line-amounts");
  const summary = make("summary");
  const net = lineInput("net_amount", line.net_amount, "Printed line net", "number");
  const tax = lineInput("tax_amount", line.tax_amount, "Printed line tax", "number");
  const update = () => {
    const values = [net.value.trim() ? `Net ${net.value.trim()}` : "", tax.value.trim() ? `Tax ${tax.value.trim()}` : ""].filter(Boolean);
    summary.textContent = values.length ? values.join(" · ") : "Add printed line net / tax";
  };
  [["Printed line net", net], ["Printed line tax", tax]].forEach(([labelText, input]) => {
    const label = make("label");
    label.append(make("span", "", labelText), input);
    details.append(label);
    input.addEventListener("input", update);
  });
  details.prepend(summary);
  details.open = line.net_amount !== null && line.net_amount !== undefined
    || line.tax_amount !== null && line.tax_amount !== undefined;
  update();
  return details;
}

function addLine(line = {}, disabled = false) {
  const row = make("tr");
  const identity = make("td");
  identity.append(lineInput("item_id", line.item_id, "Internal item · confirmed reference"), lineInput("sku", line.sku, "Supplier SKU"), lineInput("gtin", line.gtin, "GTIN or barcode"));
  identity.querySelector('[name="item_id"]').placeholder = "Internal item · reference";
  identity.querySelector('[name="sku"]').placeholder = "Supplier SKU";
  identity.querySelector('[name="gtin"]').placeholder = "Barcode / GTIN";
  const description = make("td");
  description.append(lineInput("description", line.description, "Description"));
  const find = make("button", "line-lookup", "Find item by description");
  find.type = "button";
  find.disabled = disabled;
  find.addEventListener("click", () => beginLineReferenceLookup("review", row));
  description.append(find);
  const qty = make("td"); qty.append(lineInput("qty", line.qty, "Quantity", "number"));
  const uom = make("td"); uom.append(lineInput("uom", line.uom, "Unit of measure"));
  const price = make("td"); price.append(lineInput("price", line.price, "Unit price", "number"), printedLineAmounts(line));
  const evidence = make("td");
  evidence.append(lineInput("evidence", line.evidence, "Evidence text"), lineInput("page", line.page, "Evidence page", "number"));
  const action = make("td");
  const remove = make("button", "remove-line", "×");
  remove.type = "button";
  remove.setAttribute("aria-label", "Remove line item");
  remove.addEventListener("click", () => { row.remove(); $("#no-lines").hidden = Boolean($("#line-items").children.length); markReviewDirty(); });
  action.append(remove);
  row.append(identity, description, qty, uom, price, evidence, action);
  $$("input", row).forEach((input) => { input.disabled = disabled; });
  remove.disabled = disabled;
  $("#line-items").append(row);
  $("#no-lines").hidden = true;
}

function renderValidation(job) {
  const issues = job.validation?.issues || [];
  const summary = $("#validation-summary");
  summary.replaceChildren();
  if (["queued", "processing"].includes(job.status)) {
    summary.hidden = true;
    return;
  }
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
  const details = make("details", "validation-details");
  const heading = make("summary");
  heading.append(make("strong", "", `${issues.length} issue${issues.length === 1 ? "" : "s"} to resolve`), make("span", "", "Show all"));
  const list = make("ul");
  issues.forEach((issue) => {
    const line = issue.line ? `Line ${issue.line}: ` : "";
    const owner = issue.owner ? ` · ${issue.owner}` : "";
    list.append(make("li", "", `${line}${issue.message}${owner}`));
  });
  details.append(heading, list);
  summary.append(details);
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
      const methodNames = {
        template: "Supplier template",
        layout: "Layout conversion",
        docling_table: "Table and word geometry conversion",
        docling_table_higher_resolution: "Table conversion after a higher-resolution read",
        layout_higher_resolution: "Layout conversion after a higher-resolution read",
        text_only: "Text only",
      };
      const method = entry.method ? ` · ${methodNames[entry.method] || humanize(entry.method)}` : "";
      const seconds = Number(entry.seconds);
      const timing = Number.isFinite(seconds) ? ` · ${seconds.toFixed(2)} s` : "";
      const evidence = [];
      const textCharacters = Number(entry.text_characters);
      if (Number.isFinite(textCharacters)) evidence.push(`${textCharacters.toLocaleString()} text characters`);
      const extractedFields = Number(entry.extracted_fields);
      if (Number.isFinite(extractedFields)) evidence.push(`${extractedFields.toLocaleString()} field${extractedFields === 1 ? "" : "s"}`);
      const lineItems = Number(entry.line_items);
      if (Number.isFinite(lineItems)) evidence.push(`${lineItems.toLocaleString()} item${lineItems === 1 ? "" : "s"}`);
      else if (entry.status === "text_only" || entry.method === "text_only") evidence.push("0 items");
      const evidenceText = evidence.length ? ` · ${evidence.join(" · ")}` : "";
      const completeness = entry.completeness === undefined ? "" : ` · ${Math.round(Number(entry.completeness) * 100)}% field completeness`;
      const reason = entry.reason ? ` — ${entry.reason}` : "";
      const recovery = entry.recovery?.attempted
        ? entry.recovery.selected_pass === "higher_resolution" ? " · Recovery read retained after consistency checks" : " · Original read retained; recovery did not safely improve it"
        : "";
      traceList.append(make("li", "", `${engine}${model}: ${status}${method}${timing}${evidenceText}${completeness}${recovery}${reason}`));
    }
  });
  const chosen = trace.findLast?.((entry) => entry && typeof entry === "object" && entry.status === "extracted") || trace.find((entry) => entry && typeof entry === "object" && entry.status === "extracted");
  $("#trace-summary").textContent = job.selected_engine && job.selected_engine !== "none"
    ? `Chosen: ${humanize(job.selected_engine)}`
    : chosen ? `Fields returned by ${humanize(chosen.engine)}${chosen.model ? ` · ${chosen.model}` : ""}` : "How this result was produced";

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
  $("#export-extraction-draft").hidden = busy || !hasStructuredInvoiceData(job);
  $("#export-extraction-draft").disabled = busy;
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
    return { item_id: value("item_id"), sku: value("sku"), gtin: value("gtin"), description: value("description"), qty: value("qty"), uom: value("uom"), price: value("price"), net_amount: value("net_amount"), tax_amount: value("tax_amount"), evidence: value("evidence"), page: page === null ? null : Number(page) };
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

function selectedBatchReviewJobs() {
  return [...app.selectedForBatch]
    .map((id) => app.state.jobs.find((job) => job.id === id))
    .filter(isBatchReviewable);
}

function openBatchReview() {
  const jobs = selectedBatchReviewJobs();
  if (!jobs.length) return;
  if (app.reviewDirty) {
    notify("Save the current invoice before downloading a batch. Your unsaved edits are still in the form.", "error", 8000);
    $("#save-review").focus();
    return;
  }
  $("#batch-review-count").textContent = `${jobs.length} invoice${jobs.length === 1 ? "" : "s"}`;
  $("#batch-review-dialog").showModal();
}

function openDeleteInvoices() {
  const jobs = [...app.selectedForBatch].map((id) => app.state.jobs.find((job) => job.id === id)).filter(isSelectableInvoice);
  if (!jobs.length) return;
  app.pendingDelete = jobs.map((job) => ({ id: job.id, revision: job.revision }));
  $("#delete-invoice-count").textContent = `${jobs.length} invoice${jobs.length === 1 ? "" : "s"}`;
  $("#delete-invoice-list").replaceChildren(...jobs.map((job) => make("li", "", job.filename)));
  $("#delete-invoice-acknowledge").checked = false;
  $("#confirm-delete-invoices").disabled = true;
  $("#delete-invoice-error").hidden = true;
  $("#delete-invoices-dialog").showModal();
}

async function deleteInvoices() {
  if (!$("#delete-invoice-acknowledge").checked || !app.pendingDelete?.length) return;
  const button = $("#confirm-delete-invoices");
  button.disabled = true;
  button.textContent = "Deleting…";
  try {
    const result = await api("/api/jobs/delete", { method: "POST", body: { jobs: app.pendingDelete, confirm_permanent: true } });
    const ids = new Set(result.deleted_ids);
    app.state.jobs = app.state.jobs.filter((job) => !ids.has(job.id));
    ids.forEach((id) => app.selectedForBatch.delete(id));
    if (ids.has(app.selectedJobId)) clearSelectedJob();
    app.pendingDelete = null;
    $("#delete-invoices-dialog").close();
    renderJobs();
    notify(`${result.count} invoice${result.count === 1 ? "" : "s"} permanently removed from this workspace.`, "success", 7000);
  } catch (error) {
    $("#delete-invoice-error").textContent = error.message;
    $("#delete-invoice-error").hidden = false;
  } finally {
    button.textContent = "Delete permanently";
    button.disabled = !$("#delete-invoice-acknowledge").checked;
  }
}

async function downloadBatchReview() {
  const jobs = selectedBatchReviewJobs();
  if (!jobs.length || app.reviewDirty) {
    $("#batch-review-dialog").close();
    if (app.reviewDirty) notify("Save the current invoice before downloading a batch.", "error", 8000);
    return;
  }
  const button = $("#confirm-batch-review");
  button.disabled = true;
  button.textContent = "Preparing…";
  try {
    const response = await studioFetch("/api/exports/extraction-batch", {
      method: "POST",
      body: {
        jobs: jobs.map((job) => ({ id: job.id, revision: job.revision })),
        acknowledge_unvalidated: true,
      },
    });
    if (!response.ok) throw await responseError(response);
    const disposition = response.headers.get("content-disposition") || "";
    saveBlob(await response.blob(), dispositionFilename(disposition, "Extraction_Batch_REVIEW_ONLY.xlsx"));
    $("#batch-review-dialog").close();
    notify(`${jobs.length} saved invoice${jobs.length === 1 ? "" : "s"} downloaded in one unvalidated review workbook.`);
  } catch (error) {
    notify(error.message, "error", 8000);
  } finally {
    button.disabled = false;
    button.textContent = "Download combined review Excel";
  }
}

async function exportBatch() {
  const selected = selectedBatchReviewJobs();
  const jobs = selected.filter(isBatchReady);
  if (!jobs.length || jobs.length !== selected.length) {
    notify("Validated export requires every selected invoice to be approved and ready. Use the review workbook for extracted records.", "error", 8000);
    return;
  }
  if (app.reviewDirty) {
    notify("Save the current invoice before exporting a batch.", "error", 8000);
    return;
  }
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
    button.textContent = "Export approved";
    updateBatchControls();
  }
}

function activeJobIds() {
  return (app.state?.jobs || [])
    .filter((job) => ["queued", "processing"].includes(job.status))
    .map((job) => job.id);
}

async function pollActiveJobs() {
  if (app.pollBusy) return;
  const active = activeJobIds();
  if (!active.length) {
    clearInterval(app.pollTimer);
    app.pollTimer = null;
    return;
  }

  app.pollBusy = true;
  const selectionToken = app.selectionToken;
  const selectedId = app.selectedJobId;
  const selectedWasProcessing = app.currentJob?.id === selectedId && ["queued", "processing"].includes(app.currentJob.status);
  const ids = [];
  if (selectedWasProcessing && active.includes(selectedId)) ids.push(selectedId);
  const background = active.filter((id) => id !== selectedId);
  for (let count = 0; count < Math.min(2, background.length); count += 1) {
    ids.push(background[(app.pollCursor + count) % background.length]);
  }
  if (background.length) app.pollCursor = (app.pollCursor + Math.min(2, background.length)) % background.length;

  try {
    const responses = await Promise.allSettled(ids.map((id) => api(`/api/jobs/${encodeURIComponent(id)}`)));
    let selectedResult = null;
    let listChanged = false;
    responses.forEach((result) => {
      if (result.status !== "fulfilled") return;
      const job = result.value;
      const stateIndex = app.state.jobs.findIndex((entry) => entry.id === job.id);
      if (stateIndex >= 0) {
        const previous = app.state.jobs[stateIndex];
        listChanged ||= previous.status !== job.status || previous.invoice?.number !== job.invoice?.number ||
          previous.invoice?.supplier_name !== job.invoice?.supplier_name || previous.invoice?.seller !== job.invoice?.seller;
        app.state.jobs[stateIndex] = { ...previous, ...job };
      }
      if (job.id === selectedId) selectedResult = job;
    });
    if (listChanged) renderJobs();

    // A request started for one selection must never replace a document chosen
    // while that request was in flight, or overwrite edits to a completed job.
    if (selectedResult && app.selectionToken === selectionToken && app.selectedJobId === selectedId && selectedWasProcessing) {
      const wasActive = ["queued", "processing"].includes(app.currentJob?.status);
      app.currentJob = selectedResult;
      renderSelectedJob(selectedResult);
      if (wasActive && !["queued", "processing"].includes(selectedResult.status)) {
        notify(selectedResult.status === "error" ? "Invoice processing needs attention." : "Invoice extraction finished.", selectedResult.status === "error" ? "error" : "success");
      }
    }
  } finally {
    app.pollBusy = false;
    if (!activeJobIds().length) {
      clearInterval(app.pollTimer);
      app.pollTimer = null;
    }
  }
}

function startPollingIfNeeded() {
  if (!activeJobIds().length) return;
  if (app.pollTimer) return;
  app.pollTimer = setInterval(pollActiveJobs, 1400);
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

function offeredAISettings(settings = {}) {
  const selected = {
    provider: settings.provider || "openai",
    model: String(settings.model || "").trim(),
    ai_fallback: settings.ai_fallback !== false,
  };
  if (providerReady(selected.provider, selected.model)) return selected;
  if (app.state?.connections?.vertex && app.managedVertexModel) {
    return { ...selected, provider: "vertex", model: app.managedVertexModel };
  }
  return selected;
}

async function ensureManagedVertexModel() {
  const settings = app.state?.settings || {};
  if (!app.state?.connections?.vertex || providerReady(settings.provider, settings.model)) return;
  if (app.managedVertexModelsLoaded) return;
  app.managedVertexModelsLoaded = true;
  try {
    const result = await api("/api/connections/vertex/models");
    app.managedVertexModel = String(result.models?.[0]?.id || "").trim();
  } catch (_) {
    app.managedVertexModel = "";
  }
}

function applyManagedVertexModel(prefix) {
  const provider = $(`#${prefix}-provider`);
  if (provider.value !== "vertex" || !app.managedVertexModel) return;
  $(`#${prefix}-model`).value = app.managedVertexModel;
}

function setupModelPicker(prefix) {
  const input = $(`#${prefix}-model`);
  const select = make("select", "model-choices");
  select.id = `${prefix}-model-choices`;
  select.setAttribute("aria-label", `${prefix === "retry" ? "Reprocess" : humanize(prefix)} available models`);
  select.append(new Option("Models load when you choose a connected provider", ""));
  input.before(select);
  input.placeholder = "Model ID · optional manual entry";
  const status = make("small", "model-load-status");
  status.id = `${prefix}-model-status`;
  status.setAttribute("role", "status");
  input.after(status);
  select.addEventListener("change", () => {
    if (select.value) input.value = select.value;
    if (prefix === "upload") updateUploadReadiness();
  });
}

async function loadProviderModels(prefix, { providerChanged = false, force = false } = {}) {
  const provider = $(`#${prefix}-provider`).value;
  const input = $(`#${prefix}-model`);
  const select = $(`#${prefix}-model-choices`);
  const status = $(`#${prefix}-model-status`);
  const token = (app.modelLoadTokens[prefix] || 0) + 1;
  app.modelLoadTokens[prefix] = token;
  if (providerChanged) input.value = "";
  select.replaceChildren(new Option("Loading models…", ""));
  select.disabled = true;
  if (!app.state?.connections?.[provider]) {
    select.replaceChildren(new Option("Connect this provider first", ""));
    status.textContent = `${humanize(provider)} is not connected. Open Engines & AI to connect it.`;
    if (prefix === "upload") updateUploadReadiness();
    return;
  }
  status.textContent = `Loading models from ${humanize(provider)}…`;
  try {
    let cached = app.providerModels[provider];
    if (force || !cached || Date.now() - cached.at > 300000) {
      cached = { at: Date.now(), promise: api(`/api/connections/${encodeURIComponent(provider)}/models`) };
      app.providerModels[provider] = cached;
    }
    const result = await cached.promise;
    if (app.modelLoadTokens[prefix] !== token || $(`#${prefix}-provider`).value !== provider) return;
    const models = result.models || [];
    app.providerVerified[provider] = true;
    renderConnections();
    select.replaceChildren(new Option(models.length ? "Choose a model" : "No models returned", ""));
    models.forEach((model) => select.add(new Option(model.name && model.name !== model.id ? `${model.name} · ${model.id}` : model.id, model.id)));
    if (!input.value && models.length) {
      const saved = app.state.settings?.provider === provider ? app.state.settings.model : "";
      input.value = models.find((model) => model.id === saved)?.id || models[0].id;
    }
    select.value = models.some((model) => model.id === input.value) ? input.value : "";
    select.disabled = !models.length;
    status.textContent = models.length ? `${models.length} models available. Check the model before confirming processing.` : "The provider returned no model choices. Check account access or enter a supported model ID.";
  } catch (error) {
    delete app.providerModels[provider];
    app.providerVerified[provider] = false;
    renderConnections();
    if (app.modelLoadTokens[prefix] !== token) return;
    select.replaceChildren(new Option("Models could not be loaded", ""));
    status.textContent = error.message;
  } finally {
    if (prefix === "upload") updateUploadReadiness();
  }
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
  $("#start-upload").disabled = !app.uploadFiles.length || ($("#upload-engine").value === "ai" && !providerReady(provider, model));
}

function openUpload() {
  const settings = offeredAISettings(app.state?.settings);
  $("#upload-provider").value = settings.provider;
  $("#upload-model").value = settings.model;
  $("#upload-ai-fallback").checked = settings.ai_fallback !== false;
  $("#upload-engine").value = "auto";
  $("#upload-language").value = "en";
  $("#upload-progress").hidden = true;
  updateUploadReadiness();
  $("#upload-dialog").showModal();
  loadProviderModels("upload");
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
    notify(`${completed.length} invoice${completed.length === 1 ? "" : "s"} queued. Up to two process at once.`);
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

function referenceValue(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function referenceRecordTitle(record) {
  const candidateDescription = candidateValues(record, "description")[0];
  if (candidateDescription) return candidateDescription;
  const entries = Object.entries(record.data || {}).filter(([, value]) => referenceValue(value).trim());
  const preferred = [
    /product.*name|item.*name|description/i,
    /sku|item.*code|product.*code/i,
    /barcode|gtin|upc/i,
    /po.*number|purchase.*order|\bpo\b/i,
    /supplier|vendor/i,
  ];
  for (const pattern of preferred) {
    const match = entries.find(([name]) => pattern.test(name));
    if (match) return referenceValue(match[1]);
  }
  return entries.length ? referenceValue(entries[0][1]) : `${humanize(record.kind || "source")} record`;
}

function candidateValues(record, field, sourceColumn = "") {
  if (!sourceColumn) {
    const values = record.candidate_fields?.[field];
    if (!Array.isArray(values)) return [];
    return [...new Set(values.map((value) => referenceValue(value).trim()).filter(Boolean))];
  }
  const values = (record.candidate_field_sources?.[field] || [])
    .filter((source) => referenceValue(source?.column).trim() === sourceColumn)
    .map((source) => referenceValue(source?.value).trim());
  const direct = record.data?.[sourceColumn];
  const directValues = Array.isArray(direct) ? direct : [direct];
  directValues.forEach((value) => values.push(referenceValue(value).trim()));
  return [...new Set(values.filter(Boolean))];
}

function beginLineReferenceLookup(mode, row) {
  const isDraft = mode === "draft";
  const value = (name) => $(`[name="${name}"]`, row)?.value.trim() || "";
  const query = isDraft
    ? row.dataset.lookupDescription || value("Item") || value("UPC")
    : value("description") || value("sku") || value("gtin");
  if (query.length < 2) {
    notify("Enter a product description, item code or barcode before searching.", "error");
    return;
  }
  app.referenceLookupTarget = { mode, row, context: {
    price: value(isDraft ? "Unit Cost" : "price"), qty: value(isDraft ? "Quantity" : "qty"),
    uom: isDraft ? "" : value("uom"),
    currency: $('#header-fields [name="currency"]')?.value.trim() || app.currentJob?.invoice?.currency || "",
    invoice_po: $('#header-fields [name="po"]')?.value.trim() || app.currentJob?.invoice?.po || "",
  } };
  if (isDraft) $("#manual-draft-dialog").close();
  navigate("references");
  $("#reference-lookup-kind").value = "item";
  $("#reference-lookup-query").value = query.slice(0, 100);
  $("#reference-lookup-site").value = "";
  $("#reference-lookup-supplier").value = "";
  $("#reference-lookup-po").value = "";
  searchReferenceLookup();
}

function candidateSelect(record, field, labelText, sourceColumn = "") {
  const label = make("label");
  label.append(make("span", "", labelText));
  const select = make("select");
  select.dataset.candidateField = field;
  const values = candidateValues(record, field, sourceColumn);
  if (values.length !== 1) {
    const placeholder = make("option", "", values.length ? "Choose a value" : "Not available");
    placeholder.value = "";
    select.append(placeholder);
  }
  values.forEach((value) => {
    const columns = [...new Set((record.candidate_field_sources?.[field] || [])
      .filter((source) => referenceValue(source?.value).trim() === value
        && (!sourceColumn || referenceValue(source?.column).trim() === sourceColumn))
      .map((source) => referenceValue(source?.column).trim())
      .filter(Boolean))];
    if (sourceColumn && !columns.length) columns.push(sourceColumn);
    const option = make("option", "", columns.length ? `${columns.join(" / ")}: ${value}` : value);
    option.value = value;
    select.append(option);
  });
  if (values.length === 1) select.value = values[0];
  select.disabled = !values.length;
  label.append(select);
  return label;
}

function applyReferenceCandidate(action) {
  if (!$(".candidate-confirm", action)?.checked) return;
  const target = app.referenceLookupTarget;
  if (!target?.row?.isConnected) {
    notify("That invoice line is no longer available. Open it and search again.", "error");
    app.referenceLookupTarget = null;
    return;
  }
  const selected = Object.fromEntries($$("select[data-candidate-field]", action).map((select) => [select.dataset.candidateField, select.value]));
  if (!selected.sku && !selected.internal_item && !selected.gtin && !selected.uom) return;
  const names = target.mode === "draft" ? { internal_item: "Item", gtin: "UPC" } : { internal_item: "item_id", sku: "sku", gtin: "gtin", uom: "uom", description: "description" };
  Object.entries(names).forEach(([field, inputName]) => {
    if (selected[field]) $(`[name="${inputName}"]`, target.row).value = selected[field];
  });
  const mode = target.mode;
  const evidence = $('[name="evidence"]', target.row);
  if (mode === "review" && evidence && action.dataset.sourceReference) {
    evidence.value = `${evidence.value}\nHuman-confirmed reference: ${action.dataset.sourceReference}`.trim();
  }
  if (mode === "review") markReviewDirty();
  app.referenceLookupTarget = null;
  navigate("workspace");
  if (mode === "draft") $("#manual-draft-dialog").showModal();
  notify("Candidate values copied for review. They are not approved until you verify and save them.");
}

function referenceCandidateAction(record) {
  if (!app.referenceLookupTarget || record.kind !== "item" || !record.candidate_fields) return null;
  const action = make("div", "reference-candidate-action");
  action.dataset.sourceReference = `${record.source_hash} / ${record.source_sheet} / row ${record.source_row}`;
  const contextValues = [
    ["description", "Description"], ["pack", "Pack"], ["supplier", "Supplier"], ["site", "Site"], ["po", "PO"],
  ].flatMap(([field, label]) => {
    const values = candidateValues(record, field);
    return values.length ? [`${label}: ${values.join(" / ")}`] : [];
  });
  if (contextValues.length) action.append(make("p", "reference-candidate-context", contextValues.join(" · ")));
  action.append(candidateSelect(record, "internal_item", "Internal item", "ITEM_PARENT"));
  if (app.referenceLookupTarget.mode !== "draft") action.append(candidateSelect(record, "sku", "Supplier SKU"));
  action.append(candidateSelect(record, "gtin", "Barcode / GTIN"));
  if (app.referenceLookupTarget.mode === "review") {
    action.append(candidateSelect(record, "uom", "Unit of measure"));
    action.append(candidateSelect(record, "description", "Master product name", record.data?.ITEM_DESC ? "ITEM_DESC" : ""));
  }
  const confirmation = make("label", "candidate-confirm-label");
  const confirmed = make("input", "candidate-confirm"); confirmed.type = "checkbox";
  confirmation.append(confirmed, make("span", "", "I confirm this is the correct product and pack. Keep the invoice price and quantity unchanged."));
  action.append(confirmation);
  const buttons = make("div", "reference-candidate-buttons");
  const use = make("button", "button button-secondary", "Use selected item");
  use.type = "button";
  const findPO = make("button", "button button-quiet", "Find PO/GRN rows");
  findPO.type = "button";
  const update = () => {
    use.disabled = !confirmed.checked || !$("select[data-candidate-field]:not(:disabled)", action) || !$$('select[data-candidate-field]', action).some((select) => select.value);
    findPO.disabled = !$('[data-candidate-field="gtin"]', action)?.value;
  };
  action.addEventListener("change", update);
  use.addEventListener("click", () => applyReferenceCandidate(action));
  findPO.addEventListener("click", () => {
    const gtin = $('[data-candidate-field="gtin"]', action)?.value;
    if (!gtin) return;
    $("#reference-lookup-kind").value = "po";
    $("#reference-lookup-query").value = gtin;
    $("#reference-lookup-site").value = "";
    $("#reference-lookup-supplier").value = "";
    $("#reference-lookup-po").value = String(app.currentJob?.invoice?.po || "").trim();
    searchReferenceLookup();
  });
  buttons.append(use, findPO);
  action.append(buttons);
  update();
  return action;
}

function renderReferenceRecord(record) {
  const details = make("details", "reference-result");
  const summary = make("summary");
  const title = make("span", "reference-result-title");
  title.append(make("strong", "", referenceRecordTitle(record)));
  const match = record.match || {};
  const matchBasis = match.basis ?? record.match_basis;
  const basis = matchBasis ? `Matched by ${Array.isArray(matchBasis) ? matchBasis.map(referenceValue).join(", ") : referenceValue(matchBasis)}` : "Candidate source row";
  title.append(make("small", "", basis));
  const provenance = [record.source_sheet, record.source_row ? `row ${record.source_row}` : "", record.source_hash ? `source ${String(record.source_hash).slice(0, 10)}` : ""].filter(Boolean).join(" · ");
  const meta = make("span", "reference-result-meta", provenance);
  const score = match.score ?? record.score;
  if (score !== undefined && score !== null) meta.append(make("small", "", ` · score ${score}`));
  summary.append(title, meta);
  details.append(summary);

  const body = make("div", "reference-result-body");
  if (match.name_similarity !== undefined) body.append(make("p", "reference-candidate-context", `Product-name similarity: ${Math.round(match.name_similarity * 100)}%. This is a search clue, not an approval or accuracy measurement.`));
  for (const clue of match.clues || []) {
    const facts = [`PO ${clue.po || "—"}, source row ${clue.source_row}`, `Price ${clue.unit_price ?? "—"} ${clue.currency || ""}`, `Ordered ${clue.ordered_qty ?? "—"}; received ${clue.received_qty ?? "—"}`];
    if (clue.price_similarity !== undefined) facts.push(`Price similarity ${Math.round(clue.price_similarity * 100)}%`);
    if (clue.quantity_similarity !== undefined) facts.push(`Raw quantity similarity ${Math.round(clue.quantity_similarity * 100)}%`);
    body.append(make("p", "reference-candidate-context", facts.join(" · ")));
    if (clue.price_note || clue.quantity_note) body.append(make("p", "reference-candidate-context", [clue.price_note, clue.quantity_note].filter(Boolean).join(". ")));
  }
  const candidateAction = referenceCandidateAction(record);
  if (candidateAction) body.append(candidateAction);
  const flags = Array.isArray(record.flags) ? record.flags : [];
  if (flags.length) {
    const flagList = make("div", "reference-result-flags");
    flags.forEach((flag) => flagList.append(make("span", "", referenceValue(flag))));
    body.append(flagList);
  }
  const data = make("dl", "reference-result-data");
  Object.entries(record.data || {}).forEach(([name, value]) => {
    data.append(make("dt", "", name), make("dd", "", referenceValue(value) || "—"));
  });
  body.append(data);
  details.append(body);
  return details;
}

async function ensureReferenceLookup() {
  if (app.referenceLookupLoaded) return;
  app.referenceLookupLoaded = true;
  try {
    const summary = await api("/api/reference-lookup/summary");
    const container = $("#reference-source-summary");
    container.replaceChildren();
    const sources = Array.isArray(summary.sources) ? summary.sources : [];
    if (!sources.length) container.append(make("span", "", "No staged source evidence is available yet."));
    sources.forEach((source) => {
      if (source.counts && typeof source.counts === "object") {
        const counts = Object.entries(source.counts)
          .filter(([kind, count]) => kind !== "total" && Number.isFinite(Number(count)))
          .map(([kind, count]) => `${Number(count).toLocaleString()} ${humanize(kind).toLowerCase()} rows`)
          .join(" · ");
        container.append(make("span", "", `${source.id || "Staged source"}${counts ? ` · ${counts}` : ""}`));
      } else {
        const count = Number.isFinite(Number(source.count)) ? `${Number(source.count).toLocaleString()} records` : "record count unavailable";
        const hash = source.hash ? ` · ${String(source.hash).slice(0, 10)}` : "";
        container.append(make("span", "", `${humanize(source.kind || "source")} · ${count}${hash}`));
      }
    });
    if (summary.notice) {
      $("#reference-lookup-notice").textContent = summary.notice;
      $("#reference-lookup-notice").hidden = false;
    }
  } catch (error) {
    app.referenceLookupLoaded = false;
    $("#reference-source-summary").replaceChildren(make("span", "", "Staged source evidence could not be loaded."));
    throw error;
  }
}

async function searchReferenceLookup({ append = false } = {}) {
  const query = (append ? app.referenceLookupQuery : $("#reference-lookup-query").value.trim());
  const kind = append ? app.referenceLookupKind : $("#reference-lookup-kind").value;
  const filters = append ? app.referenceLookupFilters : {
    site: $("#reference-lookup-site").value.trim(),
    supplier: $("#reference-lookup-supplier").value.trim(),
    po: $("#reference-lookup-po").value.trim(),
  };
  if (query.length < 2) {
    $("#reference-lookup-query").focus();
    return;
  }
  const button = append ? $("#reference-lookup-more") : $("#reference-lookup-submit");
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = append ? "Loading…" : "Searching…";
  try {
    const params = new URLSearchParams({ kind, q: query, limit: "25" });
    Object.entries(filters).forEach(([name, value]) => { if (value) params.set(name, value); });
    if (append && app.referenceLookupCursor) params.set("cursor", app.referenceLookupCursor);
    if (kind === "item") Object.entries(app.referenceLookupTarget?.context || {}).forEach(([name, value]) => { if (value) params.set(name, value); });
    const response = await api(`/api/reference-lookup/${kind === "item" ? "products" : "search"}?${params}`);
    const results = $("#reference-lookup-results");
    if (!append) results.replaceChildren();
    const records = Array.isArray(response.records) ? response.records : [];
    records.forEach((record) => results.append(renderReferenceRecord(record)));
    if (!append && !records.length) results.append(make("p", "table-empty", "No staged source rows matched this search."));
    app.referenceLookupQuery = query;
    app.referenceLookupKind = kind;
    app.referenceLookupFilters = filters;
    app.referenceLookupCursor = response.next_cursor || null;
    $("#reference-lookup-more").hidden = !app.referenceLookupCursor;
    $("#reference-lookup-notice").textContent = response.notice || "Candidate source rows are evidence only and require confirmation.";
    $("#reference-lookup-notice").hidden = false;
  } catch (error) {
    $("#reference-lookup-notice").textContent = error.message;
    $("#reference-lookup-notice").hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
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
  const engineHelp = {
    invoice2data: "Reads native PDF/text and supplier templates. Scanned PDFs and images use PaddleOCR locally as its input reader; the trace shows both components.",
    paddleocr: "Uses PaddleOCR for scanned text, then layout rules convert recognized labels and rows into invoice fields.",
    docling: "Converts Docling layout and table cells into invoice fields, with RapidOCR / Paddle for scanned pages. Difficult scans may still need the direct PaddleOCR reader or permitted AI fallback.",
  };
  const localQualityTarget = "Quality target: at least 90% source-correct available fields, 95% expected and 100% ideal. This target is not a measured result; test each reader separately, including line recall.";
  (app.state?.engines || []).forEach((engine) => {
    const item = make("div", "engine-item");
    const head = make("div", "engine-item-head");
    head.append(make("strong", "", humanize(engine.id)));
    const dot = make("span", `availability${engine.installed ? " yes" : ""}`);
    dot.title = engine.installed ? "Installed" : "Not installed";
    head.append(dot);
    item.append(head, make("p", "", engine.note || (engine.installed ? "Available" : "Not installed")));
    if (engineHelp[engine.id]) item.append(make("p", "engine-help", `${engineHelp[engine.id]} Choose this reader from Engine when uploading or retrying.`));
    if (["paddleocr", "docling"].includes(engine.id)) item.append(make("p", "engine-quality-target", localQualityTarget));
    if (engine.version) item.append(make("small", "", `Version ${engine.version}`));
    list.append(item);
  });
}

function renderConnections() {
  const connections = app.state?.connections || {};
  Object.entries(connections).forEach(([provider, connected]) => {
    const badge = $(`[data-connection-state="${provider}"]`);
    if (!badge) return;
    const apiProvider = ["openai", "anthropic"].includes(provider);
    badge.textContent = !connected ? "Not connected" : !apiProvider ? "Connected"
      : app.providerVerified[provider] === true ? "Verified" : app.providerVerified[provider] === false ? "Check connection" : "Key saved";
    badge.classList.toggle("connected", connected && (!apiProvider || app.providerVerified[provider] === true));
  });
  const account = $("#chatgpt-account");
  account.replaceChildren(new Option("No connected accounts", ""));
  (app.state?.accounts || []).forEach((entry) => {
    const option = new Option(`${entry.email}${entry.connected ? "" : " (disconnected)"}`, entry.id);
    option.disabled = !entry.connected;
    option.selected = entry.id === app.state.active_account;
    account.add(option);
  });
  const hasAccount = Boolean(account.value);
  $("#remove-chatgpt").disabled = !hasAccount;
  $("#remove-chatgpt-cloud").disabled = !hasAccount;
  $("#export-chatgpt").disabled = !hasAccount;
  const cloud = app.authMode === "cloud";
  $$('[data-mode-only]').forEach((element) => { element.hidden = element.dataset.modeOnly !== (cloud ? "cloud" : "local"); });
  $("#connect-chatgpt").disabled = cloud;
  $("#chatgpt-account").disabled = false;
  ["settings-provider", "upload-provider", "retry-provider"].forEach((id) => {
    const select = $(`#${id}`);
    ["chatgpt", "claude_local"].forEach((value) => {
      const option = $(`option[value="${value}"]`, select);
      if (option) option.disabled = false;
    });
  });
}

function applySettings() {
  const settings = offeredAISettings(app.state?.settings);
  $("#settings-provider").value = settings.provider;
  $("#settings-model").value = settings.model;
  $("#settings-ai-fallback").checked = settings.ai_fallback !== false;
  if (!$("#upload-dialog").open) {
    $("#upload-provider").value = settings.provider;
    $("#upload-model").value = settings.model;
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
  const button = $("#fetch-models");
  button.disabled = true;
  button.textContent = "Loading…";
  try {
    await loadProviderModels("settings", { force: true });
  }
  finally { button.disabled = false; button.textContent = "Load available models"; }
}

async function saveApiKey(form) {
  const provider = form.dataset.provider;
  const input = form.elements.api_key;
  const apiKey = input.value;
  input.value = "";
  try {
    await api(`/api/connections/${provider}`, { method: "POST", body: { api_key: apiKey } });
    delete app.providerModels[provider];
    delete app.providerVerified[provider];
    await loadState();
    if ($("#settings-provider").value === provider) loadProviderModels("settings", { force: true });
    notify(`${provider === "openai" ? "OpenAI" : "Anthropic"} API connection saved on the server.`);
  } catch (error) { notify(error.message, "error", 7000); }
}

async function removeApiConnection(provider) {
  try {
    await api(`/api/connections/${provider}`, { method: "DELETE" });
    delete app.providerModels[provider];
    delete app.providerVerified[provider];
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

async function exportChatGPT() {
  const id = $("#chatgpt-account").value;
  if (!id) return;
  const button = $("#export-chatgpt");
  button.disabled = true;
  try {
    const response = await studioFetch("/api/chatgpt/export", { method: "POST", body: { id } });
    if (!response.ok) throw await responseError(response);
    const disposition = response.headers.get("content-disposition") || "";
    saveBlob(await response.blob(), dispositionFilename(disposition, "invoice-studio-chatgpt-credentials.json"));
    await loadState();
    notify("ChatGPT connection moved out of this installation. Import the sensitive file into your hosted workspace, then delete it.");
  } catch (error) { notify(error.message, "error", 7000); }
  finally { button.disabled = false; }
}

async function importChatGPT(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const file = form.elements.bundle_file.files[0];
  const raw = file ? await file.text() : form.elements.bundle_json.value.trim();
  let bundle;
  try { bundle = JSON.parse(raw); }
  catch (_) { notify("Choose or paste a valid Invoice Studio registration JSON file.", "error"); return; }
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    await api("/api/chatgpt/import", { method: "POST", body: { bundle } });
    form.reset();
    await loadState();
    notify("ChatGPT subscription registration imported and stored securely.");
  } catch (error) { notify(error.message, "error", 7000); }
  finally { button.disabled = false; }
}

async function importClaudeSubscription(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const setupToken = form.elements.setup_token.value;
  form.elements.setup_token.value = "";
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    await api("/api/subscriptions/claude/import", { method: "POST", body: { setup_token: setupToken } });
    await loadState();
    notify("Claude subscription token stored securely. The connection is ready when the Claude CLI is installed.");
  } catch (error) { notify(error.message, "error", 7000); }
  finally { button.disabled = false; }
}

async function removeClaudeSubscription() {
  try {
    await api("/api/subscriptions/claude", { method: "DELETE" });
    await loadState();
    notify("Claude subscription connection removed.");
  } catch (error) { notify(error.message, "error"); }
}

async function selectChatGPTAccount() {
  const id = $("#chatgpt-account").value;
  $("#remove-chatgpt").disabled = !id;
  $("#remove-chatgpt-cloud").disabled = !id;
  $("#export-chatgpt").disabled = !id;
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
    entries.forEach((entry) => list.append(make("li", "", `${formatDate(entry.at)} · ${auditEventLabel(entry.event)}`)));
  } catch (error) { list.replaceChildren(make("li", "", error.message)); }
}

async function submitRetry(event) {
  event.preventDefault();
  if (!app.currentJob) return;
  const options = processingOptions("retry");
  if (options.engine === "ai" && !providerReady(options.provider, options.model)) {
    $("#retry-model-status").textContent = "Connect this provider and select a model before running AI extraction.";
    return;
  }
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
  ["settings", "upload", "retry"].forEach(setupModelPicker);
  $$('[data-nav]').forEach((control) => control.addEventListener("click", (event) => { event.preventDefault(); navigate(control.dataset.nav); }));
  $$('[data-open-upload]').forEach((button) => button.addEventListener("click", openUpload));
  $("#job-search").addEventListener("input", renderJobs);
  $("#load-demo").addEventListener("click", prepareDemo);
  $("#add-line").addEventListener("click", () => { addLine(); markReviewDirty(); });
  $("#invoice-form").addEventListener("input", markReviewDirty);
  $("#confirm-review").addEventListener("change", markReviewDirty);
  $("#save-review").addEventListener("click", saveReview);
  $("#export-job").addEventListener("click", exportCurrentJob);
  $("#batch-review").addEventListener("click", openBatchReview);
  $("#confirm-batch-review").addEventListener("click", downloadBatchReview);
  $("#batch-export").addEventListener("click", exportBatch);
  $("#batch-delete").addEventListener("click", openDeleteInvoices);
  $("#confirm-delete-invoices").addEventListener("click", deleteInvoices);
  $("#delete-invoice-acknowledge").addEventListener("change", () => {
    $("#confirm-delete-invoices").disabled = !$("#delete-invoice-acknowledge").checked;
  });
  $("#select-all-finished").addEventListener("click", () => {
    const finished = app.state.jobs.filter(isSelectableInvoice);
    const all = finished.length && finished.every((job) => app.selectedForBatch.has(job.id));
    app.selectedForBatch = all ? new Set() : new Set(finished.map((job) => job.id));
    renderJobs();
  });
  $("#select-processed").addEventListener("click", () => {
    const processed = app.state.jobs.filter(isBatchReviewable);
    const all = processed.length && app.selectedForBatch.size === processed.length
      && processed.every((job) => app.selectedForBatch.has(job.id));
    app.selectedForBatch = all ? new Set() : new Set(processed.map((job) => job.id));
    renderJobs();
  });
  $("#select-ready").addEventListener("click", () => {
    const ready = app.state.jobs.filter(isBatchReady);
    const all = ready.length && app.selectedForBatch.size === ready.length
      && ready.every((job) => app.selectedForBatch.has(job.id));
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
  $("#upload-provider").addEventListener("change", () => loadProviderModels("upload", { providerChanged: true }));
  $("#upload-form").addEventListener("submit", beginUploadPreflight);

  $("#retry-job").addEventListener("click", () => {
    const rawOptions = app.currentJob?.options || app.state.settings;
    const options = { ...rawOptions, ...offeredAISettings(rawOptions) };
    $("#retry-engine").value = options.engine || "auto";
    $("#retry-language").value = options.language || "en";
    $("#retry-ai-fallback").checked = options.ai_fallback !== false;
    $("#retry-provider").value = options.provider || "openai";
    $("#retry-model").value = options.model || "";
    $("#retry-dialog").showModal();
    loadProviderModels("retry");
  });
  $("#retry-form").addEventListener("submit", submitRetry);
  $("#preflight-form").addEventListener("submit", confirmPreflight);
  const closePreflight = () => { $("#preflight-dialog").close("cancel"); cancelPreflight(); };
  $("#preflight-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closePreflight(); });
  $("#cancel-preflight").addEventListener("click", closePreflight);
  $("#cancel-preflight-x").addEventListener("click", closePreflight);
  $("#empty-configure-ai").addEventListener("click", () => navigate("engines"));
  $("#empty-retry").addEventListener("click", () => $("#retry-job").click());
  $("#empty-show-text").addEventListener("click", () => {
    const text = $("#extracted-text-wrap");
    text.open = true;
    text.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $$('[data-open-manual-draft]').forEach((button) => button.addEventListener("click", openManualDraft));
  $("#add-manual-draft-line").addEventListener("click", () => addManualDraftLine());
  $("#manual-draft-form").addEventListener("submit", downloadManualDraft);
  const closeManualDraft = () => $("#manual-draft-dialog").close();
  $("#cancel-manual-draft").addEventListener("click", closeManualDraft);
  $("#close-manual-draft-x").addEventListener("click", closeManualDraft);
  $("#manual-draft-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeManualDraft(); });
  $("#export-extraction-draft").addEventListener("click", openExtractionDraft);
  $("#confirm-extraction-draft").addEventListener("click", downloadExtractionDraft);

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
  $("#reference-lookup-form").addEventListener("submit", (event) => { event.preventDefault(); searchReferenceLookup(); });
  $("#reference-lookup-more").addEventListener("click", () => searchReferenceLookup({ append: true }));
  $("#template-file").addEventListener("change", (event) => { $("#template-file-name").textContent = event.target.files[0]?.name || "No file chosen"; $("#upload-template").disabled = !event.target.files[0]; });
  $("#upload-template").addEventListener("click", async () => {
    try { await importFile($("#template-file"), "/api/templates", "Supplier template registered."); $("#template-file-name").textContent = "No file chosen"; $("#upload-template").disabled = true; }
    catch (error) { notify(error.message, "error", 7000); }
  });
  $("#policy-form").addEventListener("submit", savePolicy);
  $("#provider-settings-form").addEventListener("submit", saveProviderSettings);
  $("#settings-provider").addEventListener("change", () => loadProviderModels("settings", { providerChanged: true }));
  $("#retry-provider").addEventListener("change", () => loadProviderModels("retry", { providerChanged: true }));
  $("#fetch-models").addEventListener("click", fetchModels);
  $("#model-list").addEventListener("change", (event) => { if (event.target.value) $("#settings-model").value = event.target.value; });
  $$(".api-key-form").forEach((form) => {
    form.addEventListener("submit", (event) => { event.preventDefault(); saveApiKey(form); });
    $(".remove-connection", form).addEventListener("click", () => removeApiConnection(form.dataset.provider));
  });
  $("#connect-chatgpt").addEventListener("click", connectChatGPT);
  $("#export-chatgpt").addEventListener("click", exportChatGPT);
  $("#chatgpt-import-form").addEventListener("submit", importChatGPT);
  $("#chatgpt-account").addEventListener("change", selectChatGPTAccount);
  $("#remove-chatgpt").addEventListener("click", removeChatGPT);
  $("#remove-chatgpt-cloud").addEventListener("click", removeChatGPT);
  $("#claude-subscription-form").addEventListener("submit", importClaudeSubscription);
  $("#remove-claude-subscription").addEventListener("click", removeClaudeSubscription);
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
  $("#auth-send-verification").addEventListener("click", async () => {
    try {
      await window.InvoiceStudioAuth.sendVerification();
      showAuthGate({ message: "Check your email for the verification link.", verification: true, signOut: true });
    } catch (_) { showAuthGate({ message: "Verification email could not be sent. Wait a moment and try again.", verification: true, signOut: true }); }
  });
  $("#auth-refresh-verification").addEventListener("click", async () => {
    try { await window.InvoiceStudioAuth.refreshIdentity(); await verifyCloudSession(); }
    catch (_) { showAuthGate({ message: "Email verification could not be refreshed. Try again.", verification: true, signOut: true }); }
  });
  $("#cloud-sign-out").addEventListener("click", signOutCloud);
}

function showAuthGate({ message, detail = "", busy = false, google = false, password = false, retry = false, signOut = false, verification = false }) {
  $("#app-shell").hidden = true;
  $("#auth-gate").hidden = false;
  $("#auth-message").textContent = message;
  $("#auth-spinner").hidden = !busy;
  $("#google-sign-in").hidden = !google;
  $("#password-sign-in").hidden = !password;
  $("#auth-retry").hidden = !retry;
  $("#auth-sign-out").hidden = !signOut;
  $("#auth-send-verification").hidden = !verification;
  $("#auth-refresh-verification").hidden = !verification;
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
  if (window.InvoiceStudioAuth.currentUser?.()?.emailVerified === false) {
    showAuthGate({ message: "Verify your email to open this workspace.", detail: "Use the verification email, then return here to continue.", verification: true, signOut: true });
    return;
  }
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
  app.pollTimer = null;
  clearSelectedJob();
  $("#cloud-user").hidden = true;
  showAuthGate({ message: "Signed out. Use an approved account to continue.", google: app.authProviders.includes("google"), password: app.authProviders.includes("password") });
}

async function init() {
  bindEvents();
  await initializeAccess();
}

document.addEventListener("DOMContentLoaded", init);
