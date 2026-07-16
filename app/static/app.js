"use strict";

const STAGES = [
  "抽帧与预处理",
  "AI视频指纹检索",
  "画面主轨勘验",
  "时间轴主轨勘验",
  "声音主轨勘验",
  "副轨遮挡与伪装检测",
  "两轮判定与报告生成",
];

const STATUS = {
  completed: { label: "已完成", className: "completed" },
  processing: { label: "分析中", className: "processing" },
  failed: { label: "失败", className: "failed" },
  interrupted: { label: "已中断", className: "interrupted" },
};

const TIER = {
  red: { full: "高度侵权风险", short: "高", icon: "▲", code: "RED / HIGH" },
  yellow: { full: "中等侵权风险", short: "中", icon: "●", code: "YELLOW / MED" },
  gray: { full: "低风险 / 具备合理使用空间", short: "低", icon: "○", code: "GRAY / LOW" },
};

const SETTING_FIELDS = [
  { key: "fuse_visual_ratio", label: "画面硬阈值比例", hint: "画面一致帧占比达到即触发高风险", kind: "ratio", step: .05 },
  { key: "fuse_audio_ratio", label: "原声硬阈值比例", hint: "原声重合占比达到即触发高风险", kind: "ratio", step: .05 },
  { key: "fuse_continuous_seconds", label: "连续匹配秒数", hint: "最长连续视觉匹配片段阈值", kind: "seconds", step: 5 },
  { key: "fuse_density_ratio", label: "原片时长密度比例", hint: "污染秒数占原片时长比例阈值", kind: "ratio", step: .05 },
  { key: "penalty_yellow_min", label: "黄区罚分阈值", hint: "第二轮累计罚分达到即判中风险", kind: "points", step: 5 },
  { key: "subtitle_min_hits", label: "最小字幕命中数", hint: "触发字幕遮挡罚分的最小命中帧数", kind: "count", step: 1 },
  { key: "watermark_min_hits", label: "最小水印命中数", hint: "触发水印遮挡罚分的最小命中帧数", kind: "count", step: 1 },
];

const state = {
  screen: "library",
  cases: [],
  activeCaseId: null,
  activeDetail: null,
  activeComparisons: [],
  activeComparison: null,
  progress: null,
  pollingTimer: null,
  settingsPayload: null,
  settingsPreset: "standard",
  settingsDraft: null,
  settingsSaving: false,
  create: { original: null, suspects: [], uploading: false },
  modal: null,
  toastTimer: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function apiPathPart(value) {
  return encodeURIComponent(String(value ?? ""));
}

async function apiJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const payload = await response.json();
      message = payload.detail || payload.error || message;
    } catch {
      const text = await response.text();
      if (text) message = text;
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function formatDate(value) {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date).replaceAll("/", "-");
}

