const state = {
  backendReady: false,
  groqConfigured: false,
  model: "openai/gpt-oss-120b",
  repositories: [],
  activeRepository: null,
  repairs: [],
  history: [],
  passingPatchCount: 0,
  appliedPatchCount: 0,
  repairCount: 0,
  source: "local",
  requestController: null
};

const viewTitles = {
  overview: "Overview",
  repairs: "Repair queue",
  knowledge: "Knowledge base",
  validation: "Test runs",
  history: "History"
};
const layerSelectors = { bug: ".bug-detail", repo: ".repo-detail", project: ".project-detail" };
const dialogReturnFocus = new Map();

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}

function showView(view) {
  if (!viewTitles[view]) return;
  document.querySelectorAll(".view-panel").forEach(panel => {
    panel.classList.toggle("active", panel.id === `view-${view}`);
  });
  document.querySelectorAll(".nav-item").forEach(item => {
    item.classList.toggle("active", item.dataset.view === view);
    if (item.dataset.view === view) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  document.getElementById("breadcrumbCurrent").textContent = viewTitles[view];
  setMobileNavigation(false);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showToast(message, kind = "success") {
  const region = document.getElementById("toastRegion");
  const toast = document.createElement("div");
  toast.className = "toast";
  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.textContent = kind === "error" ? "!" : "✓";
  const text = document.createElement("span");
  text.textContent = message;
  toast.append(icon, text);
  region.append(toast);
  window.setTimeout(() => toast.remove(), 5_000);
}

async function api(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { ...options, headers: { ...options.headers } });
  } catch (error) {
    throw new Error("Cannot reach Patchwork. Start it with `python app.py` and open http://127.0.0.1:5000.");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status}).`);
  return body;
}

function activeRepository() {
  return state.repositories.find(repository => repository.id === state.activeRepository) || null;
}

function setBackendBadge() {
  const badge = document.getElementById("serverStatus");
  badge.textContent = state.backendReady
    ? (state.groqConfigured ? `${state.model} · local workspace` : "Local server ready · Groq key required")
    : "Local repair server offline";
  badge.closest(".sync-status").classList.toggle("backend-offline", !state.backendReady);
}

function renderRepositoryNavigation() {
  const navigation = document.getElementById("repositoryNav");
  navigation.replaceChildren();
  if (!state.repositories.length) {
    const empty = document.createElement("p");
    empty.className = "repo-empty-note";
    empty.textContent = "Connect a repository to begin.";
    navigation.append(empty);
    return;
  }
  state.repositories.forEach(repository => {
    const button = document.createElement("button");
    button.className = `repo-link ${repository.id === state.activeRepository ? "active" : ""}`;
    button.type = "button";
    button.dataset.repository = repository.id;
    const icon = document.createElement("span");
    icon.className = "repo-glyph";
    icon.textContent = "⌘";
    const name = document.createElement("span");
    name.textContent = repository.name;
    const status = document.createElement("span");
    status.className = "repo-status";
    button.append(icon, name, status);
    navigation.append(button);
  });
}

function changeRepository(repositoryId) {
  if (!state.repositories.some(repository => repository.id === repositoryId)) return;
  state.activeRepository = repositoryId;
  renderRepositoryNavigation();
  renderKnowledge();
  renderSourceOptions();
  renderOverview();
}

function updateRepositoryData(repository) {
  const index = repository.analysis;
  if (!index) throw new Error("The repository was connected, but its source-file index is missing. Re-index it.");
  state.repositories = [repository, ...state.repositories.filter(item => item.id !== repository.id)];
  state.activeRepository = repository.id;
  renderRepositoryNavigation();
  renderKnowledge();
  renderSourceOptions();
  renderOverview();
}

function renderOverview() {
  document.getElementById("currentDate").textContent = new Intl.DateTimeFormat(undefined, {
    weekday: "long", month: "long", day: "numeric", year: "numeric"
  }).format(new Date()).toUpperCase();
  const repository = activeRepository();
  const data = repository?.analysis;
  document.getElementById("activeRepositoryName").textContent = repository?.name || "No repository connected";
  document.getElementById("bannerSourceCount").textContent = data ? `${data.source_count} source files` : "Connect a repository";
  document.getElementById("bannerLanguage").textContent = data?.language || "Not indexed";
  document.getElementById("statFiles").textContent = data?.source_count ?? "0";
  document.getElementById("statDocuments").textContent = data?.doc_count ?? "0";
  document.getElementById("statPassed").textContent = state.passingPatchCount;
  document.getElementById("coverageBar").style.width = data
    ? `${Math.min(100, Math.round((data.source_count / Math.max(data.file_count, 1)) * 100))}%`
    : "0%";
  document.getElementById("overviewRepairCount").textContent = state.repairs.length;
  document.getElementById("navRepairCount").textContent = state.repairs.length;
  const recent = [...state.repairs].reverse().slice(0, 4);
  const repairList = document.getElementById("repairList");
  repairList.replaceChildren();
  if (!recent.length) {
    const empty = document.createElement("div");
    empty.className = "gentle-empty";
    empty.textContent = repository
      ? "No repairs yet. Choose an indexed source file and start with a real bug."
      : "Connect a repository and start a repair to see its real test results here.";
    repairList.append(empty);
  } else {
    recent.forEach(repair => repairList.append(renderRecentRepair(repair)));
  }
}

function renderRecentRepair(repair) {
  const passed = repair.status === "ready" || repair.status === "applied";
  const button = document.createElement("button");
  button.className = "repair-row";
  button.dataset.repair = repair.id;
  const icon = document.createElement("span");
  icon.className = `repair-severity ${passed ? "severity-low" : repair.status === "failed" ? "severity-high" : "severity-medium"}`;
  icon.textContent = passed ? "✓" : repair.status === "failed" ? "!" : "⌁";
  const main = document.createElement("span");
  main.className = "repair-main";
  const title = document.createElement("strong");
  title.textContent = repair.description;
  const location = document.createElement("small");
  location.textContent = `${repair.repositoryName} · ${repair.file}${repair.line ? `:${repair.line}` : ""}`;
  main.append(title, location);
  const meta = document.createElement("span");
  meta.className = "repair-meta";
  const status = document.createElement("span");
  status.className = `status-chip ${passed ? "status-ready" : repair.status === "failed" ? "status-review" : "status-running"}`;
  status.textContent = repair.status === "applied" ? "Patch applied"
    : repair.status === "ready" ? "Patch passed"
      : repair.status === "failed" ? "No passing patch" : "Generating";
  const timestamp = document.createElement("small");
  timestamp.textContent = repair.time;
  meta.append(status, timestamp);
  const arrow = document.createElement("span");
  arrow.className = "row-arrow";
  arrow.textContent = "↗";
  button.append(icon, main, meta, arrow);
  return button;
}

function renderKnowledge() {
  const repository = activeRepository();
  const analysis = repository?.analysis;
  document.getElementById("knowledgeBreadcrumb").textContent = repository?.name?.toUpperCase() || "YOUR REPOSITORY";
  document.getElementById("knowledgeRepoName").textContent = repository?.name || "No repository connected";
  document.getElementById("connectedBadge").hidden = !analysis;
  document.getElementById("knowledgeRepoDetails").textContent = analysis
    ? `${analysis.language} · ${analysis.file_count} indexed project files · ${analysis.skip_count} skipped safely`
    : "Connect a repository to inspect its real files, tests, and documentation.";
  document.getElementById("knowledgeSource").textContent =
    repository?.source === "github" ? "PUBLIC HTTPS GITHUB REPOSITORY"
      : repository?.source === "zip" ? "LOCAL REPOSITORY ZIP"
        : "LOCAL REPOSITORY";
  document.getElementById("indexedFileCount").textContent = analysis?.source_count ?? "0";
  document.getElementById("indexedTestCount").textContent = analysis?.test_count ?? "0";
  document.getElementById("indexedDocCount").textContent = analysis?.doc_count ?? "0";
  document.getElementById("projectDocCount").textContent = analysis
    ? `${analysis.doc_count} document${analysis.doc_count === 1 ? "" : "s"}`
    : "Awaiting index";
  document.getElementById("repoSourceCount").textContent = analysis
    ? `${analysis.source_count} source files`
    : "Awaiting index";
  document.getElementById("dependencyCount").textContent = analysis ? "See indexed manifest" : "Awaiting index";
  document.getElementById("bugLayerStatus").textContent = analysis ? "Indexed" : "Awaiting index";
  document.getElementById("contextState").textContent = analysis
    ? `${analysis.source_count} source files and ${analysis.doc_count} project documents indexed.`
    : "Connect a repository to analyze real project context.";
  document.getElementById("syncButton").disabled = !repository;
}

function renderSourceOptions() {
  const selector = document.getElementById("filePath");
  selector.replaceChildren();
  const repository = activeRepository();
  if (!repository?.analysis) {
    selector.disabled = true;
    selector.add(new Option("Connect a repository first", ""));
    return;
  }
  const files = repository.analysis.files || [];
  selector.add(new Option("Choose a file from the indexed repository…", ""));
  files.forEach(file => {
    const suffix = file.test ? " · test" : ` · ${file.language}`;
    selector.add(new Option(`${file.path}${suffix}`, file.path));
  });
  selector.disabled = !files.length;
  const fallback = files.find(file => !file.test) || files[0];
  if (fallback) selector.value = fallback.path;
  const command = repository.analysis.tests_preview?.[0]?.command;
  document.getElementById("testCommand").value =
    command && command !== "Add your project's test command" && command !== "Enter the project's test command"
      ? command
      : "";
  document.getElementById("repairSelectedRepository").children[1].textContent = repository.name;
  document.getElementById("repairRepositoryLabel").textContent = `NEW REPAIR · ${repository.name.toUpperCase()}`;
}

function renderQueue() {
  const root = document.getElementById("queueRows");
  root.replaceChildren();
  state.repairs.forEach(repair => root.append(renderQueueRow(repair)));
  updateRepairFilters();
  applyRepairFilters();
}

function renderQueueRow(repair) {
  const passed = repair.status === "ready" || repair.status === "applied";
  const button = document.createElement("button");
  button.className = "queue-row";
  button.dataset.repair = repair.id;
  button.dataset.status = repair.status;
  const title = document.createElement("span");
  title.className = "queue-title";
  const bullet = document.createElement("span");
  bullet.className = `queue-bullet ${passed ? "bullet-low" : repair.status === "failed" ? "bullet-high" : "bullet-medium"}`;
  const strong = document.createElement("strong");
  strong.textContent = repair.description;
  title.append(bullet, strong);
  const location = document.createElement("span");
  location.className = "mono";
  location.textContent = `${repair.repositoryName}/${repair.file}${repair.line ? `:${repair.line}` : ""}`;
  const severity = document.createElement("span");
  severity.innerHTML = `<span class="severity-text severity-color-${passed ? "low" : repair.status === "failed" ? "high" : "medium"}">${repair.status === "applied" ? "Applied" : repair.status === "ready" ? "Passed" : repair.status === "failed" ? "No fix" : "Running"}</span>`;
  const stateChip = document.createElement("span");
  const chip = document.createElement("span");
  chip.className = `status-chip ${passed ? "status-ready" : repair.status === "failed" ? "status-review" : "status-running"}`;
  chip.textContent = repair.status === "applied" ? "Applied"
    : repair.status === "ready" ? `${repair.candidates.length} patch${repair.candidates.length === 1 ? "" : "es"} passed`
    : repair.status === "failed" ? "No passing patch"
      : "Generating";
  stateChip.append(chip);
  const time = document.createElement("span");
  time.textContent = repair.time;
  const arrow = document.createElement("span");
  arrow.textContent = "↗";
  button.append(title, location, severity, stateChip, time, arrow);
  return button;
}

function updateRepairFilters() {
  const counts = {
    all: state.repairs.length,
    review: state.repairs.filter(repair => repair.status === "ready").length,
    running: state.repairs.filter(repair => repair.status === "running").length,
    ready: state.repairs.filter(repair => repair.status === "applied").length
  };
  document.querySelectorAll(".filter-tab").forEach(button => {
    button.querySelector("span").textContent = counts[button.dataset.filter] ?? 0;
  });
}

function applyRepairFilters() {
  const term = document.getElementById("repairSearch").value.trim().toLowerCase();
  let visible = 0;
  document.querySelectorAll(".queue-row").forEach(row => {
    const filter = document.querySelector(".filter-tab.active")?.dataset.filter || "all";
    const expected = filter === "review" ? "ready" : filter === "running" ? "running" : filter === "ready" ? "applied" : null;
    const show = (!expected || row.dataset.status === expected) && row.textContent.toLowerCase().includes(term);
    row.hidden = !show;
    if (show) visible++;
  });
  const empty = document.getElementById("queueEmpty");
  empty.hidden = visible > 0;
  empty.textContent = state.repairs.length
    ? "No repairs match this filter. Try another search or filter."
    : "Connect a repository and describe a bug to start the first real repair.";
}

function setRepoSource(source) {
  state.source = source;
  document.querySelectorAll(".repo-source-tab").forEach(tab => {
    tab.classList.toggle("active", tab.dataset.repoSource === source);
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(tab.dataset.repoSource === source));
    tab.tabIndex = tab.dataset.repoSource === source ? 0 : -1;
    tab.id = `repository-tab-${tab.dataset.repoSource}`;
    tab.setAttribute("aria-controls", `repository-panel-${tab.dataset.repoSource}`);
  });
  document.querySelectorAll(".repo-source-panel").forEach(panel => {
    const active = panel.dataset.repoPanel === source;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
    panel.setAttribute("role", "tabpanel");
    panel.id = `repository-panel-${panel.dataset.repoPanel}`;
    panel.setAttribute("aria-labelledby", `repository-tab-${panel.dataset.repoPanel}`);
  });
  document.getElementById("repositoryError").hidden = true;
}

function openDialog(id) {
  const backdrop = document.getElementById(id);
  if (backdrop.hidden) {
    const activeDialog = document.querySelector(".modal-backdrop:not([hidden])");
    const opener = activeDialog ? dialogReturnFocus.get(activeDialog.id) : document.activeElement;
    dialogReturnFocus.set(id, opener);
  }
  backdrop.hidden = false;
  document.body.classList.add("modal-open");
  const panel = backdrop.querySelector('[role="dialog"]');
  panel?.setAttribute("aria-modal", "true");
  if (panel && !panel.hasAttribute("tabindex")) panel.setAttribute("tabindex", "-1");
  const focusTarget = backdrop.querySelector(
    'input:not([type="checkbox"]):not([type="file"]):not([disabled]), textarea, select:not([disabled]), button:not([disabled])'
  ) || panel;
  focusTarget?.focus();
}

function closeDialog(id) {
  const backdrop = document.getElementById(id);
  if (backdrop.hidden) return;
  backdrop.hidden = true;
  const opener = dialogReturnFocus.get(id);
  dialogReturnFocus.delete(id);
  if (!document.querySelector(".modal-backdrop:not([hidden])")) document.body.classList.remove("modal-open");
  const activeDialog = document.querySelector(".modal-backdrop:not([hidden])");
  const focusTarget = activeDialog
    ? [...activeDialog.querySelectorAll('input:not([type="checkbox"]):not([type="file"]), textarea, select, button')]
      .find(element => !element.disabled && !element.closest("[hidden]"))
    : opener;
  if (focusTarget?.isConnected && !focusTarget.disabled && !focusTarget.closest("[hidden]")) focusTarget.focus();
}

function setMobileNavigation(open) {
  const sidebar = document.getElementById("sidebar");
  const menuButton = document.getElementById("mobileMenu");
  const mobile = window.matchMedia("(max-width: 850px)").matches;
  const shouldOpen = mobile && open;
  const focusWasInSidebar = sidebar.contains(document.activeElement);
  sidebar.classList.toggle("open", shouldOpen);
  sidebar.toggleAttribute("inert", mobile && !shouldOpen);
  sidebar.setAttribute("aria-hidden", String(mobile && !shouldOpen));
  document.body.classList.toggle("mobile-nav-open", shouldOpen);
  menuButton.setAttribute("aria-expanded", String(shouldOpen));
  menuButton.setAttribute("aria-label", shouldOpen ? "Close navigation" : "Open navigation");
  if (shouldOpen) sidebar.querySelector(".nav-item")?.focus();
  else if (focusWasInSidebar && mobile) menuButton.focus();
}

function addActivity(title, description, variant = "activity-lilac") {
  state.history.unshift({ title, description, variant, time: new Date().toLocaleTimeString() });
  state.history = state.history.slice(0, 60);
  renderHistory();
}

function renderHistory() {
  const list = document.getElementById("historyList");
  list.replaceChildren();
  document.getElementById("historyRepairCount").textContent = state.repairCount;
  document.getElementById("historyPassingCount").textContent = state.passingPatchCount;
  document.getElementById("historyAppliedCount").textContent = state.appliedPatchCount;
  const feed = document.getElementById("activityFeed");
  feed.replaceChildren();
  if (!state.history.length) {
    const empty = document.createElement("div");
    empty.className = "gentle-empty";
    empty.textContent = "Connect a repository to get started. Only real repair and test outcomes appear here.";
    list.append(empty);
    feed.append(empty.cloneNode(true));
    return;
  }
  const line = document.createElement("span");
  line.className = "activity-line";
  feed.append(line);
  state.history.slice(0, 3).forEach(item => {
    const entry = document.createElement("div");
    entry.className = "history-entry";
    const icon = document.createElement("span");
    icon.className = `history-icon ${item.variant.replace("activity-", "history-")}`;
    icon.textContent = item.variant === "activity-green" ? "✓" : item.variant === "activity-coral" ? "⌁" : "◈";
    const details = document.createElement("div");
    const heading = document.createElement("strong");
    heading.textContent = item.title;
    const message = document.createElement("p");
    message.textContent = item.description;
    const subline = document.createElement("small");
    subline.textContent = item.time;
    details.append(heading, message, subline);
    entry.append(icon, details);
    list.append(entry);

    const activityItem = document.createElement("div");
    activityItem.className = "activity-item";
    const activityIcon = document.createElement("span");
    activityIcon.className = `activity-dot ${item.variant}`;
    activityIcon.textContent = icon.textContent;
    const activityDetails = document.createElement("div");
    const activityHeading = document.createElement("p");
    const activityStrong = document.createElement("strong");
    activityStrong.textContent = item.title;
    activityHeading.append(activityStrong, document.createTextNode(` — ${item.description}`));
    const activityTime = document.createElement("small");
    activityTime.textContent = item.time;
    activityDetails.append(activityHeading, activityTime);
    activityItem.append(activityIcon, activityDetails);
    feed.append(activityItem);
  });
}

function newRepair(repository, description, filePath, testCommand) {
  const match = filePath.match(/^(.*?):(\d+)$/);
  const repair = {
    id: crypto.randomUUID(),
    repositoryId: repository.id,
    repositoryName: repository.name,
    description,
    file: match ? match[1] : filePath,
    line: match ? Number(match[2]) : 0,
    command: testCommand,
    status: "running",
    candidates: [],
    time: "Just now",
    events: []
  };
  state.repairs.push(repair);
  state.repairCount++;
  renderQueue();
  renderOverview();
  return repair;
}

function openRepairDetails(repair) {
  if (repair.events.length) {
    renderRepairDetails(repair);
    return;
  }
  document.getElementById("detailContent").innerHTML =
    `<div class="detail-header"><p class="eyebrow"><span class="eyebrow-mark"></span>REPAIR QUEUED</p><h2 id="detailTitle">${escapeHtml(repair.description)}</h2><p>${escapeHtml(repair.file)}${repair.line ? `:${repair.line}` : ""}</p></div><div class="repair-live-status"><span class="live-dot"></span>Waiting for repository analysis…</div>`;
  openDialog("detailModal");
}

function renderRepairDetails(repair) {
  const steps = repair.events.slice(-10).map(event =>
    `<div class="patch-event ${event.passed === false ? "patch-event-fail" : event.passed === true ? "patch-event-pass" : ""}"><span>${event.passed === false ? "×" : event.passed === true ? "✓" : "·"}</span>${escapeHtml(event.message)}</div>`
  ).join("");
  const candidates = repair.candidates.map((candidate, index) => `
    <article class="result-patch">
      <div class="patch-result-heading"><span class="status-chip status-ready">All tests passed · ${escapeHtml(candidate.test.duration)}s</span><strong>Patch ${index + 1} · ${escapeHtml(candidate.tier)} knowledge</strong></div>
      <p class="patch-result-summary">${escapeHtml(candidate.summary)}</p>
      <p class="patch-file-list">${candidate.files.map(file => escapeHtml(file.path)).join(" · ")}</p>
      <pre class="code-block"></pre>
      <div class="detail-actions"><button class="button button-secondary" data-action="download-patch" data-patch="${escapeHtml(candidate.id)}">↓ Download patch</button>${repair.status === "applied" ? '<span class="status-chip status-ready">Already applied</span>' : '<button class="button button-primary" data-action="apply-patch" data-patch="' + escapeHtml(candidate.id) + '">Review &amp; apply</button>'}</div>
    </article>
  `).join("");
  const footer = repair.status === "running"
    ? `<div class="repair-live-status"><span class="live-dot"></span>Checking repairs using ${escapeHtml(state.model)}…</div>`
    : `<div class="repair-final-status ${repair.status === "ready" || repair.status === "applied" ? "repair-success" : "repair-failure"}">${escapeHtml(repair.finalMessage || (repair.status === "applied" ? "This patch has already been applied to the repository." : repair.status === "ready" ? "Only patches passing your full test command are shown." : "No patch passed the selected test command. No untested patch was retained."))}</div>`;
  document.getElementById("detailContent").innerHTML = `
    <div class="detail-header"><p class="eyebrow"><span class="eyebrow-mark"></span>${escapeHtml(repair.repositoryName)} · ${repair.status === "running" ? "REPAIR IN PROGRESS" : repair.status === "applied" ? "PATCH APPLIED" : repair.status === "ready" ? "TEST-PASSING PATCHES" : "REPAIR COMPLETE"}</p><h2 id="detailTitle">${escapeHtml(repair.description)}</h2><p>${escapeHtml(repair.file)}${repair.line ? `:${repair.line}` : ""} · <span class="mono">${escapeHtml(repair.command)}</span></p></div>
    <section class="repair-event-list" aria-live="polite">${steps || '<span class="repair-live-status"><span class="live-dot"></span>Connecting to repair pipeline…</span>'}</section>
    ${footer}${candidates}
    ${repair.status !== "running" ? '<div class="detail-actions"><button class="button button-secondary" data-action="close-detail">Close</button></div>' : ""}`;
  document.querySelectorAll(".result-patch").forEach((patchElement, index) => {
    patchElement.querySelector(".code-block").textContent = repair.candidates[index].diff;
  });
  if (document.getElementById("detailModal").hidden) openDialog("detailModal");
}

function updateRepair(repair) {
  const keepOpen = !document.getElementById("detailModal").hidden
    && document.getElementById("detailTitle")?.textContent === repair.description;
  if (keepOpen) renderRepairDetails(repair);
  renderQueue();
  renderOverview();
  renderValidation();
}

function renderValidation(event = null, repair = null) {
  const root = document.getElementById("validationRuns");
  if (!event || !repair) return;
  const empty = root.querySelector(".empty-validation");
  if (empty) empty.remove();
  const row = document.createElement("div");
  row.className = "validation-run";
  const icon = document.createElement("span");
  icon.className = `run-check ${event.passed === true ? "pass" : event.passed === false ? "fail" : ""}`;
  icon.textContent = event.passed === true ? "✓" : event.passed === false ? "×" : "·";
  const description = document.createElement("span");
  const title = document.createElement("strong");
  title.textContent = repair.description;
  const detail = document.createElement("small");
  detail.textContent = event.message;
  description.append(title, detail);
  const status = document.createElement("span");
  status.className = `status-chip ${event.passed === true ? "status-ready" : event.passed === false ? "status-review" : "status-running"}`;
  status.textContent = event.passed === true ? "Tests passed" : event.passed === false ? "Rejected · not retained" : event.tier || "Checking";
  const time = document.createElement("span");
  time.textContent = new Date().toLocaleTimeString();
  row.append(icon, description, status, time);
  root.prepend(row);
}

async function processRepairStream(repair, payload) {
  state.requestController = new AbortController();
  const response = await fetch("/api/repair/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify(payload),
    signal: state.requestController.signal
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(result.error || `Repair request failed (${response.status}).`);
  }
  if (!response.body) {
    throw new Error("Streaming isn't supported by this browser. Try a current version of Chrome, Edge, or Firefox.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let savedFailure = null;
  let receivedResult = false;
  let completed = false;
  while (!completed) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() || "";
    if (done && buffer.trim()) {
      frames.push(buffer);
      buffer = "";
    }
    for (const frame of frames) {
      const data = frame.split(/\r?\n/).find(item => item.startsWith("data: "))?.slice(6);
      if (!data) continue;
      if (data === "[DONE]") {
        completed = true;
        break;
      }
      let update;
      try {
        update = JSON.parse(data);
      } catch {
        throw new Error("The repair server sent an invalid progress update.");
      }
      if (update.type === "error") throw new Error(update.message || "The repair server reported an error.");
      if (update.type === "candidate") {
        repair.events.push(update);
        renderValidation(update, repair);
        addActivity(update.passed ? "Candidate patch passed tests" : "Candidate patch rejected",
          update.message, update.passed ? "activity-green" : "activity-coral");
        repair.events = repair.events.slice(-20);
      } else if (update.type === "progress") {
        repair.events.push(update);
        repair.events = repair.events.slice(-20);
      } else if (update.type === "result") {
        receivedResult = true;
        repair.candidates = update.candidates || [];
        repair.status = repair.candidates.length ? "ready" : "failed";
        repair.finalMessage = update.message || "";
        repair.tier = update.tier;
        if (repair.candidates.length) {
          state.passingPatchCount += repair.candidates.length;
          addActivity("Patch passed selected test command",
            `${repair.candidates.length} patch(es) passed · ${update.tier} Knowledge`, "activity-green");
        }
        savedFailure = update.candidates?.length ? null : update.failures?.[0];
      }
      updateRepair(repair);
    }
    if (done) completed = true;
  }
  if (!receivedResult) throw new Error("The repair stream ended before sending final test results. Run the repair again.");
  if (savedFailure) console.info("All repair candidates failed validation:", savedFailure);
}

async function submitRepair(event) {
  event.preventDefault();
  const repository = activeRepository();
  if (!repository) {
    closeDialog("repairModal");
    showToast("Connect a repository before starting a repair.", "error");
    openDialog("repositoryModal");
    return;
  }
  if (!state.groqConfigured) {
    const error = document.getElementById("repairError");
    error.textContent = "Repository scanning is local and does not need Groq. AI repairs need GROQ_API_KEY configured in PowerShell before starting Patchwork; don't enter the key in this page.";
    error.hidden = false;
    return;
  }
  const description = document.getElementById("issueTitle").value.trim();
  const testCommand = document.getElementById("testCommand").value.trim();
  const filePath = document.getElementById("filePath").value;
  const failingTests = document.getElementById("errorDetails").value.trim();
  const error = document.getElementById("repairError");
  error.hidden = true;
  if (!filePath) {
    error.textContent = "Choose a real source file from this repository's index.";
    error.hidden = false;
    return;
  }
  if (!document.getElementById("shareConsent").checked) {
    error.textContent = "Review and confirm before sending this selected source and bug context to Groq.";
    error.hidden = false;
    return;
  }
  if (!document.getElementById("testConsent").checked) {
    error.textContent = "Confirm you trust this repository before running its tests. Test commands execute real project code on this computer.";
    error.hidden = false;
    return;
  }
  if (!description || !testCommand) {
    error.textContent = "Describe what failed and provide the exact test command to verify a fix.";
    error.hidden = false;
    return;
  }
  const repair = newRepair(repository, description, filePath, testCommand);
  closeDialog("repairModal");
  showView("repairs");
  document.getElementById("detailContent").innerHTML =
    `<div class="detail-header"><p class="eyebrow"><span class="eyebrow-mark"></span>${escapeHtml(repository.name)} · STARTING REPAIR</p><h2 id="detailTitle">${escapeHtml(description)}</h2><p>${escapeHtml(filePath)} · <span class="mono">${escapeHtml(testCommand)}</span></p></div><div class="repair-live-status"><span class="live-dot"></span>Checking the local Groq connection…</div>`;
  openDialog("detailModal");
  const submitButton = document.getElementById("startRepairButton");
  submitButton.disabled = true;
  try {
    await processRepairStream(repair, {
      repository_id: repository.id,
      file_path: filePath,
      description,
      failing_tests: failingTests,
      test_command: testCommand,
      share_context: true,
      test_consent: "I trust this repository and its test command. Test commands execute project code, "
        + "which can access files available to this computer."
    });
  } catch (failure) {
    repair.status = "failed";
    repair.finalMessage = failure instanceof Error ? failure.message : String(failure);
    repair.events.push({ passed: false, message: repair.finalMessage });
    showToast(repair.finalMessage, "error");
    renderRepairDetails(repair);
    renderValidation(repair.events.at(-1), repair);
    addActivity("Repair could not be completed", repair.finalMessage, "activity-coral");
    updateRepair(repair);
  } finally {
    state.requestController = null;
    submitButton.disabled = false;
    document.getElementById("repairForm").reset();
  }
}

async function submitRepository(event) {
  event.preventDefault();
  const error = document.getElementById("repositoryError");
  error.hidden = true;
  const button = document.getElementById("connectRepositoryButton");
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Connecting & indexing…";
  try {
    let response;
    if (state.source === "zip") {
      const archive = document.getElementById("repositoryZip").files[0];
      if (!archive) throw new Error("Choose a repository ZIP file.");
      if (archive.size > 50_000_000) throw new Error("ZIP uploads are limited to 50 MB.");
      const body = new FormData();
      body.append("archive", archive, archive.name);
      response = await api("/api/repos/upload", { method: "POST", body });
    } else {
      response = await api("/api/repos/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state.source === "local"
          ? { source: "local", path: document.getElementById("repositoryPath").value.trim() }
          : { source: "github", url: document.getElementById("repositoryUrl").value.trim() })
      });
    }
    updateRepositoryData(response.repository);
    closeDialog("repositoryModal");
    document.getElementById("repositoryForm").reset();
    setRepoSource(state.source);
    showToast(`${response.repository.name}: ${response.repository.analysis.source_count} source file(s) indexed.`);
    addActivity("Repository connected", `${response.repository.name} · ${response.repository.analysis.source_count} source files · ${response.repository.analysis.doc_count} project documents`);
  } catch (failure) {
    error.textContent = failure instanceof Error ? failure.message : String(failure);
    error.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

async function openPatch(repair, candidateId, action, button) {
  const candidate = repair.candidates.find(item => item.id === candidateId);
  if (!candidate) {
    showToast("This test-passing patch has expired. Run the repair again.", "error");
    return;
  }
  if (action === "download-patch") {
    const blob = new Blob([candidate.diff], { type: "text/x-diff" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `patchwork-${repair.file.split("/").pop() || "repair"}.patch`;
    link.click();
    URL.revokeObjectURL(url);
    return;
  }
  if (candidate.applyPending) return;
  const files = candidate.files.map(file => `  ${file.path}`).join("\n");
  const confirmed = window.confirm(
    `Apply this patch to your original repository?\n\n${candidate.summary}\n\n` +
    `Validated by: ${repair.command}\nFiles changed:\n${files}\n\n` +
    "Original files are rechecked before modification, and local backups are retained."
  );
  if (!confirmed) return;
  candidate.applyPending = true;
  if (button) {
    button.disabled = true;
    button.textContent = "Applying…";
  }
  try {
    const result = await api(`/api/repair/${encodeURIComponent(candidate.id)}/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true })
    });
    repair.status = "applied";
    repair.finalMessage = result.message;
    state.appliedPatchCount++;
    addActivity("Reviewed test-passing patch applied", `${result.message} · ${result.files.join(", ")}`, "activity-green");
    renderQueue();
    renderOverview();
    renderHistory();
    showToast(result.message);
    closeDialog("detailModal");
  } catch (failure) {
    showToast(failure instanceof Error ? failure.message : String(failure), "error");
  } finally {
    candidate.applyPending = false;
    if (button?.isConnected && repair.status !== "applied") {
      button.disabled = false;
      button.textContent = "Review & apply";
    }
  }
}

