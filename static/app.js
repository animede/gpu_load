"use strict";

const MAX_POINTS = 60;
const sessionStarted = Date.now();
const historyByGpu = new Map();

const $ = (id) => document.getElementById(id);
const els = {
  dashboard: $("dashboard"), empty: $("emptyState"), connection: $("connection"),
  host: $("hostName"), picker: $("devicePicker"), secondaryPicker: $("secondaryDevicePicker"),
  secondaryPickerWrap: $("secondaryPickerWrap"), primaryPickerLabel: $("primaryPickerLabel"),
  viewMode: $("viewMode"), singleOverview: $("singleOverview"), dualOverview: $("dualOverview"),
  deviceDetails: $("deviceDetails"), rate: $("ratePicker"), pause: $("pauseButton"),
  singleChartCard: $("singleChartCard"), dualCharts: $("dualCharts"),
  chart: $("historyChart"), dualChartA: $("dualHistoryChartA"), dualChartB: $("dualHistoryChartB"),
  singleProcessList: $("singleProcessList"), dualProcessListA: $("dualProcessListA"),
  dualProcessListB: $("dualProcessListB"),
};

let snapshot = null;
let selectedGpuId = null;
let secondaryGpuId = null;
let viewMode = "single";
let viewModeInitialized = false;
let timer = null;
let intervalMs = 1000;
let paused = false;
let fetching = false;

function finite(value) { return typeof value === "number" && Number.isFinite(value); }
function value(value, suffix = "") { return finite(value) ? `${Math.round(value)}${suffix}` : "—"; }
function clamp(value, min = 0, max = 100) { return Math.min(max, Math.max(min, value)); }
function setText(id, content) { $(id).textContent = content; }
function setBar(id, amount) { $(id).style.width = finite(amount) ? `${clamp(amount)}%` : "0%"; }

function formatMemory(mib) {
  if (!finite(mib)) return "—";
  if (mib >= 1024) return `${(mib / 1024).toFixed(mib > 10240 ? 1 : 2)} GiB`;
  return `${Math.round(mib)} MiB`;
}

function makeElement(tag, className, content = "") {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = content;
  return element;
}

function renderProcesses(gpu, listElement, countId) {
  const processes = Array.isArray(gpu.processes) ? gpu.processes : [];
  setText(countId, processes.length ? `${processes.length} ACTIVE` : "NONE");
  if (!gpu.processesSupported) {
    const empty = makeElement("p", "process-empty", "PROCESS DATA IS NOT AVAILABLE FOR THIS GPU");
    listElement.replaceChildren(empty);
    return;
  }
  if (processes.length === 0) {
    const empty = makeElement("p", "process-empty", "NO MAJOR COMPUTE PROCESS DETECTED");
    listElement.replaceChildren(empty);
    return;
  }
  const rows = processes.map((process, index) => {
    const row = makeElement("div", "process-row");
    const rank = makeElement("span", "process-rank", String(index + 1).padStart(2, "0"));
    const identity = makeElement("div", "process-identity");
    const name = makeElement("strong", "", process.name || "UNKNOWN");
    const pid = makeElement("span", "", `PID ${process.pid ?? "—"}`);
    identity.replaceChildren(name, pid);
    const memory = makeElement("span", "process-memory", formatMemory(process.memoryUsedMiB));
    row.replaceChildren(rank, identity, memory);
    return row;
  });
  listElement.replaceChildren(...rows);
}