function formatDuration(value) {
  const total = Math.max(0, Math.round(Number(value) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function percent(value, digits = 1) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(digits)}%` : "—";
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function settingDisplay(field, value) {
  if (field.kind === "ratio") return `${Math.round(Number(value) * 100)}%`;
  if (field.kind === "seconds") return `${Number(value)}s`;
  if (field.kind === "points") return `${Number(value)} 分`;
  return String(Number(value));
}

function currentSettings() {
  return state.settingsPayload?.settings || null;
}

function currentPresetLabel() {
  const settings = currentSettings();
  if (!settings) return "读取中";
  if (settings.preset === "custom") return "自定义";
  return state.settingsPayload?.presets?.[settings.preset]?.label || settings.preset;
}

function normalizeStatus(value) {
  return STATUS[value] ? value : "completed";
}

function statusBadge(status) {
  const normalized = normalizeStatus(status);
  const item = STATUS[normalized];
  return `<span class="status-badge status-${item.className}">${item.label}</span>`;
}

function riskBar(counts, total) {
  const denominator = Math.max(1, Number(total) || 0);
  const red = Number(counts?.red) || 0;
  const yellow = Number(counts?.yellow) || 0;
  const gray = Number(counts?.gray) || 0;
  return `<div class="risk-bar" aria-label="高风险 ${red}，中风险 ${yellow}，低风险 ${gray}">
    <span class="red" style="width:${red / denominator * 100}%"></span>
    <span class="yellow" style="width:${yellow / denominator * 100}%"></span>
    <span class="gray" style="width:${gray / denominator * 100}%"></span>
  </div>`;
}

function tierBadge(color) {
  const item = TIER[color];
  if (!item) return `<span class="tier-badge tier-failed">分析失败</span>`;
  return `<span class="tier-badge tier-${color}">${item.icon} ${item.short}</span>`;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.remove("is-hidden");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.add("is-hidden"), 2800);
}

function stopPolling() {
  if (state.pollingTimer) clearTimeout(state.pollingTimer);
  state.pollingTimer = null;
}

function setScreen(name) {
  state.screen = name;
  if (name !== "progress") stopPolling();
  $$(".screen").forEach((screen) => screen.classList.toggle("is-active", screen.id === `screen-${name}`));
  $("#nav-library").classList.toggle("is-active", name !== "settings");
  $("#nav-settings").classList.toggle("is-active", name === "settings");
  $("#main-content").scrollTop = 0;
  window.scrollTo(0, 0);
}

function goLibrary() {
  state.activeCaseId = null;
  state.activeDetail = null;
  state.activeComparison = null;
  setScreen("library");
  loadCases();
}

async function initialize() {
  bindStaticEvents();
  await Promise.allSettled([loadSettings(), loadCases()]);
}

function bindStaticEvents() {
  $("#nav-library").addEventListener("click", goLibrary);
  $("#mobile-library").addEventListener("click", goLibrary);
  $("#nav-settings").addEventListener("click", openSettings);
  $("#mobile-settings").addEventListener("click", openSettings);
  $("#new-case").addEventListener("click", openCreate);
  $("#empty-new-case").addEventListener("click", openCreate);
  $$('[data-go="library"]').forEach((button) => button.addEventListener("click", goLibrary));
  $("#create-open-settings").addEventListener("click", openSettings);
  $("#back-overview").addEventListener("click", () => {
    setScreen("overview");
    renderOverview();
  });

  $("#original-drop").addEventListener("click", () => $("#original-input").click());
  $("#suspect-drop").addEventListener("click", () => $("#suspect-input").click());
  $("#original-input").addEventListener("change", (event) => {
    const [file] = event.target.files;
    if (file) state.create.original = file;
    event.target.value = "";
    renderCreateFiles();
  });
  $("#suspect-input").addEventListener("change", (event) => {
    addSuspectFiles(event.target.files);
    event.target.value = "";
  });
  bindDropZone($("#original-drop"), (files) => {
    if (files[0]) state.create.original = files[0];
    renderCreateFiles();
  });
  bindDropZone($("#suspect-drop"), addSuspectFiles);
  $("#original-selection").addEventListener("click", (event) => {
    if (event.target.closest("[data-remove-original]")) {
      state.create.original = null;
      renderCreateFiles();
    }
  });
  $("#suspect-files").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-suspect]");
    if (!button) return;
    state.create.suspects.splice(Number(button.dataset.removeSuspect), 1);
    renderCreateFiles();
  });
  $("#create-form").addEventListener("submit", submitCase);

  $("#case-list").addEventListener("click", handleCaseAction);
  $("#overview-content").addEventListener("click", handleOverviewAction);
  $("#detail-content").addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-toggle-technical]");
    if (!toggle) return;
    toggle.classList.toggle("is-open");
    $("#technical-body")?.classList.toggle("is-hidden");
  });
  $("#preset-grid").addEventListener("click", handlePresetSelect);
  $("#settings-fields").addEventListener("click", handleSettingStep);
  $("#save-settings").addEventListener("click", saveSettings);

  $("#modal-layer").addEventListener("click", (event) => {
    if (event.target.id === "modal-layer" || event.target.closest("[data-close-modal]")) closeModal();
  });
  $("#modal-content").addEventListener("submit", handleModalSubmit);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.modal) closeModal();
  });
}

function bindDropZone(element, onFiles) {
  ["dragenter", "dragover"].forEach((name) => element.addEventListener(name, (event) => {
    event.preventDefault();
    element.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((name) => element.addEventListener(name, (event) => {
    event.preventDefault();
    element.classList.remove("is-dragging");
  }));
  element.addEventListener("drop", (event) => onFiles(Array.from(event.dataTransfer?.files || [])));
}

async function loadCases() {
  const loading = $("#library-loading");
  const errorBox = $("#library-error");
  if (state.screen === "library") loading.classList.remove("is-hidden");
  errorBox.classList.add("is-hidden");
  try {
    const payload = await apiJson("/api/cases");
    state.cases = payload.cases || [];
    renderLibrary();
  } catch (error) {
    errorBox.textContent = `案件读取失败：${error.message}`;
    errorBox.classList.remove("is-hidden");
    $("#case-list").classList.add("is-hidden");
    $("#case-empty").classList.add("is-hidden");
  } finally {
    loading.classList.add("is-hidden");
  }
}

function renderLibrary() {
  const list = $("#case-list");
  const empty = $("#case-empty");
  if (!state.cases.length) {
    list.classList.add("is-hidden");
    empty.classList.remove("is-hidden");
    list.innerHTML = "";
    return;
  }
  empty.classList.add("is-hidden");
  list.classList.remove("is-hidden");
  list.innerHTML = state.cases.map(caseCardHtml).join("");
}

function caseCardHtml(item) {
  const id = item.case_id;
  const status = normalizeStatus(item.status);
  const counts = item.risk_counts || { red: 0, yellow: 0, gray: 0 };
  const total = Number(item.suspect_count) || 0;
  const processing = status === "processing";
  const completed = status === "completed";
  const preset = item.threshold_preset_label || (item.threshold_preset === "custom" ? "自定义" : "未记录");
  let banner = "";
  if (processing) banner = `<div class="case-banner processing">正在处理可疑视频；打开案件可查看当前进度。</div>`;
  if (status === "failed") banner = `<div class="case-banner failed">${escapeHtml(item.error || "案件分析失败。")}</div>`;
  if (status === "interrupted") banner = `<div class="case-banner interrupted">应用在分析期间停止或重启，此案件没有完整结果。</div>`;
  return `<article class="case-card">
    <div class="case-main">
      <div class="case-identity">
        <div class="case-name-row">${statusBadge(status)}<h2 title="${escapeHtml(item.name)}">${escapeHtml(item.name || `案件 ${id}`)}</h2></div>
        <div class="case-meta">
          <code>${escapeHtml(id)}</code>
          <span class="file-name" title="${escapeHtml(item.original_filename)}">原始视频：${escapeHtml(item.original_filename || "未记录")}</span>
          <span>更新于 ${escapeHtml(formatDate(item.updated_at || item.created_at))}</span>
          <span class="snapshot-chip">${escapeHtml(preset)}</span>
        </div>
      </div>
      <div class="case-counts">
        <div class="case-count-copy"><span>可疑视频 <b>${total}</b> 个</span><span>完成 ${Number(item.completed_count) || 0} · 失败 ${Number(item.failed_count) || 0}</span></div>
        ${riskBar(counts, total)}
        <div class="risk-legend"><span class="red"><i></i>高 ${counts.red || 0}</span><span class="yellow"><i></i>中 ${counts.yellow || 0}</span><span class="gray"><i></i>低 ${counts.gray || 0}</span></div>
      </div>
      <div class="case-actions">
        <button class="btn btn-primary" data-case-action="open" data-case-id="${escapeHtml(id)}" type="button">${processing ? "查看进度" : "打开案件"}</button>
        <div class="action-pair">
          <button class="btn" data-case-action="rename" data-case-id="${escapeHtml(id)}" type="button">重命名</button>
          <button class="btn btn-subtle-danger" data-case-action="delete" data-case-id="${escapeHtml(id)}" type="button" ${processing ? "disabled" : ""}>删除</button>
        </div>
        <button class="download-link" data-case-action="report" data-case-id="${escapeHtml(id)}" type="button" ${completed ? "" : "disabled"}>↓ PDF 报告</button>
      </div>
    </div>${banner}
  </article>`;
}

function handleCaseAction(event) {
  const button = event.target.closest("[data-case-action]");
  if (!button) return;
  const id = button.dataset.caseId;
  const item = state.cases.find((entry) => entry.case_id === id);
  if (!item) return;
  const action = button.dataset.caseAction;
  if (action === "open") openCase(item);
  if (action === "rename") openRename(item);
  if (action === "delete") openDelete(item);
  if (action === "report") downloadReport(id);
}

function openCreate() {
  state.create = { original: null, suspects: [], uploading: false };
  $("#case-name").value = "";
  $("#create-error").classList.add("is-hidden");
  renderCreateFiles();
  renderCreateSettings();
  setScreen("create");
}

function addSuspectFiles(fileList) {
  const existing = new Set(state.create.suspects.map((file) => `${file.name}:${file.size}:${file.lastModified}`));
  for (const file of Array.from(fileList || [])) {
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (!existing.has(key)) {
      state.create.suspects.push(file);
      existing.add(key);
    }
  }
  renderCreateFiles();
}

function renderCreateFiles() {
  const original = state.create.original;
  const selection = $("#original-selection");
  const drop = $("#original-drop");
  if (original) {
    selection.innerHTML = `<span class="file-icon">▸</span><div class="file-copy"><strong>${escapeHtml(original.name)}</strong><span>原始视频 · ${formatBytes(original.size)}</span></div><button class="remove-file" data-remove-original type="button" aria-label="移除原始视频">×</button>`;
    selection.classList.remove("is-hidden");
    drop.classList.add("is-hidden");
  } else {
    selection.classList.add("is-hidden");
    selection.innerHTML = "";
    drop.classList.remove("is-hidden");
  }
  $("#suspect-files").innerHTML = state.create.suspects.map((file, index) => `<div class="selected-file"><span class="file-icon">▸</span><div class="file-copy"><strong>${escapeHtml(file.name)}</strong><span>可疑视频 · ${formatBytes(file.size)}</span></div><button class="remove-file" data-remove-suspect="${index}" type="button" aria-label="移除 ${escapeHtml(file.name)}">×</button></div>`).join("");
  $("#suspect-count").textContent = state.create.suspects.length;
  const submit = $("#submit-case");
  submit.disabled = !original || !state.create.suspects.length || state.create.uploading;
  submit.innerHTML = state.create.uploading ? `<span class="spinner"></span>正在上传并创建…` : "开始分析";
}

function renderCreateSettings() {
  const settings = currentSettings();
  const container = $("#create-settings-snapshot");
  if (!settings) {
    container.innerHTML = `<div class="skeleton-line"></div><div class="skeleton-line"></div>`;
    return;
  }
  const values = settings.values || {};
  const items = [
    ["画面 / 原声", `${settingDisplay(SETTING_FIELDS[0], values.fuse_visual_ratio)} / ${settingDisplay(SETTING_FIELDS[1], values.fuse_audio_ratio)}`],
    ["连续匹配", settingDisplay(SETTING_FIELDS[2], values.fuse_continuous_seconds)],
    ["原片时长密度", settingDisplay(SETTING_FIELDS[3], values.fuse_density_ratio)],
    ["黄区罚分", settingDisplay(SETTING_FIELDS[4], values.penalty_yellow_min)],
    ["字幕命中", settingDisplay(SETTING_FIELDS[5], values.subtitle_min_hits)],
    ["水印命中", settingDisplay(SETTING_FIELDS[6], values.watermark_min_hits)],
  ];
  container.innerHTML = `<div class="snapshot-item preset"><span>当前预设</span><b>${escapeHtml(currentPresetLabel())} · v${Number(settings.revision) || 1}</b></div>${items.map(([label, value]) => `<div class="snapshot-item"><span>${label}</span><b>${escapeHtml(value)}</b></div>`).join("")}`;
}

async function submitCase(event) {
  event.preventDefault();
  const errorBox = $("#create-error");
  if (!state.create.original) return showCreateError("请先选择一个原始视频。");
  if (!state.create.suspects.length) return showCreateError("请至少添加一个可疑视频。");
  state.create.uploading = true;
  errorBox.classList.add("is-hidden");
  renderCreateFiles();
  const form = new FormData();
  form.append("case_name", $("#case-name").value.trim());
  form.append("original", state.create.original);
  state.create.suspects.forEach((file) => form.append("suspects", file));
  try {
    const created = await apiJson("/api/cases", { method: "POST", body: form });
    const settings = currentSettings();
    state.activeCaseId = created.case_id;
    state.activeDetail = null;
    state.progress = { status: "running", stage: "排队中", current: 0, total: created.count, current_name: null };
    state.cases.unshift({
      case_id: created.case_id,
      name: created.name,
      original_filename: state.create.original.name,
      suspect_count: created.count,
      status: "processing",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      threshold_preset: settings?.preset,
      threshold_preset_label: currentPresetLabel(),
      threshold_revision: settings?.revision,
      thresholds: settings?.values,
      risk_counts: { red: 0, yellow: 0, gray: 0 },
    });
    setScreen("progress");
    renderProgress();
    startPolling();
  } catch (error) {
    showCreateError(`案件创建失败：${error.message}`);
  } finally {
    state.create.uploading = false;
    renderCreateFiles();
  }
}

function showCreateError(message) {
  const box = $("#create-error");
  box.textContent = message;
  box.classList.remove("is-hidden");
}

async function openCase(summary) {
  state.activeCaseId = summary.case_id;
  state.activeDetail = null;
  state.activeComparison = null;
  if (summary.status === "processing") {
    state.progress = { status: "running", stage: "排队中", current: 0, total: summary.suspect_count, current_name: null };
    setScreen("progress");
    renderProgress();
    startPolling();
    return;
  }
  if (summary.status === "failed") {
    state.progress = { status: "error", error: summary.error || "案件分析失败。" };
    setScreen("progress");
    renderProgress();
    return;
  }
  if (summary.status === "interrupted") {
    state.activeDetail = { case: summary, results: [], errors: [], report: null, interrupted: true };
    setScreen("overview");
    renderOverview();
    return;
  }
  setScreen("overview");
  $("#overview-content").innerHTML = `<div class="loading-card"><span class="spinner"></span>正在读取案件结果并准备报告…</div>`;
  await loadCaseDetail(summary.case_id, summary);
}

async function loadCaseDetail(caseId, fallbackSummary = null) {
  try {
    const payload = await apiJson(`/api/cases/${apiPathPart(caseId)}`);
    if (payload.status && !payload.results) {
      state.progress = payload.status;
      state.activeDetail = null;
      setScreen("progress");
      renderProgress();
      startPolling();
      return;
    }
    state.activeDetail = payload;
    state.progress = null;
    state.activeComparisons = buildComparisons(payload);
    setScreen("overview");
    renderOverview();
    loadCasesSilently();
  } catch (error) {
    if (fallbackSummary?.status === "interrupted" || error.status === 404) {
      state.activeDetail = { case: fallbackSummary || { case_id: caseId, name: `案件 ${caseId}` }, results: [], errors: [], report: null, interrupted: true };
      renderOverview();
      return;
    }
    $("#overview-content").innerHTML = `<div class="notice notice-error">案件读取失败：${escapeHtml(error.message)}</div>`;
  }
}

async function loadCasesSilently() {
  try {
    const payload = await apiJson("/api/cases");
    state.cases = payload.cases || [];
  } catch { /* Keep the current local list. */ }
}

function activeSummary() {
  return state.cases.find((item) => item.case_id === state.activeCaseId) || state.activeDetail?.case || {};
}

function startPolling() {
  stopPolling();
  pollJob();
}

async function pollJob() {
  if (state.screen !== "progress" || !state.activeCaseId) return;
  try {
    const progress = await apiJson(`/api/jobs/${apiPathPart(state.activeCaseId)}`);
    state.progress = progress;
    renderProgress();
    if (progress.status === "done") {
      await loadCaseDetail(state.activeCaseId, activeSummary());
      return;
    }
    if (progress.status === "error") {
      loadCasesSilently();
      return;
    }
  } catch (error) {
    state.progress = { status: "error", error: error.message };
    renderProgress();
    return;
  }
  state.pollingTimer = setTimeout(pollJob, 3000);
}

function renderProgress() {
  const summary = activeSummary();
  $("#progress-title").textContent = summary.name || "案件分析";
  $("#progress-case-id").textContent = state.activeCaseId || "";
  const progress = state.progress || {};
  const content = $("#progress-content");
  if (progress.status === "error" || summary.status === "failed") {
    content.innerHTML = `<div class="failed-state"><div class="failure-icon">!</div><h2>案件分析失败</h2><p>${escapeHtml(progress.error || summary.error || "分析过程中发生错误。")}</p></div>`;
    return;
  }
  const total = Math.max(1, Number(progress.total || summary.suspect_count) || 1);
  const current = Math.max(1, Number(progress.current) || 1);
  const stageIndex = STAGES.indexOf(progress.stage);
  const effectiveStage = stageIndex >= 0 ? stageIndex : 0;
  const completedSteps = (current - 1) * STAGES.length + effectiveStage + 1;
  const overall = progress.status === "done" ? 100 : Math.min(99, Math.max(1, Math.round(completedSteps / (total * STAGES.length) * 100)));
  const stages = STAGES.map((label, index) => {
    const done = index < effectiveStage;
    const active = index === effectiveStage;
    return `<div class="stage ${done ? "done" : active ? "active" : ""}"><span class="stage-dot">${done ? "✓" : ""}</span><span>${label}</span>${active ? `<span class="stage-state">进行中…</span>` : ""}</div>`;
  }).join("");
  content.innerHTML = `<div class="card progress-card">
    <div class="progress-top">
      <div class="progress-current"><span class="large-spinner"></span><div><h2>正在处理第 ${current} / ${total} 个可疑视频</h2><p>${escapeHtml(progress.current_name || progress.stage || "等待分析任务启动")}</p></div></div>
      <div class="progress-percent"><strong>${overall}%</strong><span>估算整体进度</span></div>
    </div>
    <div class="progress-bar"><span style="width:${overall}%"></span></div>
    <div class="stage-title">当前分析阶段</div><div class="stages">${stages}</div>
    <div class="poll-note">系统每 3 秒轮询一次服务器状态。可疑视频将按顺序依次处理。</div>
  </div>`;
}

function buildComparisons(payload) {
  const completed = (payload.results || []).map((item) => ({
    key: item.result_id,
    resultId: item.result_id,
    filename: item.filename,
    status: "completed",
    result: item.result,
  }));
  const failed = (payload.errors || []).map((item, index) => ({
    key: `failed-${index}`,
    resultId: null,
    filename: item.filename,
    status: "failed",
    error: item.error,
    result: null,
  }));
  return [...completed, ...failed];
}

function renderOverview() {
  const payload = state.activeDetail;
  if (!payload) return;
  const caseData = payload.case || activeSummary();
  const comparisons = state.activeComparisons = buildComparisons(payload);
  const counts = { red: 0, yellow: 0, gray: 0 };
  comparisons.forEach((item) => {
    const color = item.result?.tier?.color;
    if (color in counts) counts[color] += 1;
  });
  const failed = comparisons.filter((item) => item.status === "failed").length;
  const total = Number(caseData.suspect_count) || comparisons.length;
  const thresholds = caseData.thresholds || comparisons.find((item) => item.result)?.result?.thresholds || {};
  const reportAvailable = Boolean(payload.report?.pdf);
  const status = payload.interrupted ? "interrupted" : (caseData.status || "completed");
  const summaryText = payload.interrupted
    ? "此案件在分析过程中被中断，尚未生成可供复核的完整结果或综合报告。现有上传文件仍保留在本地。"
    : payload.report?.text || "案件结果已生成，暂无综合摘要文本。";
  const comparisonRows = comparisons.length
    ? comparisons.map((item, index) => comparisonRowHtml(item, index)).join("")
    : `<div class="interrupted-card">暂无完整的可疑视频比对结果。</div>`;
  $("#overview-content").innerHTML = `<div class="overview-header">
    <div><div class="overview-title-row"><h1 id="overview-title">${escapeHtml(caseData.name || `案件 ${state.activeCaseId}`)}</h1>${statusBadge(status)}</div>
      <div class="overview-meta"><code>${escapeHtml(caseData.case_id || state.activeCaseId)}</code><span>原始视频：${escapeHtml(caseData.original_filename || "未记录")}</span><span>更新于 ${escapeHtml(formatDate(caseData.updated_at || caseData.created_at))}</span></div>
    </div>
    <div class="overview-actions">
      <button class="btn" data-overview-action="rename" type="button">重命名</button>
      <button class="btn btn-subtle-danger" data-overview-action="delete" type="button">删除</button>
      <button class="btn btn-primary" data-overview-action="report" type="button" ${reportAvailable ? "" : "disabled"}>↓ 下载 PDF 报告</button>
    </div>
  </div>
  <div class="overview-grid">
    <div class="card overview-panel"><h2 class="panel-title">风险分布</h2>
      <div class="distribution-top"><div class="distribution-total"><strong>${total}</strong><span>项比对</span></div>${riskBar(counts, total)}</div>
      <div class="risk-stat-grid"><div class="risk-stat red"><strong>${counts.red}</strong><span>高风险</span></div><div class="risk-stat yellow"><strong>${counts.yellow}</strong><span>中风险</span></div><div class="risk-stat gray"><strong>${counts.gray}</strong><span>低风险</span></div><div class="risk-stat failed"><strong>${failed}</strong><span>分析失败</span></div></div>
    </div>
    <div class="card overview-panel"><h2 class="panel-title">案件配置快照</h2><div class="snapshot-list">
      <div><span>阈值预设</span><b>${escapeHtml(caseData.threshold_preset_label || (caseData.threshold_preset === "custom" ? "自定义" : "未记录"))} · v${Number(caseData.threshold_revision) || 1}</b></div>
      <div><span>画面 / 原声硬阈值</span><b>${settingDisplay(SETTING_FIELDS[0], thresholds.fuse_visual_ratio ?? 0)} / ${settingDisplay(SETTING_FIELDS[1], thresholds.fuse_audio_ratio ?? 0)}</b></div>
      <div><span>连续匹配 / 时长密度</span><b>${settingDisplay(SETTING_FIELDS[2], thresholds.fuse_continuous_seconds ?? 0)} / ${settingDisplay(SETTING_FIELDS[3], thresholds.fuse_density_ratio ?? 0)}</b></div>
      <div><span>黄区罚分阈值</span><b>${settingDisplay(SETTING_FIELDS[4], thresholds.penalty_yellow_min ?? 0)}</b></div>
      <div><span>报告来源</span><b>${payload.report?.source === "deepseek" ? "DeepSeek" : payload.report ? "本地模板" : "未生成"}</b></div>
    </div></div>
  </div>
  <div class="card summary-card"><h2 class="panel-title">综合案情摘要</h2><p>${escapeHtml(summaryText)}</p></div>
  <div class="card comparison-card"><h2>可疑视频比对结果（${total}）</h2><div class="comparison-head"><div>#</div><div>文件名</div><div>风险等级</div><div>判定说明</div><div style="text-align:right">关键证据</div></div>${comparisonRows}</div>
  <p class="legal-footnote">以上为客观技术比对结果，不构成侵权定性；请结合授权与具体使用情境进行法务复核。</p>`;
}

function comparisonRowHtml(item, index) {
  if (item.status === "failed") {
    return `<button class="comparison-row" data-comparison-key="${escapeHtml(item.key)}" type="button"><span class="comparison-num">${String(index + 1).padStart(2, "0")}</span><span class="comparison-file">${escapeHtml(item.filename)}</span>${tierBadge(null)}<span class="comparison-decision">${escapeHtml(item.error || "分析失败，无法给出判定。")}</span><span class="evidence-chips"><i class="evidence-chip">分析失败</i></span></button>`;
  }
  const result = item.result || {};
  const color = result.tier?.color;
  const chips = evidenceChips(result);
  const shortDecision = String(result.decision || result.tier?.conclusion || "结果已生成").split("。")[0] + "。";
  return `<button class="comparison-row" data-comparison-key="${escapeHtml(item.key)}" type="button"><span class="comparison-num">${String(index + 1).padStart(2, "0")}</span><span class="comparison-file" title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</span>${tierBadge(color)}<span class="comparison-decision">${escapeHtml(shortDecision)}</span><span class="evidence-chips">${chips.map((chip) => `<i class="evidence-chip">${escapeHtml(chip)}</i>`).join("")}</span></button>`;
}

function evidenceChips(result) {
  const fuses = (result.fuses || []).map((item) => item.name);
  if (fuses.length) return fuses.slice(0, 2);
  const penalties = (result.round2?.penalties || []).filter((item) => item.triggered).map((item) => item.label.replace(/【.*?】/g, ""));
  if (penalties.length) return penalties.slice(0, 2);
  return ["基础审计日志"];
}

function handleOverviewAction(event) {
  const comparison = event.target.closest("[data-comparison-key]");
  if (comparison) {
    openComparison(comparison.dataset.comparisonKey);
    return;
  }
  const action = event.target.closest("[data-overview-action]")?.dataset.overviewAction;
  if (!action) return;
  const item = state.activeDetail?.case || activeSummary();
  if (action === "rename") openRename(item);
  if (action === "delete") openDelete(item);
  if (action === "report") downloadReport(state.activeCaseId);
}

function openComparison(key) {
  state.activeComparison = state.activeComparisons.find((item) => item.key === key) || null;
  if (!state.activeComparison) return;
  renderDetail();
  setScreen("detail");
}

function renderDetail() {
  const item = state.activeComparison;
  const container = $("#detail-content");
  if (!item) return;
  if (item.status === "failed") {
    container.innerHTML = `<div class="detail-heading"><h1 id="detail-title">${escapeHtml(item.filename)}</h1><p>可疑视频分析结果</p></div><div class="risk-banner failed"><div class="risk-icon">!</div><div class="risk-copy"><span>FAILED</span><h2>分析失败</h2></div></div><div class="card detail-card decision-card"><h2>失败原因</h2><p>${escapeHtml(item.error || "视频分析未能完成。")}</p></div>`;
    return;
  }
  const result = item.result || {};
  const color = result.tier?.color || "gray";
  const tier = TIER[color];
  const hardChecks = hardChecksHtml(result);
  const round2 = result.round2 || {};
  const evidence = result.evidence || {};
  const showTimeline = color === "red" || color === "yellow";
  const gallery = evidenceGalleryHtml(item, evidence);
  const audit = (evidence.audit_log || []).map((line, index) => `<div class="audit-entry"><code>步骤 ${String(index + 1).padStart(2, "0")}</code><span>${escapeHtml(line)}</span></div>`).join("") || `<div class="audit-entry"><code>—</code><span>暂无审计日志。</span></div>`;
  container.innerHTML = `<div class="detail-heading"><h1 id="detail-title">${escapeHtml(item.filename)}</h1><p>所属案件：${escapeHtml(state.activeDetail?.case?.name || state.activeCaseId)}</p></div>
  <div class="risk-banner ${color}"><div class="risk-icon">${tier.icon}</div><div class="risk-copy"><span>${tier.code}</span><h2>${escapeHtml(result.tier?.label || tier.full)}</h2></div></div>
  <div class="card detail-card decision-card"><h2>系统判定</h2><p>${escapeHtml(result.tier?.conclusion || "")}</p><p class="decision-path">${escapeHtml(result.decision || "")}</p></div>
  <div class="card detail-card"><h2>第一轮 · 主轨硬阈值检查</h2><div class="hard-checks">${hardChecks}</div></div>
  ${round2.reached ? penaltiesHtml(round2) : ""}
  <h2 class="evidence-title">证据</h2>
  ${gallery ? `<div class="card detail-card"><h2>对齐截图与遮挡证据</h2><div class="evidence-gallery">${gallery}</div></div>` : ""}
  ${showTimeline ? timelineHtml(evidence.heatmap || {}) : ""}
  <div class="card detail-card"><h2>取证审计日志</h2><div class="audit-list">${audit}</div></div>
  <div class="card" style="margin-top:14px;overflow:hidden"><button class="technical-toggle" data-toggle-technical type="button"><span>技术详情 · 原始指标</span><span>⌄</span></button><div class="technical-body is-hidden" id="technical-body"><div class="metric-grid">${metricsHtml(result)}</div></div></div>
  <div class="recommendation"><div class="recommendation-icon">✦</div><div><strong>建议的法务复核动作</strong><p>${escapeHtml(result.tier?.action || "请结合案件背景进行人工复核。")}</p></div></div>`;
}

function hardChecksHtml(result) {
  const metrics = result.metrics || {};
  const thresholds = result.thresholds || state.activeDetail?.case?.thresholds || {};
  const data = {
    visual: ["画面匹配率", percent(metrics.visual_ratio), `≥ ${percent(thresholds.fuse_visual_ratio, 0)}`],
    audio: ["原声重合率", percent(metrics.audio_ratio), `≥ ${percent(thresholds.fuse_audio_ratio, 0)}`],
    continuous: ["最长连续匹配片段", `${Number(metrics.longest_seconds) || 0}s`, `≥ ${Number(thresholds.fuse_continuous_seconds) || 0}s`],
    density: ["复制时长占原片比例", percent(metrics.density_ratio), `≥ ${percent(thresholds.fuse_density_ratio, 0)}`],
  };
  const checks = result.fuse_checks?.length
    ? result.fuse_checks
    : Object.keys(data).map((key) => ({ key, triggered: (result.fuses || []).some((fuse) => fuse.key === key) }));
  return checks.map((check) => {
    const [label, value, threshold] = data[check.key] || [check.name, check.measured || "—", check.threshold || "—"];
    return `<div class="hard-check ${check.triggered ? "hit" : ""}"><div class="hard-check-top"><span>${escapeHtml(label)}</span><span class="check-tag">${check.triggered ? "已命中" : "未命中"}</span></div><div class="hard-value">${escapeHtml(value)}</div><div class="hard-threshold">阈值 ${escapeHtml(threshold)}</div></div>`;
  }).join("");
}

function penaltiesHtml(round2) {
  const rows = (round2.penalties || []).map((item) => `<div class="penalty-item ${item.triggered ? "hit" : ""}"><span class="penalty-mark">${item.triggered ? "✓" : ""}</span><div class="penalty-copy"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.measured || item.detail || "未触发")}</span></div><span class="penalty-points">${item.triggered ? "+" : ""}${Number(item.points) || 0} 分</span></div>`).join("");
  return `<div class="card detail-card"><div class="penalty-heading"><div><h2>第二轮 · 罚分检查</h2></div><div class="penalty-total">累计<strong>${Number(round2.penalty_total) || 0}</strong>/ 黄区 ${Number(round2.threshold) || 0} 分</div></div><div class="penalty-list">${rows}</div><p class="legal-footnote">罚分为技术加权指标，用于辅助分级，不代表侵权程度的法律结论。</p></div>`;
}

function evidenceGalleryHtml(item, evidence) {
  if (!item.resultId) return "";
  const base = `/api/jobs/${apiPathPart(state.activeCaseId)}/results/${apiPathPart(item.resultId)}/evidence/`;
  const records = [];
  (evidence.pairs || []).forEach((pair) => {
    const flags = [];
    if (pair.mirrored) flags.push("镜像命中");
    if (pair.embedded) flags.push("画中画 / 背景图区域命中");
    if (pair.geometric) flags.push("局部特征几何验证");
    records.push({ image: pair.image, caption: `原始 ${pair.original_time || ""} ↔ 可疑 ${pair.suspect_time || ""}`, flags });
  });
  (evidence.subtitle || []).forEach((record) => records.push({ image: record.image, caption: `疑似二次字幕 @ ${record.suspect_time || ""}`, flags: record.texts || [] }));
  (evidence.watermark || []).forEach((record) => records.push({ image: record.image, caption: `${record.corner || ""}${record.kind || ""} @ ${record.suspect_time || ""}`, flags: ["水印遮挡痕迹"] }));
  return records.map((record) => `<figure class="evidence-figure"><img src="${base}${apiPathPart(record.image)}" alt="${escapeHtml(record.caption)}" loading="lazy"><figcaption>${escapeHtml(record.caption)}${record.flags?.length ? `<span class="flag-row">${record.flags.map((flag) => `<i class="flag">${escapeHtml(flag)}</i>`).join("")}</span>` : ""}</figcaption></figure>`).join("");
}

function timelineHtml(heatmap) {
  const visual = Array.from(heatmap.visual_seconds || heatmap.seconds || [], Boolean);
  const audio = Array.from(heatmap.audio_seconds || [], Boolean);
  const total = Math.max(visual.length, audio.length, Number(heatmap.duration) || 0, 1);
  while (visual.length < total) visual.push(false);
  while (audio.length < total) audio.push(false);
  const visualTrack = timelineSegments(visual, audio, "#2563eb");
  const audioTrack = timelineSegments(audio, visual, "#0d9488");
  return `<div class="card detail-card"><div class="penalty-heading"><h2>画面 / 声音时间轴热力图</h2><div class="timeline-legend"><span><i style="background:#7c3aed"></i>画面+声音</span><span><i style="background:#2563eb"></i>画面</span><span><i style="background:#0d9488"></i>声音</span><span><i style="background:#e2e8f0"></i>无匹配</span></div></div><div class="timeline-row"><span class="timeline-label">画面轨</span><div class="timeline-track">${visualTrack}</div></div><div class="timeline-row"><span class="timeline-label">声音轨</span><div class="timeline-track">${audioTrack}</div></div><div class="timeline-times"><span>00:00</span><span>${formatDuration(heatmap.duration || total)}</span></div></div>`;
}

function timelineSegments(primary, secondary, ownColor) {
  const segments = [];
  let color = null;
  let count = 0;
  primary.forEach((hit, index) => {
    const next = hit && secondary[index] ? "#7c3aed" : hit ? ownColor : "#e2e8f0";
    if (next === color) count += 1;
    else {
      if (count) segments.push([color, count]);
      color = next;
      count = 1;
    }
  });
  if (count) segments.push([color, count]);
  const total = Math.max(1, primary.length);
  return segments.map(([background, length]) => `<span style="width:${length / total * 100}%;background:${background}"></span>`).join("");
}

function metricsHtml(result) {
  const m = result.metrics || {};
  const meta = result.meta || {};
  const values = [
    ["画面一致帧占比", percent(m.visual_ratio)],
    ["原声重合占比", percent(m.audio_ratio)],
    ["最长连续片段", `${Number(m.longest_seconds) || 0}s`],
    ["原片时长密度", percent(m.density_ratio)],
    ["视觉污染秒数", `${Number(m.polluted_seconds) || 0}s`],
    ["原片总时长", formatDuration(m.original_duration || meta.original_duration)],
    ["涉案视频时长", formatDuration(meta.suspect_duration)],
    ["时序正序对率", percent(m.sequential_order_rate)],
    ["离散重合片段", `${Number(m.segment_count) || 0} 段`],
    ["镜像命中帧", String(Number(m.mirrored_frames) || 0)],
    ["画中画命中帧", String(Number(m.embedded_frames) || 0)],
    ["几何验证命中帧", String(Number(m.geometric_frames) || 0)],
    ["原声命中秒数", `${Number(m.audio_matched_seconds ?? meta.audio_matched_seconds) || 0}s`],
    ["AI 指纹模型", m.fingerprint_model || meta.fingerprint_model || "未记录"],
    ["指纹缓存", (m.fingerprint_cache_hit ?? meta.fingerprint_cache_hit) ? "已复用" : "新生成"],
    ["指纹平均相似度", Number.isFinite(Number(m.fingerprint_mean_similarity ?? meta.fingerprint_mean_similarity)) ? Number(m.fingerprint_mean_similarity ?? meta.fingerprint_mean_similarity).toFixed(4) : "—"],
  ];
  return values.map(([label, value]) => `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}

