"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const node = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
};

const flowDetails = {
  1: ["Input control", "Each selected file becomes one job and is queued in order.", "Supported document, image and structured-text formats are checked for type and size. Combined invoices must be split before upload."],
  2: ["Decision control", "The reviewer sees the effective rules before any document is processed.", "The plan names the engine, optional provider and model, reference version, tolerances, files and warnings. Cancel sends no document and preserves the queue."],
  3: ["Extraction control", "The job names each stage; optional AI is a permitted fallback branch.", "A supplier template is tried first. A readable PDF with no template keeps native text and skips image OCR; scans can use PaddleOCR/Docling. Completeness is not confidence."],
  4: ["Business control", "Canonical references approve; lookup only supplies source evidence.", "Search covers 114,940 item and 297,199 PO rows with conflict context and explicit confirmation. A lookup choice does not make an invoice ready."],
  5: ["Human control", "A person compares editable values with the source and confirms the evidence review.", "The original document, extracted evidence, derivations and validation issues stay together. Revision checks stop stale updates from overwriting newer work."],
  6: ["Release control", "Approved export and manual draft remain visibly separate.", "Approved export revalidates and reserves receipts. The all-field manual path works during OCR but requires acknowledgement, says DRAFT_UNVALIDATED and reserves nothing."],
};