function updatePicker(gpus) {
  const ids = gpus.map((gpu) => gpu.id);
  if (!selectedGpuId || !ids.includes(selectedGpuId)) selectedGpuId = ids[0] || null;
  if (!secondaryGpuId || !ids.includes(secondaryGpuId) || secondaryGpuId === selectedGpuId) {
    secondaryGpuId = ids.find((id) => id !== selectedGpuId) || null;
  }
  const dualOption = els.viewMode.querySelector('option[value="dual"]');
  dualOption.disabled = gpus.length < 2;
  if (!viewModeInitialized && gpus.length > 0) {
    viewModeInitialized = true;
    setViewMode(gpus.length >= 2 ? "dual" : "single");
  } else if (gpus.length < 2 && viewMode === "dual") {
    setViewMode("single");
  }
  const signature = gpus.map((gpu) => `${gpu.id}:${gpu.name}`).join("|");
  if (els.picker.dataset.signature === signature) return;
  const makeOptions = (selectedId) => gpus.map((gpu) => {
    const option = document.createElement("option");
    option.value = gpu.id;
    option.textContent = `GPU ${gpu.index ?? "?"} / ${gpu.name}`;
    option.selected = gpu.id === selectedId;
    return option;
  });
  els.picker.replaceChildren(...makeOptions(selectedGpuId));
  els.secondaryPicker.replaceChildren(...makeOptions(secondaryGpuId));
  els.picker.dataset.signature = signature;
}

function setViewMode(mode) {
  viewMode = mode === "dual" && snapshot?.gpus?.length >= 2 ? "dual" : "single";
  els.viewMode.value = viewMode;
  const dual = viewMode === "dual";
  els.singleOverview.hidden = dual;
  els.dualOverview.hidden = !dual;
  els.secondaryPickerWrap.hidden = !dual;
  els.deviceDetails.hidden = dual;
  els.singleChartCard.hidden = dual;
  els.dualCharts.hidden = !dual;
  els.primaryPickerLabel.textContent = dual ? "GPU A" : "ACTIVE DEVICE";
  renderSelected();
}

function stateFor(load) {
  if (!finite(load)) return "NO UTILIZATION READING";
  if (load >= 90) return "SATURATED / MAXIMUM LOAD";
  if (load >= 65) return "HIGH COMPUTE ACTIVITY";
  if (load >= 25) return "ACTIVE WORKLOAD";
  if (load > 2) return "LIGHT WORKLOAD";
  return "IDLE / READY";
}

function thermalState(temp) {
  if (!finite(temp)) return "NO THERMAL READING";
  if (temp >= 85) return "CRITICAL TEMPERATURE";
  if (temp >= 75) return "HIGH TEMPERATURE";
  if (temp >= 55) return "OPERATING RANGE";
  return "COOL / NOMINAL";
}

function appendHistory(gpu) {
  if (!historyByGpu.has(gpu.id)) historyByGpu.set(gpu.id, []);
  const entries = historyByGpu.get(gpu.id);
  entries.push({
    time: snapshot.timestamp,
    load: finite(gpu.utilization) ? gpu.utilization : null,
    memory: finite(gpu.memoryPercent) ? gpu.memoryPercent : null,
    temp: finite(gpu.temperatureC) ? gpu.temperatureC : null,
    power: finite(gpu.powerW) && finite(gpu.powerLimitW) && gpu.powerLimitW > 0
      ? clamp(gpu.powerW / gpu.powerLimitW * 100) : null,
  });
  while (entries.length > MAX_POINTS) entries.shift();
}