async function loadSettings() {
  try {
    state.settingsPayload = await apiJson("/api/settings");
    state.settingsPreset = state.settingsPayload.settings.preset;
    state.settingsDraft = { ...state.settingsPayload.settings.values };
    renderSettings();
    renderCreateSettings();
  } catch (error) {
    $("#preset-grid").innerHTML = `<div class="notice notice-error">设置读取失败：${escapeHtml(error.message)}</div>`;
  }
}

function openSettings() {
  if (state.settingsPayload) {
    state.settingsPreset = state.settingsPayload.settings.preset;
    state.settingsDraft = { ...state.settingsPayload.settings.values };
  }
  renderSettings();
  setScreen("settings");
}

function renderSettings() {
  const payload = state.settingsPayload;
  if (!payload) return;
  const presets = payload.presets || {};
  const cards = Object.entries(presets).map(([id, preset]) => ({ id, label: preset.label, description: preset.description })).concat([{ id: "custom", label: "自定义", description: "手动设定各项数值" }]);
  $("#preset-grid").innerHTML = cards.map((item) => `<button class="preset-card ${state.settingsPreset === item.id ? "is-active" : ""}" data-preset="${escapeHtml(item.id)}" type="button"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.description)}</span></button>`).join("");
  const locked = state.settingsPreset !== "custom";
  $("#settings-lock-note").textContent = locked ? "选择“自定义”以手动调整" : "正在编辑自定义配置";
  $("#settings-fields").innerHTML = SETTING_FIELDS.map((field) => {
    const value = state.settingsDraft?.[field.key];
    return `<div class="settings-field"><div class="settings-field-copy"><strong>${field.label}</strong><span>${field.hint}</span></div><div class="stepper ${locked ? "is-locked" : ""}"><button data-setting-key="${field.key}" data-delta="-${field.step}" type="button" ${locked ? "disabled" : ""}>−</button><output>${escapeHtml(settingDisplay(field, value))}</output><button data-setting-key="${field.key}" data-delta="${field.step}" type="button" ${locked ? "disabled" : ""}>＋</button></div></div>`;
  }).join("");
  const settings = payload.settings;
  $("#settings-revision").textContent = `当前配置版本 v${Number(settings.revision) || 1}${settings.updated_at ? ` · 更新于 ${formatDate(settings.updated_at)}` : ""}`;
  const save = $("#save-settings");
  save.disabled = state.settingsSaving;
  save.textContent = state.settingsSaving ? "正在保存…" : "保存配置";
}