async function connectBackend() {
  document.getElementById("currentDate").textContent = new Intl.DateTimeFormat(undefined, {
    weekday: "long", month: "long", day: "numeric", year: "numeric"
  }).format(new Date()).toUpperCase();
  if (window.location.protocol === "file:") {
    document.getElementById("serverStatus").textContent = "Open http://127.0.0.1:5000 after starting Patchwork";
    document.querySelectorAll('[data-action="connect-repo"], [data-action="new-repair"]').forEach(button => {
      button.title = "Start the backend with python app.py and visit http://127.0.0.1:5000.";
    });
    return;
  }
  try {
    const result = await api("/api/status");
    state.backendReady = result.ready === true;
    state.groqConfigured = result.groq_configured === true;
    state.model = result.model || state.model;
    state.repositories = result.repositories || [];
    if (state.repositories[0]) {
      const indexed = await api(`/api/repos/${encodeURIComponent(state.repositories[0].id)}/reindex`, { method: "POST" });
      state.repositories[0] = indexed.repository;
      state.activeRepository = indexed.repository.id;
    }
    setBackendBadge();
    renderRepositoryNavigation();
    renderKnowledge();
    renderSourceOptions();
    renderOverview();
  } catch (error) {
    state.backendReady = false;
    setBackendBadge();
    showToast(error instanceof Error ? error.message : "The Patchwork repair server is offline.", "error");
  }
}