function renderGpu(gpu) {
  setText("gpuLoad", value(gpu.utilization));
  setText("gpuState", stateFor(gpu.utilization));
  setBar("gpuLoadBar", gpu.utilization);

  setText("memoryLoad", value(gpu.memoryPercent));
  setText("memoryUsed", formatMemory(gpu.memoryUsedMiB));
  setText("memoryTotal", formatMemory(gpu.memoryTotalMiB));
  setBar("memoryLoadBar", gpu.memoryPercent);

  setText("temperature", value(gpu.temperatureC));
  setText("thermalState", thermalState(gpu.temperatureC));
  setBar("temperatureBar", finite(gpu.temperatureC) ? gpu.temperatureC : 0);

  const powerPercent = finite(gpu.powerW) && finite(gpu.powerLimitW) && gpu.powerLimitW > 0
    ? gpu.powerW / gpu.powerLimitW * 100 : null;
  setText("power", value(gpu.powerW));
  setText("powerPercent", finite(powerPercent) ? `${Math.round(powerPercent)}%` : "—");
  setBar("powerBar", powerPercent);

  setText("gpuName", gpu.name || "UNKNOWN GPU");
  setText("vendorTag", gpu.vendor || "UNKNOWN");
  setText("backendTag", gpu.backend || "UNKNOWN");
  setText("gpuIndex", `GPU ${gpu.index ?? "?"}`);
  setText("clock", finite(gpu.clockMHz) ? `${Math.round(gpu.clockMHz)} MHz` : "—");
  setText("fan", finite(gpu.fanPercent) ? `${Math.round(gpu.fanPercent)}%` : finite(gpu.fanRpm) ? `${Math.round(gpu.fanRpm)} RPM` : "—");
  setText("memoryEngine", finite(gpu.memoryUtilization) ? `${Math.round(gpu.memoryUtilization)}% BUSY` : "—");
  setText("lastSample", new Date(snapshot.timestamp).toLocaleTimeString("ja-JP", { hour12: false }));
  renderProcesses(gpu, els.singleProcessList, "singleProcessCount");
  drawCharts();
}

function renderDualGpu(gpu, suffix) {
  setText(`dualName${suffix}`, gpu.name || "UNKNOWN GPU");
  setText(`dualIndex${suffix}`, `GPU ${gpu.index ?? "?"}`);
  setText(`dualLoad${suffix}`, value(gpu.utilization));
  setText(`dualState${suffix}`, stateFor(gpu.utilization));
  setBar(`dualLoadBar${suffix}`, gpu.utilization);
  setText(`dualMemory${suffix}`, finite(gpu.memoryPercent) ? `${Math.round(gpu.memoryPercent)}%` : "—");
  setText(`dualTemp${suffix}`, finite(gpu.temperatureC) ? `${Math.round(gpu.temperatureC)}°C` : "—");
  setText(`dualPower${suffix}`, finite(gpu.powerW) ? `${Math.round(gpu.powerW)} W` : "—");
  renderProcesses(gpu, els[`dualProcessList${suffix}`], `dualProcessCount${suffix}`);
}

function renderSelected() {
  const gpus = snapshot?.gpus || [];
  const primary = gpus.find((item) => item.id === selectedGpuId);
  if (!primary) return;
  if (viewMode === "dual") {
    const secondary = gpus.find((item) => item.id === secondaryGpuId);
    if (!secondary) return;
    renderDualGpu(primary, "A");
    renderDualGpu(secondary, "B");
    setText("dualChartNameA", primary.name || "UNKNOWN GPU");
    setText("dualChartNameB", secondary.name || "UNKNOWN GPU");
    drawCharts();
  } else {
    renderGpu(primary);
  }
}

function setConnected(isConnected, label = null) {
  els.connection.classList.toggle("offline", !isConnected);
  els.connection.lastChild.textContent = ` ${label || (isConnected ? "LIVE" : "OFFLINE")}`;
}

async function fetchMetrics() {
  if (paused || fetching) return;
  fetching = true;
  try {
    const response = await fetch("/api/gpu", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    snapshot = await response.json();
    const gpus = Array.isArray(snapshot.gpus) ? snapshot.gpus : [];
    setConnected(true, snapshot.demo ? "DEMO" : "LIVE");
    els.host.textContent = (snapshot.hostname || "LOCALHOST").toUpperCase();
    updatePicker(gpus);
    gpus.forEach(appendHistory);
    els.empty.hidden = gpus.length > 0;
    els.dashboard.hidden = gpus.length === 0;
    renderSelected();
  } catch (error) {
    console.error("GPU metrics request failed", error);
    setConnected(false);
  } finally {
    fetching = false;
  }
}

function resizeCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width * ratio));
  const height = Math.max(1, Math.round(rect.height * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width: rect.width, height: rect.height };
}