function handlePresetSelect(event) {
  const button = event.target.closest("[data-preset]");
  if (!button || !state.settingsPayload) return;
  state.settingsPreset = button.dataset.preset;
  if (state.settingsPreset !== "custom") {
    state.settingsDraft = { ...state.settingsPayload.presets[state.settingsPreset].values };
  } else {
    state.settingsDraft = { ...(state.settingsDraft || state.settingsPayload.settings.values) };
  }
  renderSettings();
}

function handleSettingStep(event) {
  const button = event.target.closest("[data-setting-key]");
  if (!button || state.settingsPreset !== "custom") return;
  const key = button.dataset.settingKey;
  const delta = Number(button.dataset.delta);
  const limit = state.settingsPayload?.limits?.[key];
  const current = Number(state.settingsDraft[key]);
  const next = Math.min(Number(limit?.max ?? Infinity), Math.max(Number(limit?.min ?? -Infinity), current + delta));
  state.settingsDraft[key] = Number.isInteger(delta) ? Math.round(next) : Math.round(next * 10000) / 10000;
  renderSettings();
}

async function saveSettings() {
  if (!state.settingsPayload || state.settingsSaving) return;
  state.settingsSaving = true;
  renderSettings();
  try {
    const payload = await apiJson("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset: state.settingsPreset, values: state.settingsPreset === "custom" ? state.settingsDraft : null }),
    });
    state.settingsPayload.settings = payload.settings;
    state.settingsPreset = payload.settings.preset;
    state.settingsDraft = { ...payload.settings.values };
    showToast("阈值配置已保存，仅对之后创建的案件生效。");
    renderCreateSettings();
  } catch (error) {
    showToast(`设置保存失败：${error.message}`);
  } finally {
    state.settingsSaving = false;
    renderSettings();
  }
}

