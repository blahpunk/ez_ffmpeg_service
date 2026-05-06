const stateRef = {
  state: null,
  applying: false,
  settingsLoaded: false,
};

const els = {
  serviceStatus: document.querySelector("#serviceStatus"),
  monitorButton: document.querySelector("#monitorButton"),
  runButton: document.querySelector("#runButton"),
  folderForm: document.querySelector("#folderForm"),
  folderInput: document.querySelector("#folderInput"),
  folderBrowseButton: document.querySelector("#folderBrowseButton"),
  folderList: document.querySelector("#folderList"),
  normalizeInput: document.querySelector("#normalizeInput"),
  stereoInput: document.querySelector("#stereoInput"),
  replaceInput: document.querySelector("#replaceInput"),
  convertInput: document.querySelector("#convertInput"),
  mbMinInput: document.querySelector("#mbMinInput"),
  mbMinValue: document.querySelector("#mbMinValue"),
  thresholdInput: document.querySelector("#thresholdInput"),
  encoderInput: document.querySelector("#encoderInput"),
  tempFolderInput: document.querySelector("#tempFolderInput"),
  tempBrowseButton: document.querySelector("#tempBrowseButton"),
  queueSummary: document.querySelector("#queueSummary"),
  queueStats: document.querySelector("#queueStats"),
  clearQueueButton: document.querySelector("#clearQueueButton"),
  excludedSummary: document.querySelector("#excludedSummary"),
  clearExclusionsButton: document.querySelector("#clearExclusionsButton"),
  excludedBody: document.querySelector("#excludedBody"),
  progressFill: document.querySelector("#progressFill"),
  progressText: document.querySelector("#progressText"),
  queueBody: document.querySelector("#queueBody"),
  stopDialog: document.querySelector("#stopDialog"),
  finishStopButton: document.querySelector("#finishStopButton"),
  abortStopButton: document.querySelector("#abortStopButton"),
  browseDialog: document.querySelector("#browseDialog"),
  browseTitle: document.querySelector("#browseTitle"),
  browseUpButton: document.querySelector("#browseUpButton"),
  browsePathInput: document.querySelector("#browsePathInput"),
  browseRoots: document.querySelector("#browseRoots"),
  browseList: document.querySelector("#browseList"),
  browseSelectButton: document.querySelector("#browseSelectButton"),
};

async function request(path, payload = null) {
  const options = payload
    ? {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    : {};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error || `Request failed: ${response.status}`);
  }
  return data;
}

async function getJson(path) {
  const response = await fetch(path);
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error || `Request failed: ${response.status}`);
  }
  return data;
}

function debounce(fn, delay = 250) {
  let handle = null;
  return (...args) => {
    clearTimeout(handle);
    handle = setTimeout(() => fn(...args), delay);
  };
}

const saveSettings = debounce(async () => {
  if (stateRef.applying) return;
  try {
    await request("/api/settings", collectSettings());
    await refreshState();
  } catch (error) {
    setStatus(error.message, true);
  }
}, 250);

function collectSettings() {
  return {
    normalize: els.normalizeInput.checked,
    stereo: els.stereoInput.checked,
    replace: els.replaceInput.checked,
    convert: els.convertInput.checked,
    mb_min: Number(els.mbMinInput.value),
    threshold: Number(els.thresholdInput.value),
    encoder_mode: els.encoderInput.value,
    temp_folder: els.tempFolderInput.value.trim(),
    monitor_enabled: stateRef.state?.settings?.monitor_enabled ?? true,
  };
}

function setStatus(message, isError = false) {
  els.serviceStatus.textContent = message || "";
  els.serviceStatus.style.color = isError ? "#a33b37" : "";
}

async function refreshState() {
  try {
    const state = await request("/api/state");
    stateRef.state = state;
    render(state);
  } catch (error) {
    setStatus(error.message, true);
  }
}