function drawSeries(ctx, points, key, width, height, color) {
  ctx.beginPath();
  let started = false;
  points.forEach((point, index) => {
    const metric = point[key];
    if (!finite(metric)) { started = false; return; }
    const x = points.length === 1 ? width : index / (MAX_POINTS - 1) * width;
    const y = height - clamp(metric) / 100 * height;
    if (!started) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    started = true;
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = key === "load" ? 2.4 : 1.5;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();
}

function drawChartCanvas(canvas, gpuId) {
  const { ctx, width, height } = resizeCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  ctx.save();
  ctx.setLineDash([2, 6]);
  ctx.strokeStyle = "#282d2e";
  ctx.lineWidth = 1;
  for (let row = 0; row <= 4; row += 1) {
    const y = row / 4 * height;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }
  for (let column = 0; column <= 6; column += 1) {
    const x = column / 6 * width;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke();
  }
  ctx.restore();
  const points = historyByGpu.get(gpuId) || [];
  drawSeries(ctx, points, "power", width, height, "#b48cff");
  drawSeries(ctx, points, "temp", width, height, "#ff9f68");
  drawSeries(ctx, points, "memory", width, height, "#6be4dc");
  drawSeries(ctx, points, "load", width, height, "#c8ff34");
}

function drawCharts() {
  if (viewMode === "dual") {
    drawChartCanvas(els.dualChartA, selectedGpuId);
    drawChartCanvas(els.dualChartB, secondaryGpuId);
  } else {
    drawChartCanvas(els.chart, selectedGpuId);
  }
  const label = `−${Math.round(MAX_POINTS * intervalMs / 1000)} SEC`;
  setText("windowLabel", label);
  document.querySelectorAll(".window-label").forEach((element) => { element.textContent = label; });
}

function schedule() {
  clearInterval(timer);
  timer = setInterval(fetchMetrics, intervalMs);
  setText("updateRateLabel", `${(intervalMs / 1000).toFixed(1)} SEC`);
  drawCharts();
}

els.picker.addEventListener("change", () => {
  const previousPrimaryId = selectedGpuId;
  selectedGpuId = els.picker.value;
  if (secondaryGpuId === selectedGpuId) {
    secondaryGpuId = previousPrimaryId && previousPrimaryId !== selectedGpuId
      ? previousPrimaryId
      : snapshot?.gpus?.find((item) => item.id !== selectedGpuId)?.id || null;
    els.secondaryPicker.value = secondaryGpuId || "";
  }
  renderSelected();
});

els.secondaryPicker.addEventListener("change", () => {
  const previousSecondaryId = secondaryGpuId;
  secondaryGpuId = els.secondaryPicker.value;
  if (secondaryGpuId === selectedGpuId) {
    selectedGpuId = previousSecondaryId && previousSecondaryId !== secondaryGpuId
      ? previousSecondaryId
      : snapshot?.gpus?.find((item) => item.id !== secondaryGpuId)?.id || null;
    els.picker.value = selectedGpuId || "";
  }
  renderSelected();
});

els.viewMode.addEventListener("change", () => setViewMode(els.viewMode.value));

els.rate.addEventListener("change", () => {
  intervalMs = Number(els.rate.value);
  schedule();
});

els.pause.addEventListener("click", () => {
  paused = !paused;
  els.pause.classList.toggle("paused", paused);
  els.pause.querySelector("span").textContent = paused ? "RESUME" : "PAUSE";
  setConnected(!paused, paused ? "PAUSED" : "LIVE");
  if (!paused) fetchMetrics();
});

window.addEventListener("resize", drawCharts);
setInterval(() => {
  const elapsed = Math.floor((Date.now() - sessionStarted) / 1000);
  const hours = String(Math.floor(elapsed / 3600)).padStart(2, "0");
  const minutes = String(Math.floor(elapsed % 3600 / 60)).padStart(2, "0");
  const seconds = String(elapsed % 60).padStart(2, "0");
  setText("sessionTime", `${hours}:${minutes}:${seconds}`);
}, 1000);

fetchMetrics();
schedule();