function openRename(item) {
  state.modal = { type: "rename", caseId: item.case_id, name: item.name || "" };
  $("#modal-content").innerHTML = `<form><h2 id="modal-title">重命名案件</h2><p>已完成案件的 PDF 报告将同步更新案件名称。</p><input class="text-input" id="rename-value" maxlength="100" value="${escapeHtml(item.name || "")}" required><div class="modal-actions"><button class="btn" data-close-modal type="button">取消</button><button class="btn btn-primary" type="submit">保存</button></div></form>`;
  $("#modal-layer").classList.remove("is-hidden");
  requestAnimationFrame(() => $("#rename-value")?.focus());
}

function openDelete(item) {
  state.modal = { type: "delete", caseId: item.case_id, name: item.name || item.case_id };
  $("#modal-content").innerHTML = `<form><h2 id="modal-title">永久删除案件</h2><p>此操作不可撤销。案件“${escapeHtml(state.modal.name)}”及其全部上传视频、比对结果、证据与报告将被永久删除。</p><div class="modal-actions"><button class="btn" data-close-modal type="button">取消</button><button class="btn btn-danger" type="submit">永久删除</button></div></form>`;
  $("#modal-layer").classList.remove("is-hidden");
}

function closeModal() {
  state.modal = null;
  $("#modal-layer").classList.add("is-hidden");
  $("#modal-content").innerHTML = "";
}

