import { Command } from "@tauri-apps/plugin-shell";
import { confirm } from "@tauri-apps/plugin-dialog";

const elements = {};
const state = {
  sourceFolder: null,
  includeSubfolders: false,
  verifyHash: false,
  dryRun: false,
  verbose: false,
  busy: false,
  reviewItems: [],
  sidecarStatus: "down",
  sidecarCommand: null,
  sidecarChild: null,
  nextRequestId: 0,
  pendingRequests: new Map(),
  stdoutBuffer: "",
  undoEntryCount: 0,
  undoBusy: false,
  sessionInitialized: false,
  recoveredUndoAvailable: false,
  recoveredUndoCount: 0,
};
const SIDECAR_NAME = "binaries/file-organizer";
const REQUEST_TIMEOUT_MS = 10 * 60 * 1000;

function cacheElements() {
  elements.folderPicker = document.getElementById("folder-picker");
  elements.includeSubfolders = document.getElementById("include-subfolders");
  elements.verifyHash = document.getElementById("opt-verify-hash");
  elements.dryRun = document.getElementById("opt-dry-run");
  elements.verbose = document.getElementById("opt-verbose");
  elements.startButton = document.getElementById("start-button");
  elements.undoButton = document.getElementById("undo-button");
  elements.recoveredUndoControl = document.getElementById("recovered-undo-control");
  elements.recoveredUndoCopy = document.getElementById("recovered-undo-copy");
  elements.reviewRecoveredUndo = document.getElementById("review-recovered-undo");
  elements.appMessage = document.getElementById("app-message");
  elements.appMessageText = document.getElementById("app-message-text");
  elements.restartSidecar = document.getElementById("restart-sidecar");
  elements.progressPanel = document.getElementById("progress-panel");
  elements.progressPhase = document.getElementById("progress-phase");
  elements.progressLabel = document.getElementById("progress-label");
  elements.progressSubfolderCounter = document.getElementById("progress-subfolder-counter");
  elements.progressFileCounter = document.getElementById("progress-file-counter");
  elements.progressTrack = document.getElementById("progress-track");
  elements.progressFill = document.getElementById("progress-fill");
  elements.verboseLog = document.getElementById("verbose-log");
  elements.resultsPanel = document.getElementById("results-panel");
  elements.resultsBody = document.getElementById("results-body");
  elements.resultTemplate = document.getElementById("folder-result-template");
  elements.reviewQueue = document.getElementById("review-queue");
  elements.reviewTemplate = document.getElementById("review-card-template");
  elements.themeButtons = [...document.querySelectorAll("[data-theme-choice]")];
}

function setTheme(preference) {
  localStorage.setItem("theme", preference);
  for (const button of elements.themeButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.themeChoice === preference));
  }
  if (preference === "system") {
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  } else {
    document.documentElement.dataset.theme = preference;
  }
}

function initTheme() {
  const saved = localStorage.getItem("theme");
  const preference = ["system", "light", "dark"].includes(saved) ? saved : "system";
  setTheme(preference);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (localStorage.getItem("theme") === "system") setTheme("system");
  });
}

function showMessage(message, kind = "info") {
  elements.appMessageText.textContent = message || "";
  elements.restartSidecar.hidden = state.sidecarStatus !== "down" || !message;
  elements.appMessage.className = `inline-message${message && kind === "error" ? " is-error" : ""}`;
  elements.appMessage.hidden = !message;
}

function makeFolderIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "folder-picker-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M3 6.5h7l2 2h9v10H3z");
  svg.append(path);
  return svg;
}