const stories = [
  {
    number: "INV-01", status: "working", persona: "AP operator", title: "Select and confirm an invoice batch",
    summary: "Know which files, data and reader will be used.", delivery: "Local and hosted processing gate verified",
    story: "As an AP operator, I want to choose several invoices and confirm the processing rules so I know which files, data and reader will be used.",
    acceptance: ["Cancel sends no invoice-processing request.", "A confirmed plan is bound to filenames/sizes, engine/model, reference version and tolerances.", "Changed rules require a fresh confirmation."],
  },
  {
    number: "INV-02", status: "working", persona: "AP operator", title: "Read digital invoices and scans",
    summary: "Avoid retyping supplier documents.", delivery: "Prior reader baseline verified; fast-path/stage release validation pending",
    story: "As an AP operator, I want to extract headers and every item line from supported files so I avoid retyping supplier documents.",
    acceptance: ["Digital invoice2data, PaddleOCR scan and Docling reader paths produce structured synthetic results.", "A readable PDF without a matching template keeps native text, skips image OCR and can use permitted AI fallback.", "The active stage and failure trace remain visible."],
  },
  {
    number: "INV-03", status: "untested", persona: "AP operator", title: "Use a chosen AI connection for exceptions",
    summary: "Let unfamiliar layouts be proposed for review.", delivery: "Adapters verified with mocked provider responses; live account evaluation pending",
    story: "As an AP operator, I want to select a provider/model and enable automatic fallback so unfamiliar layouts can be proposed for review.",
    acceptance: ["AI is called after explicit selection/confirmed fallback and local extraction failure or incompleteness.", "Provider output must pass the strict invoice schema; missing facts remain missing.", "Claude/ChatGPT controls do not imply live subscription eligibility or inference."],
  },
  {
    number: "INV-04", status: "planned", persona: "Data steward", title: "Map and approve business reference extracts",
    summary: "Make the validator use evidence with known meaning.", delivery: "114,940 item / 297,199 PO lookup indexed; approval remains separate",
    story: "As a data steward, I want to profile the whole item and PO/GRN files and approve explicit mappings so the validator uses evidence with known meaning.",
    acceptance: ["Preserve original bytes, hashes and row/column provenance; inspect all populated cells.", "Product-name and identifier lookup shows conflicts and requires explicit confirmation.", "Lookup evidence never becomes approved matching data; missing business meanings remain held."],
  },
  {
    number: "INV-05", status: "working", persona: "AP operator", title: "Match the exact supplier route and items",
    summary: "Prevent approval from leaking across markets or entities.", delivery: "Canonical matching implemented and tested",
    story: "As an AP operator, I want to check supplier, site, company, PO, item identity and unit so an invoice cannot borrow approval from another market or entity.",
    acceptance: ["Domestic and cross-border routes require their exact approval tuple.", "Conflicting SKU and barcode identity is held.", "Location type comes from an explicit reference field, not a numeric prefix."],
  },
  {
    number: "INV-06", status: "working", persona: "Finance owner", title: "Protect receipt quantity and commercial controls",
    summary: "Avoid incorrect or duplicate charges.", delivery: "Implemented and tested",
    story: "As a finance owner, I want to check price, accepted receipt balance and tax with decimal rules so we avoid incorrect or duplicate charges.",
    acceptance: ["Accepted receipts minus returns, external baseline and this app allocations determine available quantity.", "Repeated lines and multiple invoices consume quantity cumulatively.", "Tolerance and currency rounding are explicit; no credit or mixed-tax allocation is invented."],
  },
  {
    number: "INV-07", status: "working", persona: "AP reviewer", title: "Review evidence and resolve exceptions",
    summary: "Ensure every exported value has checked evidence.", delivery: "Implemented and browser verified",
    story: "As an AP reviewer, I want to compare the source with editable fields and record review so every exported value has checked evidence.",
    acceptance: ["Review confirmation cannot override validation holds.", "Concurrent edits with stale revisions are rejected.", "A manual draft created from an active job cannot alter its revision, invoice or review status."],
  },
  {
    number: "INV-08", status: "working", persona: "AP operator", title: "Export one joined three-sheet workbook",
    summary: "Let the receiving team import without manual Excel assembly.", delivery: "Implemented and browser/workbook verified",
    story: "As an AP operator, I want to download all selected ready invoices into the exact target so the receiving team can import without manual Excel assembly.",
    acceptance: ["Header/Tax_Breakdown/Details have exactly 13/3/6 columns and no helper formulas.", "Invoice transaction IDs are 1..N and consistent across all three sheets.", "Export rechecks and reserves quantities atomically; repeat download does not duplicate an allocation."],
  },
  {
    number: "INV-09", status: "working", persona: "Service owner", title: "Recover and audit local operations",
    summary: "Explain and retry a failed reader or restart.", delivery: "Instance-replacement persistence verified; database backup restoration remains a production gate",
    story: "As a service owner, I want to retain sources, rule versions, edits and exports so a failed reader or restart can be explained and retried.",
    acceptance: ["Restarted in-flight jobs visibly require retry; completed exports remain downloadable.", "Export records include reference version, policy and allocations.", "Recovery procedure preserves the database plus the corresponding encryption key."],
  },
  {
    number: "INV-10", status: "working", persona: "Workspace owner", title: "Run a restricted cloud workspace",
    summary: "Use a hosted portal without losing evidence on restart.", delivery: "Hosted synthetic workflow and instance-replacement persistence verified",
    story: "As a workspace owner, I want to sign in to a Firebase frontend backed by durable GCP services so the team can use a hosted portal without losing evidence on restart.",
    acceptance: ["Unauthenticated and non-allowlisted accounts cannot access invoices, references or connections.", "Database and document evidence survive a replacement application instance.", "Current source additions require a fresh deployment smoke; prior hosted results do not prove they are deployed."],
  },
  {
    number: "INV-11", status: "planned", persona: "Procurement owner", title: "Qualify suppliers for a new store or market",
    summary: "Avoid repeating every supplier task for store openings.", delivery: "Proposed next product increment",
    story: "As a procurement owner, I want to reuse approved coverage and start discovery only for gaps so store openings do not repeat every supplier task.",
    acceptance: ["Same-market coverage reuses valid route facts with provenance/expiry.", "New market or buying entity creates an explicit qualification case.", "AI suggestions cannot approve a legal entity, route or commercial agreement."],
  },
  {
    number: "INV-12", status: "planned", persona: "Item-data steward", title: "Normalize supplier items for buying and digital channels",
    summary: "Let purchasing and commerce reuse a consistent catalog.", delivery: "Proposed next product increment",
    story: "As an item-data steward, I want to turn varied supplier files into approved item identities and channel content so purchasing and commerce teams can reuse a consistent catalog.",
    acceptance: ["Supplier codes, internal items, barcodes, packaging and units have explicit mappings.", "Conflicting identities and unit conversions need steward approval.", "Channel-specific text, attributes, images and translations are versioned separately from purchasing identity."],
  },
  {
    number: "INV-13", status: "pending", persona: "AP operator", title: "Download an explicitly unvalidated manual draft",
    summary: "Keep urgent manual work moving during OCR.", delivery: "Implemented in source; integrated and hosted validation pending",
    story: "As an AP operator, I want to edit every target workbook field and download a draft while OCR is still running.",
    acceptance: ["Header/Tax_Breakdown/Details expose exactly 13/3/6 editable fields with one joined transaction.", "Explicit acknowledgement and workbook disclosures say DRAFT_UNVALIDATED and no receipt reservation.", "Draft creation does not change the job, approve references, write the approved ledger or reserve receipts."],
  },
  {
    number: "INV-14", status: "pending", persona: "Data steward", title: "Search large source extracts without implying approval",
    summary: "Find source evidence without promoting it into matching.", delivery: "114,940 item / 297,199 PO rows indexed; release validation pending",
    story: "As a data steward, I want to search actual item and PO sources by identifier or product name.",
    acceptance: ["Results retain source row and conflict context with bounded pagination and filters.", "The operator explicitly confirms a selection before it populates the manual workspace.", "Lookup is never represented as matching approval, receipt allocation or export readiness."],
  },
  {
    number: "INV-15", status: "pending", persona: "Workspace owner", title: "Move an owner subscription connection",
    summary: "Use an authorized account without exposing it through browser state.", delivery: "Controls implemented; live subscription inference and hosted deployment pending",
    story: "As a workspace owner, I want to connect restricted Claude or move my ChatGPT registration into my hosted workspace.",
    acceptance: ["Claude setup tokens are encrypted, removable and supplied only to the restricted CLI.", "ChatGPT export deletes local encrypted credentials, disconnects and clears the active account.", "Only authenticated cloud owners import; tests do not imply live eligibility or quality."],
  },
];