async function handleModalSubmit(event) {
  event.preventDefault();
  if (!state.modal) return;
  const modal = state.modal;
  const submit = event.target.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    if (modal.type === "rename") {
      const name = $("#rename-value").value.trim();
      if (!name) throw new Error("案件名称不能为空");
      await apiJson(`/api/cases/${apiPathPart(modal.caseId)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
      });
      closeModal();
      await loadCasesSilently();
      if (state.activeCaseId === modal.caseId && state.activeDetail) await loadCaseDetail(modal.caseId, activeSummary());
      else renderLibrary();
      showToast("案件已重命名；已完成案件的 PDF 已同步更新。");
    } else {
      await apiJson(`/api/cases/${apiPathPart(modal.caseId)}`, { method: "DELETE" });
      closeModal();
      if (state.activeCaseId === modal.caseId) goLibrary();
      else {
        state.cases = state.cases.filter((item) => item.case_id !== modal.caseId);
        renderLibrary();
      }
      showToast("案件已永久删除。");
    }
  } catch (error) {
    submit.disabled = false;
    showToast(`${modal.type === "rename" ? "重命名" : "删除"}失败：${error.message}`);
  }
}

function downloadReport(caseId) {
  const anchor = document.createElement("a");
  anchor.href = `/api/cases/${apiPathPart(caseId)}/report`;
  anchor.download = `case-report-${caseId}.pdf`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

document.addEventListener("DOMContentLoaded", initialize);