function renderPicker() {
  const picker = elements.folderPicker;
  picker.replaceChildren();

  if (!state.sourceFolder) {
    picker.className = "folder-picker is-empty";
    const button = document.createElement("button");
    button.id = "choose-folder";
    button.type = "button";
    button.className = "folder-picker-empty";
    button.setAttribute("aria-haspopup", "dialog");
    const icon = makeFolderIcon();
    const plus = document.createElementNS("http://www.w3.org/2000/svg", "path");
    plus.setAttribute("d", "M12 11v5m-2.5-2.5h5");
    icon.append(plus);
    const copy = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = "Click to choose a folder";
    const helper = document.createElement("small");
    helper.textContent = "Select one folder to organize.";
    copy.append(title, helper);
    button.append(icon, copy);
    button.addEventListener("click", chooseFolder);
    picker.append(button);
    syncStartButton();
    return;
  }

  picker.className = "folder-picker is-filled";
  const filled = document.createElement("div");
  filled.className = "folder-picker-filled";
  filled.append(makeFolderIcon());

  const details = document.createElement("div");
  details.className = "folder-details";
  const segments = state.sourceFolder.split(/[\\/]/).filter(Boolean);
  const name = document.createElement("strong");
  name.className = "folder-name";
  name.textContent = segments.at(-1) || state.sourceFolder;
  const path = document.createElement("span");
  path.className = "folder-path";
  path.textContent = state.sourceFolder;
  path.title = state.sourceFolder;
  details.append(name, path);

  const clear = document.createElement("button");
  clear.className = "folder-clear";
  clear.type = "button";
  clear.textContent = "×";
  clear.setAttribute("aria-label", `Clear selected folder ${state.sourceFolder}`);
  clear.addEventListener("click", clearFolder);
  filled.append(details, clear);
  picker.append(filled);
  syncStartButton();
}

function syncStartButton() {
  elements.startButton.disabled = state.busy || !state.sourceFolder || state.sidecarStatus !== "running";
  for (const control of [elements.includeSubfolders, elements.verifyHash, elements.dryRun, elements.verbose]) {
    control.disabled = state.busy;
  }
  const chooseButton = document.getElementById("choose-folder");
  const clearButton = elements.folderPicker.querySelector(".folder-clear");
  if (chooseButton) chooseButton.disabled = state.busy;
  if (clearButton) clearButton.disabled = state.busy;
  syncUndoControls();
}

function syncUndoControls() {
  elements.undoButton.disabled = state.busy || state.undoBusy || state.undoEntryCount <= 0;
  elements.recoveredUndoControl.hidden = !state.recoveredUndoAvailable;
  elements.recoveredUndoCopy.textContent = state.recoveredUndoAvailable
    ? `${state.recoveredUndoCount} undo changes from a previous session are available.`
    : "";
  elements.reviewRecoveredUndo.disabled = state.busy || state.undoBusy || state.sidecarStatus !== "running";
}

function undoResultMessage(result) {
  const undone = Array.isArray(result.undone) ? result.undone : [];
  const errors = Array.isArray(result.errors) ? result.errors : [];
  const restoredFiles = undone.filter((entry) => entry.op === "move" && entry.kind === "file").length;
  const restoredFolders = undone.filter((entry) => entry.op === "move" && entry.kind === "dir").length
    + undone.filter((entry) => entry.op === "mkdir").length;
  const recopied = undone.filter((entry) => entry.op === "dedupe_delete").length;
  const summary = `Restored ${restoredFiles} files, ${restoredFolders} folders, ${recopied} re-copied. Errors: ${errors.length}.`;
  return errors.length > 0 && errors[0]?.message
    ? `${summary} ${errors[0].message}`
    : summary;
}

async function performUndo({ recovered = false } = {}) {
  if (state.undoBusy || state.busy || state.sidecarStatus !== "running") return;
  state.undoBusy = true;
  syncUndoControls();
  if (!recovered) showMessage("Undoing the last run…");

  try {
    const result = await sendSidecarRequest("undo");
    state.undoEntryCount = Number(result.remainingCount) || 0;
    if (recovered) {
      state.recoveredUndoCount = state.undoEntryCount;
      state.recoveredUndoAvailable = state.undoEntryCount > 0;
    } else if (state.undoEntryCount === 0) {
      state.recoveredUndoCount = 0;
      state.recoveredUndoAvailable = false;
    }
    const errors = Array.isArray(result.errors) ? result.errors : [];
    showMessage(undoResultMessage(result), errors.length > 0 ? "error" : "info");
  } catch (error) {
    if (recovered) {
      state.recoveredUndoAvailable = true;
      state.recoveredUndoCount = Math.max(state.recoveredUndoCount, 1);
    }
    showMessage(`Undo could not complete: ${error.message || error}`, "error");
  } finally {
    state.undoBusy = false;
    syncUndoControls();
  }
}