function render(state) {
  applySettings(state);
  renderFolders(state.folders || [], state.folder_progress || []);
  renderMonitorState(state);
  renderRunState(state);
  renderSummary(state.summary || {});
  renderProgress(state.items || []);
  renderQueue(state.items || []);
  renderExcluded(state.excluded_items || []);

  const scanText = state.last_scan_finished
    ? `Last scan ${new Date(state.last_scan_finished * 1000).toLocaleTimeString()}`
    : "Waiting for first scan";
  const ffmpegText = state.ffmpeg_available ? "FFmpeg ready" : "FFmpeg or ffprobe not found";
  const monitorText = state.settings?.monitor_enabled ? "Monitor on" : "Monitor off";
  setStatus(
    `${ffmpegText} | ${monitorText} | ${scanText}${state.status_message ? ` | ${state.status_message}` : ""}`,
    !state.ffmpeg_available,
  );
}

function applySettings(state) {
  stateRef.applying = true;
  const settings = state.settings || {};
  if (!stateRef.settingsLoaded || document.activeElement !== els.tempFolderInput) {
    els.tempFolderInput.value = settings.temp_folder || "";
  }
  els.normalizeInput.checked = Boolean(settings.normalize);
  els.stereoInput.checked = Boolean(settings.stereo);
  els.replaceInput.checked = Boolean(settings.replace);
  els.convertInput.checked = Boolean(settings.convert);
  els.mbMinInput.value = settings.mb_min ?? 12;
  els.mbMinValue.textContent = settings.mb_min ?? 12;
  if (!stateRef.settingsLoaded || document.activeElement !== els.thresholdInput) {
    els.thresholdInput.value = settings.threshold ?? 2;
  }

  const existing = new Set([...els.encoderInput.options].map((option) => option.value));
  const incoming = new Set((state.encoder_options || []).map((option) => option.value));
  const needsEncoderRender =
    existing.size !== incoming.size || [...incoming].some((value) => !existing.has(value));
  if (needsEncoderRender) {
    els.encoderInput.replaceChildren();
    for (const option of state.encoder_options || []) {
      const node = document.createElement("option");
      node.value = option.value;
      node.textContent = option.label;
      els.encoderInput.append(node);
    }
  }
  els.encoderInput.value = settings.encoder_mode || "auto";
  stateRef.settingsLoaded = true;
  stateRef.applying = false;
}

function renderFolders(folders, progressItems = []) {
  els.folderList.replaceChildren();
  if (!folders.length) {
    const empty = document.createElement("div");
    empty.className = "folder-empty";
    empty.textContent = "No monitored folders yet.";
    els.folderList.append(empty);
    return;
  }

  const progressByPath = new Map(progressItems.map((progress) => [progress.path, progress]));
  for (const folder of folders) {
    const row = document.createElement("div");
    row.className = "folder-row";

    const details = document.createElement("div");
    details.className = "folder-details";

    const path = document.createElement("div");
    path.className = "folder-path";
    path.textContent = folder;
    path.title = folder;

    const progress = progressByPath.get(folder);
    const progressWrap = document.createElement("div");
    progressWrap.className = "folder-progress";
    const progressFill = document.createElement("div");
    progressFill.className = "folder-progress-fill";
    progressFill.style.width = `${Math.max(0, Math.min(progress?.percent ?? 0, 100))}%`;
    progressWrap.append(progressFill);

    const progressText = document.createElement("div");
    progressText.className = "folder-progress-text";
    progressText.textContent = formatFolderProgress(progress);

    details.append(path, progressWrap, progressText);

    const remove = document.createElement("button");
    remove.className = "icon-button";
    remove.type = "button";
    remove.title = "Remove folder";
    remove.textContent = "x";
    remove.addEventListener("click", async () => {
      try {
        await request("/api/folders/remove", { path: folder });
        await refreshState();
      } catch (error) {
        setStatus(error.message, true);
      }
    });

    row.append(details, remove);
    els.folderList.append(row);
  }
}