let storyFilter = "all";

function setView(name, updateHash = true) {
  const valid = ["overview", "stories", "architecture", "roadmap", "decisions"];
  const selected = valid.includes(name) ? name : "overview";
  $$(".view").forEach((section) => {
    const active = section.dataset.section === selected;
    section.hidden = !active;
    section.classList.toggle("active", active);
  });
  $$(".section-nav [data-view]").forEach((button) => {
    const active = button.dataset.view === selected;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (updateHash) history.replaceState(null, "", `#${selected}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
  $("#content").focus({ preventScroll: true });
}

function setFlowStep(number) {
  $$(".flow-step").forEach((button) => button.classList.toggle("active", button.dataset.step === number));
  const [label, title, description] = flowDetails[number];
  const detail = $("#flow-detail");
  detail.replaceChildren(node("span", "", label), node("strong", "", title), node("p", "", description));
}

function storyStatus(status) {
  if (status === "working") return ["working", "Working MVP"];
  if (status === "pending") return ["pending", "Pending validation"];
  if (status === "untested") return ["untested", "Live AI untested"];
  return ["planned", "Planned"];
}

function renderStories() {
  const query = $("#story-search").value.trim().toLowerCase();
  const visible = stories.filter((story) => {
    const matchesFilter = storyFilter === "all" || story.status === storyFilter;
    const haystack = [story.number, story.persona, story.title, story.summary, story.story, ...story.acceptance].join(" ").toLowerCase();
    return matchesFilter && haystack.includes(query);
  });
  const list = $("#story-list");
  list.replaceChildren();
  visible.forEach((story) => {
    const card = node("details", "story-card");
    const summary = node("summary");
    const number = node("span", "story-number", story.number);
    const title = node("span", "story-title");
    title.append(node("strong", "", story.title), node("small", "", `${story.persona} · ${story.summary}`));
    const chevron = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    chevron.classList.add("chevron");
    chevron.setAttribute("viewBox", "0 0 20 20");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "m6 8 4 4 4-4");
    chevron.append(path);
    summary.append(number, title, chevron);
    const body = node("div", "story-body");
    body.append(node("p", "", story.story));
    const acceptance = node("ol", "acceptance");
    story.acceptance.forEach((criterion) => acceptance.append(node("li", "", criterion)));
    body.append(acceptance);
    const meta = node("div", "story-meta");
    const [statusClass, statusLabel] = storyStatus(story.status);
    meta.append(node("span", `status ${statusClass}`, statusLabel), node("span", "", `${story.delivery} · ${story.acceptance.length} acceptance checks`));
    body.append(meta);
    card.append(summary, body);
    list.append(card);
  });
  $("#story-empty").hidden = visible.length > 0;
}

function setArchitecturePath(path) {
  $$('[data-path]').forEach((button) => button.classList.toggle("active", button.dataset.path === path));
  const local = $(".local-node");
  const ai = $(".ai-node");
  const caption = $("#architecture-caption");
  if (path === "ai") {
    ai.classList.add("highlight");
    ai.classList.remove("dim");
    local.classList.remove("highlight");
    caption.replaceChildren(node("strong", "", "AI fallback path selected: "), document.createTextNode("after a confirmed plan permits sharing, the chosen provider can fill incomplete extraction; its result still passes reference validation and evidence review."));
  } else {
    local.classList.add("highlight");
    ai.classList.add("dim");
    ai.classList.remove("highlight");
    caption.replaceChildren(node("strong", "", "Local path selected: "), document.createTextNode("a matching template is tried first; otherwise readable PDF text skips image OCR, while scans continue through local readers. Every stage remains visible."));
  }
}

function bind() {
  $$('[data-view]').forEach((control) => control.addEventListener("click", () => setView(control.dataset.view)));
  $$('[data-view-link]').forEach((control) => control.addEventListener("click", (event) => { event.preventDefault(); setView(control.dataset.viewLink); }));
  $$(".flow-step").forEach((button) => button.addEventListener("click", () => setFlowStep(button.dataset.step)));
  $$('[data-story-filter]').forEach((button) => button.addEventListener("click", () => {
    storyFilter = button.dataset.storyFilter;
    $$('[data-story-filter]').forEach((candidate) => candidate.classList.toggle("active", candidate === button));
    renderStories();
  }));
  $("#story-search").addEventListener("input", renderStories);
  $$('[data-path]').forEach((button) => button.addEventListener("click", () => setArchitecturePath(button.dataset.path)));
  $("#print-button").addEventListener("click", () => window.print());
  window.addEventListener("hashchange", () => setView(location.hash.slice(1), false));
}

bind();
renderStories();
setArchitecturePath("local");
setView(location.hash.slice(1) || "overview", false);