async function reviewRecoveredUndo() {
  if (!state.recoveredUndoAvailable || state.sidecarStatus !== "running") return;
  try {
    const accepted = await confirm(
      `Undo ${state.recoveredUndoCount} changes from a previous session?`,
      { title: "File Organizer", kind: "warning" },
    );
    if (accepted) await performUndo({ recovered: true });
  } catch (error) {
    showMessage(`Could not open the undo confirmation: ${error.message || error}`, "error");
  }
}

async function initializeUndoSession() {
  const result = await sendSidecarRequest("start_session");
  state.sessionInitialized = true;
  if (!result.priorSession || Number(result.entryCount) <= 0) return;

  state.recoveredUndoCount = Number(result.entryCount) || 0;
  try {
    const accepted = await confirm(
      `Undo ${state.recoveredUndoCount} changes from a previous session?`,
      { title: "File Organizer", kind: "warning" },
    );
    if (accepted) {
      await performUndo({ recovered: true });
      return;
    }
  } catch (error) {
    showMessage(`Could not open the undo confirmation: ${error.message || error}`, "error");
  }

  state.recoveredUndoAvailable = true;
  syncUndoControls();
}

async function chooseFolder() {
  if (state.sidecarStatus !== "down") showMessage("");
  try {
    const invoke = window.__TAURI__?.core?.invoke;
    if (!invoke) throw new Error("The native folder dialog is unavailable.");
    const selectedPath = await invoke("plugin:dialog|open", {
      options: { directory: true, multiple: false },
    });
    if (typeof selectedPath !== "string" || selectedPath.length === 0) return;
    state.sourceFolder = selectedPath;
    renderPicker();
  } catch (error) {
    showMessage(`Could not open the folder picker: ${error.message || error}`, "error");
  }
}

function clearFolder() {
  if (state.busy) return;
  state.sourceFolder = null;
  if (state.sidecarStatus !== "down") showMessage("");
  renderPicker();
}

function armRequestTimeout(requestId, pending) {
  clearTimeout(pending.timeout);
  pending.timeout = setTimeout(() => {
    if (state.pendingRequests.has(requestId)) {
      markSidecarUnavailable("The sidecar request timed out. Restart it to continue.", pending.child);
    }
  }, REQUEST_TIMEOUT_MS);
}

function rejectPendingRequests(error) {
  for (const [requestId, pending] of state.pendingRequests) {
    clearTimeout(pending.timeout);
    state.pendingRequests.delete(requestId);
    pending.reject(error);
  }
}

function markSidecarUnavailable(message, child = state.sidecarChild) {
  if (child && state.sidecarChild !== child) return;
  state.sidecarCommand = null;
  state.sidecarChild = null;
  state.sidecarStatus = "down";
  state.stdoutBuffer = "";
  rejectPendingRequests(new Error(message));
  syncStartButton();
  showMessage(message, "error");
  if (child) void child.kill().catch(() => {});
}

function handleSidecarExit(child, detail) {
  if (state.sidecarChild !== child) return;
  const reason = typeof detail === "string"
    ? detail
    : `exit code ${detail?.code ?? "unknown"}`;
  markSidecarUnavailable(`The sidecar stopped (${reason}). Restart it to continue.`, child);
}

function handleSidecarProgress(message) {
  const pending = state.pendingRequests.get(String(message.id));
  if (pending) armRequestTimeout(String(message.id), pending);

  const actionName = { scan: "Scanning", organize: "Organizing", confirm: "Saving review" }[message.action]
    || "Working";
  const isComplete = message.phase === "completed";
  updateProgress({
    ...message,
    phase: message.label || (isComplete ? "Complete" : actionName),
    message: message.message || (isComplete ? "Request complete." : `${actionName} files…`),
    indeterminate: isComplete ? false : message.indeterminate,
    fileIndex: isComplete ? 1 : message.fileIndex,
    fileTotal: isComplete ? 1 : message.fileTotal,
  });
  if (state.verbose && message.message) appendVerboseLog(message.message);
}

function handleSidecarLine(line) {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    appendVerboseLog(`Ignored non-JSON sidecar output: ${line}`);
    return;
  }

  if (message?.event === "progress") {
    handleSidecarProgress(message);
    return;
  }

  const requestId = String(message?.id ?? "");
  const pending = state.pendingRequests.get(requestId);
  if (!pending) {
    appendVerboseLog(`Ignored sidecar response without a pending request: ${line}`);
    return;
  }
  clearTimeout(pending.timeout);
  state.pendingRequests.delete(requestId);
  pending.resolve(message);
}