function formatFolderProgress(progress) {
  if (!progress) return "Waiting";
  const phase = progress.phase || "Waiting";
  if (phase === "Counting") {
    return `Counting: ${progress.counted_files || 0} videos found`;
  }
  if (phase === "Loading") {
    return `Loading: ${progress.processed_files || 0}/${progress.total_files || 0} checked, ${
      progress.loaded_files || 0
    } analyzed`;
  }
  if (phase === "Complete") {
    return `Complete: ${progress.processed_files || 0}/${progress.total_files || 0} checked`;
  }
  if (phase === "Paused") {
    return `Paused: ${progress.processed_files || 0}/${progress.total_files || 0} checked`;
  }
  if (phase === "Missing") {
    return "Folder not found";
  }
  return phase;
}

function renderRunState(state) {
  els.runButton.classList.toggle("is-running", Boolean(state.run_enabled));
  els.runButton.textContent = state.run_enabled ? "Stop Conversion" : "Run Conversion";
}

function renderMonitorState(state) {
  const enabled = Boolean(state.settings?.monitor_enabled);
  els.monitorButton.classList.toggle("is-off", !enabled);
  els.monitorButton.textContent = enabled ? "Monitor On" : "Monitor Off";
}

function renderSummary(summary) {
  els.queueSummary.textContent = `Current file ETA: ${summary.current_eta || "--"} | Queue remaining: ${
    summary.queue_remaining || "--"
  } | Finish: ${summary.finish || "--"}`;
  els.queueStats.textContent = `Queued: ${summary.queued || 0} | Processing: ${
    summary.processing || 0
  } | Completed: ${summary.completed || 0} | Skipped: ${summary.skipped || 0} | Failed: ${
    summary.failed || 0
  } | Saved: ${formatNumber(summary.saved_mb, 2)} MB`;
}

function renderProgress(items) {
  const current = items.find((item) => item.is_current);
  const progress = current ? Number(current.progress || 0) : 0;
  els.progressFill.style.width = `${Math.max(0, Math.min(progress, 100))}%`;
  const parts = [`${progress.toFixed(1)}%`];
  if (current?.avg_speed_display) parts.push(current.avg_speed_display);
  if (current?.eta_display && current.eta_display !== "--") parts.push(`ETA ${current.eta_display}`);
  els.progressText.textContent = parts.join(" | ");
}

function renderQueue(items) {
  els.queueBody.replaceChildren();
  for (const item of items) {
    const row = document.createElement("tr");
    row.className = rowClass(item);

    const filename = document.createElement("td");
    filename.className = "filename-cell";
    const name = document.createElement("div");
    name.className = "filename-main";
    name.textContent = item.filename || "";
    const path = document.createElement("div");
    path.className = "filename-path";
    path.textContent = item.path || "";
    path.title = item.path || "";
    filename.append(name, path);
    row.append(filename);

    appendCell(row, item.status || "");
    appendCell(row, item.encoder || "");
    appendCell(row, item.video_codec_label || "");
    appendCell(row, item.resolution_label || "");
    appendCell(row, item.audio_label || "");
    appendCell(row, formatNumber(item.size_mb, 2));
    appendCell(row, formatNumber(item.mb_per_min_before, 2));
    appendCell(row, item.length_formatted || "");
    appendCell(row, item.eta_display || "--");
    appendCell(row, item.elapsed_display || "");
    appendCell(row, item.avg_speed_display || "");
    appendCell(row, formatNumber(item.output_size_mb, 2));
    appendCell(row, formatNumber(item.mb_per_min_after, 2));

    els.queueBody.append(row);
  }
}