document.addEventListener("click", event => {
  const target = event.target.closest("button, a");
  if (!target) return;
  if (target.dataset.view) {
    event.preventDefault();
    showView(target.dataset.view);
  }
  if (target.dataset.action === "connect-repo") openDialog("repositoryModal");
  if (target.dataset.action === "open-repository") {
    closeDialog("repairModal");
    openDialog("repositoryModal");
  }
  if (target.dataset.action === "new-repair") {
    if (!state.backendReady) {
      showToast("Start the Patchwork backend with `python app.py` first.", "error");
      return;
    }
    if (!state.repositories.length) {
      showToast("Connect your repository before describing a repair.", "error");
      openDialog("repositoryModal");
      return;
    }
    renderSourceOptions();
    document.getElementById("shareConsent").checked = false;
    document.getElementById("testConsent").checked = false;
    document.getElementById("repairError").hidden = true;
    openDialog("repairModal");
  }
  if (target.dataset.action === "close-modal") closeDialog("repairModal");
  if (target.dataset.action === "close-repository") closeDialog("repositoryModal");
  if (target.dataset.action === "close-detail") closeDialog("detailModal");
  if (target.id === "mobileMenu") setMobileNavigation(!document.getElementById("sidebar").classList.contains("open"));
  if (target.dataset.repoSource) setRepoSource(target.dataset.repoSource);
  if (target.dataset.repository) {
    changeRepository(target.dataset.repository);
    showView("overview");
  }
  if (target.dataset.filter) {
    document.querySelectorAll(".filter-tab").forEach(tab => {
      const active = tab === target;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-pressed", String(active));
    });
    applyRepairFilters();
  }
  if (target.dataset.repair) {
    const repair = state.repairs.find(item => item.id === target.dataset.repair);
    if (repair) {
      renderRepairDetails(repair);
    }
  }
  if (target.dataset.action === "download-patch" || target.dataset.action === "apply-patch") {
    const repair = state.repairs.find(item => item.candidates.some(candidate => candidate.id === target.dataset.patch));
    if (repair) void openPatch(repair, target.dataset.patch, target.dataset.action, target);
  }
  if (target.dataset.layer) {
    showView("knowledge");
    document.querySelector(layerSelectors[target.dataset.layer])?.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  if (target.dataset.action === "view-signals") {
    if (!activeRepository()) {
      showToast("Connect a repository before analyzing its signals.", "error");
      openDialog("repositoryModal");
    } else {
      showToast("Knowledge counts show only files actually indexed for this repository.");
    }
  }
  if (target.id === "syncButton" && activeRepository()) {
    const repository = activeRepository();
    const button = target;
    button.disabled = true;
    button.textContent = "Re-indexing…";
    void api(`/api/repos/${encodeURIComponent(repository.id)}/reindex`, { method: "POST" })
      .then(result => {
        const index = state.repositories.findIndex(item => item.id === repository.id);
        state.repositories[index] = result.repository;
        renderKnowledge();
        renderSourceOptions();
        renderOverview();
        showToast(`Re-index complete: ${result.repository.analysis.source_count} source file(s).`);
      })
      .catch(error => showToast(error.message, "error"))
      .finally(() => { button.innerHTML = "<span>↻</span> Re-index repository"; button.disabled = !activeRepository(); });
  }
  if (target.id === "exportHistory") {
    const output = [
      "Patchwork local-session repair history",
      `Exported ${new Date().toISOString()}`,
      ...state.history.map(item => `${item.time} — ${item.title}: ${item.description}`)
    ].join("\n");
    const url = URL.createObjectURL(new Blob([output], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "patchwork-repair-history.txt";
    link.click();
    URL.revokeObjectURL(url);
  }
  if (target.id === "rerunAll") {
    showToast("Run a new repair to re-test fresh patch candidates.");
  }
});

document.getElementById("repositoryForm").addEventListener("submit", submitRepository);
document.getElementById("repairForm").addEventListener("submit", submitRepair);
document.getElementById("repositoryZip").addEventListener("change", event => {
  const file = event.target.files[0];
  document.getElementById("repositoryZipName").textContent = file
    ? `${file.name} · ${(file.size / (1024 * 1024)).toFixed(1)} MB`
    : "Choose a ZIP containing your project";
});
document.getElementById("repairSearch").addEventListener("input", applyRepairFilters);
document.getElementById("historyFilter")?.addEventListener("change", event => {
  document.querySelectorAll(".history-entry[data-type]").forEach(row => {
    row.hidden = event.target.value !== "all" && row.dataset.type !== event.target.value;
  });
});
document.addEventListener("keydown", event => {
  const activeDialog = document.querySelector(".modal-backdrop:not([hidden])");
  if (event.key === "Tab" && activeDialog) {
    const focusable = [...activeDialog.querySelectorAll(
      'button:not([disabled]), input:not([type="file"]):not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
    )].filter(element => !element.closest("[hidden]") && element.getClientRects().length > 0);
    if (focusable.length) {
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !activeDialog.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !activeDialog.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    }
  }
  if (event.key === "Escape") {
    closeDialog("repositoryModal");
    closeDialog("repairModal");
    if (state.repairs.every(repair => repair.status !== "running")) closeDialog("detailModal");
    setMobileNavigation(false);
  }
});
document.addEventListener("click", event => {
  if (document.getElementById("sidebar").classList.contains("open")
    && !event.target.closest("#sidebar, #mobileMenu")) setMobileNavigation(false);
});
document.querySelectorAll(".modal-backdrop").forEach(backdrop => backdrop.addEventListener("click", event => {
  if (event.target === backdrop && !state.repairs.some(repair => repair.status === "running" && backdrop.id === "detailModal")) {
    closeDialog(backdrop.id);
  }
}));

const repairFilters = document.querySelector(".filter-tabs");
repairFilters.setAttribute("role", "group");
document.querySelectorAll(".filter-tab").forEach(tab => {
  tab.setAttribute("aria-pressed", String(tab.classList.contains("active")));
});
document.querySelectorAll(".nav-item").forEach(item => {
  if (item.classList.contains("active")) item.setAttribute("aria-current", "page");
});
document.getElementById("mobileMenu").setAttribute("aria-controls", "sidebar");
document.getElementById("mobileMenu").setAttribute("aria-expanded", "false");
setRepoSource(state.source);
document.querySelectorAll(".repo-source-tab").forEach(tab => {
  tab.addEventListener("keydown", event => {
    const tabs = [...document.querySelectorAll(".repo-source-tab")];
    const currentIndex = tabs.indexOf(event.currentTarget);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
    else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    else return;
    event.preventDefault();
    tabs[nextIndex].focus();
    setRepoSource(tabs[nextIndex].dataset.repoSource);
  });
});
setMobileNavigation(false);
window.addEventListener("resize", () => {
  setMobileNavigation(false);
});
function connectListeners() {
  window.addEventListener("unhandledrejection", event => {
    console.error("Patchwork operation failed:", event.reason);
    showToast(event.reason?.message || "A Patchwork request failed. Check the local server log.", "error");
  });
}

connectListeners();
connectBackend();