function consumeSidecarOutput(chunk) {
  const text = typeof chunk === "string" ? chunk : new TextDecoder().decode(chunk);
  state.stdoutBuffer += text;
  let newline = state.stdoutBuffer.indexOf("\n");
  while (newline !== -1) {
    const line = state.stdoutBuffer.slice(0, newline).replace(/\r$/, "");
    state.stdoutBuffer = state.stdoutBuffer.slice(newline + 1);
    if (line.trim()) handleSidecarLine(line);
    newline = state.stdoutBuffer.indexOf("\n");
  }
}

function sendSidecarRequest(action, payload = {}) {
  const child = state.sidecarChild;
  if (state.sidecarStatus !== "running" || !child) {
    return Promise.reject(new Error("The sidecar is not running. Restart it and try again."));
  }

  const requestId = String(++state.nextRequestId);
  const request = { id: requestId, action, payload };
  return new Promise((resolve, reject) => {
    const pending = { action, child, resolve, reject, timeout: null };
    state.pendingRequests.set(requestId, pending);
    armRequestTimeout(requestId, pending);
    child.write(`${JSON.stringify(request)}\n`).catch((error) => {
      if (!state.pendingRequests.has(requestId)) return;
      clearTimeout(pending.timeout);
      state.pendingRequests.delete(requestId);
      const writeError = new Error(`Could not write to the sidecar: ${error.message || error}`);
      reject(writeError);
      markSidecarUnavailable(writeError.message, child);
    });
  }).then((response) => {
    if (!response.ok) {
      throw new Error(response.error?.message || `The sidecar could not ${action}.`);
    }
    return response.result;
  });
}

async function startSidecar() {
  if (state.sidecarStatus === "starting" || state.sidecarStatus === "running") return;
  state.sidecarStatus = "starting";
  syncStartButton();
  showMessage("Starting the organizer sidecar…");

  let child = null;
  try {
    const command = Command.sidecar(SIDECAR_NAME);
    state.sidecarCommand = command;
    state.stdoutBuffer = "";
    command.stdout.on("data", consumeSidecarOutput);
    command.stderr.on("data", (line) => {
      if (state.verbose) appendVerboseLog(`[sidecar] ${line}`);
    });
    command.on("error", (error) => {
      if (state.sidecarCommand === command && child) handleSidecarExit(child, error);
    });
    command.on("close", (detail) => {
      if (state.sidecarCommand === command && child) handleSidecarExit(child, detail);
    });
    child = await command.spawn();
    state.sidecarChild = child;
    state.sidecarStatus = "running";
    syncStartButton();
    showMessage("");
    if (!state.sessionInitialized) {
      try {
        await initializeUndoSession();
      } catch (error) {
        showMessage(`Could not check recovered undo history: ${error.message || error}`, "error");
      }
      syncUndoControls();
    }
  } catch (error) {
    state.sidecarCommand = null;
    state.sidecarChild = null;
    state.sidecarStatus = "down";
    syncStartButton();
    showMessage(`Could not start the organizer sidecar: ${error.message || error}`, "error");
  }
}

async function restartSidecar() {
  const previousChild = state.sidecarChild;
  const previousCommand = state.sidecarCommand;
  state.sidecarStatus = "starting";
  syncStartButton();
  showMessage("Restarting the organizer sidecar…");
  if (previousChild) {
    try {
      await previousChild.kill();
    } catch {
      // The process may already have exited.
    }
  }
  if (state.sidecarChild === previousChild) state.sidecarChild = null;
  if (state.sidecarCommand === previousCommand) state.sidecarCommand = null;
  state.sidecarStatus = "down";
  await startSidecar();
}

function finishProgress(message) {
  updateProgress({
    phase: "Complete",
    message,
    subfolderIndex: 1,
    subfolderTotal: 1,
    fileIndex: 1,
    fileTotal: 1,
    indeterminate: false,
  });
}