function renderExcluded(items) {
  els.excludedBody.replaceChildren();
  els.excludedSummary.textContent = items.length ? `${items.length} excluded file${items.length === 1 ? "" : "s"}` : "No excluded files.";
  els.clearExclusionsButton.disabled = items.length === 0;

  for (const item of items) {
    const row = document.createElement("tr");
    row.className = "is-error";

    const filename = document.createElement("td");
    filename.className = "filename-cell";
    const name = document.createElement("div");
    name.className = "filename-main";
    name.textContent = item.filename || "";
    const path = document.createElement("div");
    path.className = "filename-path";
    path.textContent = item.path || "";
    path.title = item.path || "";
    filename.append(name, path);
    row.append(filename);

    appendCell(row, item.error || item.status || "Output was not smaller");
    appendCell(row, formatNumber(item.size_mb, 2));
    appendCell(row, formatNumber(item.mb_per_min_before, 2));
    appendCell(row, item.length_formatted || "");
    appendCell(row, formatNumber(item.output_size_mb, 2));
    appendCell(row, formatNumber(item.mb_per_min_after, 2));

    const actionCell = document.createElement("td");
    const remove = document.createElement("button");
    remove.className = "icon-button";
    remove.type = "button";
    remove.title = "Clear this exclusion";
    remove.textContent = "x";
    remove.addEventListener("click", async () => {
      try {
        await request("/api/exclusions/remove", { path: item.path });
        await refreshState();
      } catch (error) {
        setStatus(error.message, true);
      }
    });
    actionCell.append(remove);
    row.append(actionCell);

    els.excludedBody.append(row);
  }
}

function rowClass(item) {
  const classes = [];
  if (item.is_current) classes.push("is-current");
  if (item.status === "Ready") classes.push("is-ready");
  if (item.status === "Skipped" || item.status === "Below threshold") classes.push("is-skip");
  if ((item.status || "").startsWith("Error") || (item.status || "").startsWith("Exception")) {
    classes.push("is-error");
  }
  return classes.join(" ");
}

function appendCell(row, value) {
  const cell = document.createElement("td");
  cell.textContent = value ?? "";
  row.append(cell);
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return number.toFixed(digits);
}

async function openFolderBrowser(mode) {
  stateRef.browseMode = mode;
  els.browseTitle.textContent = mode === "temp" ? "Choose Temp Folder" : "Add Monitored Folder";
  els.browseSelectButton.textContent = mode === "temp" ? "Use For Temp" : "Add Folder";
  const initialPath =
    mode === "temp"
      ? els.tempFolderInput.value.trim() || stateRef.state?.settings?.temp_folder || ""
      : els.folderInput.value.trim();

  if (typeof els.browseDialog.showModal === "function") {
    els.browseDialog.showModal();
  }

  await loadBrowseDirectory(initialPath);
}

async function loadBrowseDirectory(path = "") {
  try {
    const query = new URLSearchParams();
    if (path) query.set("path", path);
    const data = await getJson(`/api/browse?${query.toString()}`);
    stateRef.browsePath = data.current_path || "";
    stateRef.browseParentPath = data.parent_path || "";
    renderBrowseDialog(data);
  } catch (error) {
    setStatus(error.message, true);
    if (path) {
      await loadBrowseDirectory("");
    }
  }
}

function renderBrowseDialog(data) {
  els.browsePathInput.value = data.current_path || "Computer";
  els.browseUpButton.disabled = !data.parent_path;
  els.browseSelectButton.disabled = !data.current_path;

  els.browseRoots.replaceChildren();
  for (const root of data.roots || []) {
    const button = document.createElement("button");
    button.className = "secondary-button";
    button.type = "button";
    button.textContent = root.name;
    button.addEventListener("click", () => loadBrowseDirectory(root.path));
    els.browseRoots.append(button);
  }

  els.browseList.replaceChildren();
  const entries = data.entries || [];
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "browse-empty";
    empty.textContent = "No folders here.";
    els.browseList.append(empty);
    return;
  }

  for (const entry of entries) {
    const row = document.createElement("button");
    row.className = "browse-row";
    row.type = "button";
    row.textContent = entry.name;
    row.title = entry.path;
    row.addEventListener("click", () => loadBrowseDirectory(entry.path));
    els.browseList.append(row);
  }
}

async function useBrowsedFolder() {
  const path = stateRef.browsePath;
  if (!path) return;

  try {
    if (stateRef.browseMode === "temp") {
      els.tempFolderInput.value = path;
      await request("/api/settings", { ...collectSettings(), temp_folder: path });
    } else {
      await request("/api/folders", { path });
      els.folderInput.value = "";
    }
    els.browseDialog.close();
    await refreshState();
  } catch (error) {
    setStatus(error.message, true);
  }
}

els.folderForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const path = els.folderInput.value.trim();
  if (!path) return;
  try {
    await request("/api/folders", { path });
    els.folderInput.value = "";
    await refreshState();
  } catch (error) {
    setStatus(error.message, true);
  }
});

els.folderBrowseButton.addEventListener("click", async () => {
  try {
    await openFolderBrowser("monitor");
  } catch (error) {
    setStatus(error.message, true);
  }
});

els.monitorButton.addEventListener("click", async () => {
  const enabled = Boolean(stateRef.state?.settings?.monitor_enabled);
  try {
    await request("/api/settings", { monitor_enabled: !enabled });
    await refreshState();
  } catch (error) {
    setStatus(error.message, true);
  }
});

els.clearQueueButton.addEventListener("click", async () => {
  try {
    await request("/api/queue/clear", {});
    await refreshState();
  } catch (error) {
    setStatus(error.message, true);
  }
});

els.clearExclusionsButton.addEventListener("click", async () => {
  try {
    await request("/api/exclusions/clear", {});
    await refreshState();
  } catch (error) {
    setStatus(error.message, true);
  }
});

for (const input of [
  els.normalizeInput,
  els.stereoInput,
  els.replaceInput,
  els.convertInput,
  els.mbMinInput,
  els.thresholdInput,
  els.encoderInput,
]) {
  input.addEventListener("input", () => {
    if (input === els.mbMinInput) els.mbMinValue.textContent = els.mbMinInput.value;
    saveSettings();
  });
  input.addEventListener("change", saveSettings);
}

els.tempFolderInput.addEventListener("change", saveSettings);
els.tempFolderInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    saveSettings();
    els.tempFolderInput.blur();
  }
});

els.tempBrowseButton.addEventListener("click", async () => {
  try {
    await openFolderBrowser("temp");
  } catch (error) {
    setStatus(error.message, true);
  }
});

els.browseUpButton.addEventListener("click", () => {
  loadBrowseDirectory(stateRef.browseParentPath || "");
});

els.browseSelectButton.addEventListener("click", useBrowsedFolder);

document.querySelectorAll("[data-preset]").forEach((button) => {
  button.addEventListener("click", () => {
    const preset = button.dataset.preset;
    if (preset === "movies") {
      els.mbMinInput.value = 10;
      els.thresholdInput.value = 2;
    }
    if (preset === "television") {
      els.mbMinInput.value = 12;
      els.thresholdInput.value = 2;
    }
    if (preset === "animation") {
      els.mbMinInput.value = 8;
      els.thresholdInput.value = 1;
    }
    els.mbMinValue.textContent = els.mbMinInput.value;
    saveSettings();
  });
});

els.runButton.addEventListener("click", async () => {
  const state = stateRef.state;
  if (!state?.run_enabled) {
    try {
      await request("/api/run", { enabled: true });
      await refreshState();
    } catch (error) {
      setStatus(error.message, true);
    }
    return;
  }

  if (state.processing_active && typeof els.stopDialog.showModal === "function") {
    els.stopDialog.showModal();
    return;
  }

  try {
    await request("/api/run", { enabled: false, mode: "finish" });
    await refreshState();
  } catch (error) {
    setStatus(error.message, true);
  }
});

els.finishStopButton.addEventListener("click", async () => {
  els.stopDialog.close();
  try {
    await request("/api/run", { enabled: false, mode: "finish" });
    await refreshState();
  } catch (error) {
    setStatus(error.message, true);
  }
});

els.abortStopButton.addEventListener("click", async () => {
  els.stopDialog.close();
  try {
    await request("/api/run", { enabled: false, mode: "abort" });
    await refreshState();
  } catch (error) {
    setStatus(error.message, true);
  }
});

refreshState();
setInterval(refreshState, 1000);