async function start() {
  if (state.busy || !state.sourceFolder) return;
  if (state.sidecarStatus !== "running") {
    showMessage("The sidecar is unavailable. Restart it before starting a run.", "error");
    return;
  }

  state.busy = true;
  state.undoEntryCount = 0;
  state.recoveredUndoAvailable = false;
  state.recoveredUndoCount = 0;
  syncStartButton();
  showMessage("");
  elements.progressPanel.hidden = false;
  elements.verboseLog.textContent = "";
  elements.resultsPanel.hidden = true;
  const runOptions = {
    sourceFolder: state.sourceFolder,
    includeSubfolders: state.includeSubfolders,
    verifyHash: state.verifyHash,
    verbose: state.verbose,
  };
  let reviewItems = [];

  try {
    updateProgress({ phase: "Scanning", message: "Building a preview…", indeterminate: true });
    const scan = await sendSidecarRequest("scan", runOptions);
    renderResults(scan.folders || [], { planned: true });
    reviewItems = scan.reviewItems || [];

    if (state.dryRun) {
      finishProgress("Dry run complete. No files were changed.");
      showMessage("Dry run complete. No files were changed.");
      return;
    }

    updateProgress({ phase: "Organizing", message: "Applying the file plan…", indeterminate: true });
    const organized = await sendSidecarRequest("organize", runOptions);
    renderResults(organized.folders || [], { planned: false });
    reviewItems = organized.reviewItems || reviewItems;
    state.undoEntryCount = Number(organized.undoCount) || 0;
    finishProgress("Organization complete.");
    if (organized.errors?.length) showMessage("Organization completed with errors. Review the results below.", "error");
  } catch (error) {
    showMessage(error.message || "The organizer could not complete the run.", "error");
  } finally {
    state.busy = false;
    syncStartButton();
    renderReviewQueue(reviewItems);
  }
}

function updateProgress(progress = {}) {
  const subfolderIndex = Number(progress.subfolderIndex) || 0;
  const subfolderTotal = Number(progress.subfolderTotal) || 0;
  const fileIndex = Number(progress.fileIndex) || 0;
  const fileTotal = Number(progress.fileTotal) || 0;
  const indeterminate = Boolean(progress.indeterminate) || fileTotal === 0;

  elements.progressPhase.textContent = progress.phase || "Working";
  elements.progressLabel.textContent = progress.message || "";
  elements.progressSubfolderCounter.textContent = `Subfolder ${subfolderIndex} of ${subfolderTotal}`;
  elements.progressFileCounter.textContent = `File ${fileIndex} of ${fileTotal}`;
  elements.progressPanel.hidden = false;
  elements.progressTrack.classList.toggle("is-indeterminate", indeterminate);

  if (indeterminate) {
    elements.progressTrack.removeAttribute("aria-valuenow");
    elements.progressTrack.setAttribute("aria-valuetext", "Progress total is not known");
    elements.progressFill.style.width = "30%";
  } else {
    const percent = Math.min(100, Math.max(0, (fileIndex / fileTotal) * 100));
    elements.progressTrack.setAttribute("aria-valuenow", String(Math.round(percent)));
    elements.progressTrack.setAttribute("aria-valuetext", `File ${fileIndex} of ${fileTotal}`);
    elements.progressFill.style.width = `${percent}%`;
  }
}

function appendVerboseLog(line) {
  elements.verboseLog.textContent += `${String(line)}\n`;
  elements.verboseLog.scrollTop = elements.verboseLog.scrollHeight;
}

function syncVerboseLog() {
  elements.verboseLog.hidden = !state.verbose;
}

function renderResultList(section, values, emptyMessage) {
  const empty = section.querySelector("[data-result-empty]");
  const list = section.querySelector("[data-result-list]");
  const items = Array.isArray(values) ? values : [];
  list.replaceChildren();
  empty.hidden = items.length > 0;
  empty.textContent = emptyMessage;
  list.hidden = items.length === 0;
  for (const value of items.slice(0, 500)) {
    const item = document.createElement("li");
    item.textContent = typeof value === "string"
      ? value
      : (value?.description || value?.message || value?.path || String(value));
    list.append(item);
  }
  if (items.length > 500) {
    const remaining = document.createElement("li");
    remaining.textContent = `…and ${items.length - 500} more.`;
    list.append(remaining);
  }
}

function renderResults(folders = [], { planned = false } = {}) {
  elements.resultsBody.replaceChildren();
  if (folders.length === 0) {
    const empty = document.createElement("p");
    empty.className = "results-empty";
    empty.textContent = "No run results yet.";
    elements.resultsBody.append(empty);
    elements.resultsPanel.hidden = false;
    return;
  }

  for (const folder of folders) {
    const card = elements.resultTemplate.content.firstElementChild.cloneNode(true);
    card.querySelector("[data-folder-heading]").textContent = folder.path || folder.folder || "Selected folder";
    const counts = {
      copied: folder.copied ?? 0,
      deduplicated: folder.deduplicated ?? 0,
      failed: folder.failed ?? 0,
      unmatched: folder.unmatched?.length ?? folder.unmatched ?? 0,
    };
    for (const [key, value] of Object.entries(counts)) {
      card.querySelector(`[data-result-count="${key}"]`).textContent = String(value);
    }
    card.querySelector('[data-result-count="copied"] + span').textContent = planned ? "Planned" : "Copied";
    card.querySelector('[data-result-count="deduplicated"] + span').textContent = planned
      ? "Would dedupe"
      : "Deduplicated";
    const sections = card.querySelectorAll(".result-list-section");
    const actionsSection = card.querySelector("[data-actions-section]");
    actionsSection.querySelector("[data-actions-heading]").textContent = planned ? "Planned actions" : "Actions";
    renderResultList(actionsSection, folder.actions || [], planned ? "No planned actions." : "No file actions.");
    renderResultList(sections[1], folder.unmatched || [], "No unmatched files.");
    renderResultList(sections[2], folder.errors || [], "No errors.");
    elements.resultsBody.append(card);
  }
  elements.resultsPanel.hidden = false;
}

function renderReviewQueue(items = state.reviewItems) {
  state.reviewItems = items;
  elements.reviewQueue.replaceChildren();
  if (items.length === 0) {
    const empty = document.createElement("p");
    empty.className = "results-empty";
    empty.textContent = "Nothing needs review.";
    elements.reviewQueue.append(empty);
    return;
  }

  for (const [index, item] of items.entries()) {
    const card = elements.reviewTemplate.content.firstElementChild.cloneNode(true);
    card.querySelector("[data-review-description]").textContent = item.description || "Please review this item.";
    card.querySelector("[data-review-target]").textContent = item.targetPath || "Not specified";
    card.dataset.reviewId = String(item.id ?? index);
    const resolution = card.querySelector(".review-resolution");
    for (const button of card.querySelectorAll("[data-review-answer]")) {
      button.addEventListener("click", async () => {
        if (item.answer) return;
        const answer = button.dataset.reviewAnswer;
        item.answer = answer;
        card.classList.add("is-resolved");
        resolution.textContent = answer === "yes" ? "Marked Yes" : "Marked No";
        resolution.hidden = false;
        const answerButtons = [...card.querySelectorAll("[data-review-answer]")];
        for (const answerButton of answerButtons) {
          answerButton.disabled = true;
        }
        try {
          await sendSidecarRequest("confirm", { itemId: item.id, answer });
        } catch (error) {
          item.answer = null;
          card.classList.remove("is-resolved");
          resolution.hidden = true;
          for (const answerButton of answerButtons) answerButton.disabled = false;
          showMessage(`Could not save this review answer: ${error.message || error}`, "error");
        }
      });
    }
    elements.reviewQueue.append(card);
  }
}

function initialize() {
  cacheElements();
  initTheme();
  for (const button of elements.themeButtons) {
    button.addEventListener("click", () => setTheme(button.dataset.themeChoice));
  }
  elements.includeSubfolders.addEventListener("change", () => {
    state.includeSubfolders = elements.includeSubfolders.checked;
  });
  for (const [control, key] of [
    [elements.verifyHash, "verifyHash"],
    [elements.dryRun, "dryRun"],
    [elements.verbose, "verbose"],
  ]) {
    control.addEventListener("change", () => {
      state[key] = control.checked;
      if (key === "verbose") syncVerboseLog();
    });
  }
  elements.startButton.addEventListener("click", start);
  elements.undoButton.addEventListener("click", () => performUndo());
  elements.reviewRecoveredUndo.addEventListener("click", reviewRecoveredUndo);
  elements.restartSidecar.addEventListener("click", restartSidecar);
  syncVerboseLog();
  renderPicker();
  renderReviewQueue();
  window.fileOrganizerUI = {
    showMessage,
    updateProgress,
    appendVerboseLog,
    renderResults,
    renderReviewQueue,
  };
  void startSidecar();
}

document.addEventListener("DOMContentLoaded", initialize, { once: true });
