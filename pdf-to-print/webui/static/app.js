"use strict";
// Pen plotter web UI. All text from the server is inserted via textContent (no innerHTML).

const TOKEN = document.querySelector('meta[name="api-token"]').content;
const $ = (id) => document.getElementById(id);
const SETTINGS = ["pdf", "holder", "page_order", "pressure", "feedrate", "strikethrough", "z_hop",
  "draw_speed", "draw_accel", "time_factor", "start_page", "l_stop_height", "reference"];
const MAX_LOG = 3000;
let files = { pdfs: [], gcodes: [], others: [] };
let lastGuidance = "";
let lastJob = null;
let resultFile = localStorage.getItem("resultFile") || "";

// ------------------------------------------------------------ helpers
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}

function fileUrl(rel) { return `/files/${rel}?token=${encodeURIComponent(TOKEN)}`; }

function toast(text, bad = false) {
  const t = $("toast");
  t.textContent = text;
  t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), bad ? 9000 : 4000);
}

async function api(path, body, raw) {
  const opts = { headers: { "X-Token": TOKEN } };
  if (raw) { opts.method = "POST"; opts.body = raw.data; opts.headers["X-Filename"] = encodeURIComponent(raw.name); }
  else if (body !== undefined) {
    opts.method = "POST"; opts.body = JSON.stringify(body);
    opts.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
}

async function act(path, body, okText) {
  try {
    const r = await api(path, body);
    if (okText) toast(okText);
    return r;
  } catch (e) { toast(e.message, true); return null; }
}

function fmtSize(b) {
  return b >= 1e6 ? `${(b / 1e6).toFixed(1)} МБ` : `${Math.max(1, Math.round(b / 1024))} КБ`;
}

function fmtMin(m) {
  if (m === null || m === undefined) return "—";
  return m >= 60 ? `${Math.floor(m / 60)} ч ${m % 60} мин` : `${m} мин`;
}

// ------------------------------------------------------------ settings
function settings() {
  const s = {};
  for (const k of SETTINGS) {
    const e = $(k);
    s[k] = e.type === "checkbox" ? e.checked : e.value;
  }
  return s;
}

function restoreSettings() {
  const saved = JSON.parse(localStorage.getItem("settings") || "{}");
  for (const k of SETTINGS) {
    if (!(k in saved)) continue;
    const e = $(k);
    if (e.type === "checkbox") e.checked = saved[k]; else e.value = saved[k];
  }
}

function saveSettings() { localStorage.setItem("settings", JSON.stringify(settings())); }

// ------------------------------------------------------------ files
function fillSelect(sel, values, keepFirst) {
  const prev = sel.value;
  const first = keepFirst ? sel.options[0] : null;
  sel.replaceChildren(...(first ? [first] : []), ...values.map((v) => el("option", { value: v }, v)));
  if (values.includes(prev) || (keepFirst && prev === "")) sel.value = prev;
}

function renderFiles(f) {
  files = f;
  fillSelect($("pdf"), f.pdfs);
  const names = f.gcodes.map((g) => g.name);
  fillSelect($("reference"), names, true);
  fillSelect($("send-file"), names);
  const sendSel = $("send-file");
  const best = f.gcodes.find((g) => g.status === "PASS" && !g.name.startsWith("test_"));
  if (best && (!sendSel.dataset.touched || !names.includes(sendSel.value))) sendSel.value = best.name;
  const saved = JSON.parse(localStorage.getItem("settings") || "{}");
  if (saved.pdf && f.pdfs.includes(saved.pdf)) $("pdf").value = saved.pdf;
  if ($("motion-file")) {
    fillSelect($("motion-file"), names);
    if (motionFile && names.includes(motionFile)) $("motion-file").value = motionFile;
    else if (best) $("motion-file").value = best.name;
  }
  renderLstop(f);
  const tbody = $("gcodes").querySelector("tbody");
  tbody.replaceChildren(...f.gcodes.map(gcodeRow));
  $("others").replaceChildren(...f.others.map((n) =>
    el("li", {}, el("a", { href: fileUrl(`output/${n}`), target: "_blank" }, n))));
  if (resultFile) showResult(resultFile);
}

function gcodeRow(g) {
  const tag = el("span", { class: `tag ${g.status}` }, g.status);
  const stale = g.status !== "нет проверки" && !g.fresh ? el("span", { class: "tag stale" }, "устарела") : "";
  const btns = el("div", { class: "btns" },
    el("button", { class: "small", onclick: () => showReport(g.name) }, "Отчёт"),
    el("button", { class: "small", onclick: () => simulate(g.name) }, "Проверить на модели"),
    el("button", { class: "small", onclick: () => showResult(g.name, true) }, "Превью"),
    el("button", { class: "small", onclick: () => openMotion(g.name) }, "Движение"),
    el("a", { href: fileUrl(`output/${g.name}`) }, el("button", { class: "small" }, "Скачать")),
    el("button", { class: "small primary", onclick: () => toPrinter(g.name) }, "На принтер"));
  return el("tr", {}, el("td", {}, g.name), el("td", {}, fmtMin(g.est_min)),
    el("td", {}, tag, " ", stale), el("td", {}, fmtSize(g.size)), el("td", {}, btns));
}

async function showReport(name) {
  const r = await act(`/api/report?file=${encodeURIComponent(name)}`);
  if (!r) return;
  $("report-card").classList.remove("hidden");
  $("report-name").textContent = name;
  $("report").textContent = r.text || "Отчёта нет — нажмите «Проверить на модели».";
  $("report-card").scrollIntoView({ behavior: "smooth" });
}

function simulate(name) {
  const s = settings();
  act("/api/simulate", { file: name, holder: s.holder, l_stop_height: s.l_stop_height,
    reference: s.reference }, `Проверка ${name} запущена`);
}

function toPrinter(name) {
  $("send-file").value = name;
  $("send-file").dataset.touched = "1";
  switchTab("printer");
}

async function showResult(name, jump) {
  resultFile = name;
  localStorage.setItem("resultFile", name);
  $("result-file").textContent = name;
  const g = files.gcodes.find((x) => x.name === name);
  const [rep, prev] = await Promise.all([
    api(`/api/report?file=${encodeURIComponent(name)}`).catch(() => ({ text: "" })),
    api(`/api/previews?file=${encodeURIComponent(name)}`).catch(() => ({ images: [] }))]);
  const head = (rep.text || "").split("\n").filter((l) => l.startsWith("#") || l.startsWith("- ")).slice(0, 8);
  $("result-summary").replaceChildren(
    el("div", {}, g ? `Оценка: ${fmtMin(g.est_min)} · ${fmtSize(g.size)}` : ""),
    el("pre", {}, head.join("\n") || "Проверки на модели ещё не было."));
  $("previews").replaceChildren(...prev.images.map((src) => {
    const url = fileUrl(src);
    return el("figure", {}, el("a", { href: url, target: "_blank" }, el("img", { src: url, loading: "lazy" })),
      el("figcaption", {}, src.split("/").pop().replace(".png", "")));
  }));
  if (jump) switchTab("build");
}

// ------------------------------------------------------------ motion preview
let motionFile = "";
let pendingLstopDownload = false;

function fmtSec(sec) {
  sec = Math.round(sec || 0);
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

async function loadMotionFile(name) {
  const sel = $("motion-page");
  if (!name || !sel || !window.MotionViewer) return;
  motionFile = name;
  if ($("motion-file")) $("motion-file").value = name;
  sel.replaceChildren(el("option", { value: "" }, "загрузка…"));
  try {
    const ov = await api(`/api/motion?file=${encodeURIComponent(name)}`);
    sel.replaceChildren(...ov.pages.map((p) =>
      el("option", { value: String(p.index) }, `${p.index}. стр. ${String(p.page).padStart(2, "0")} · ${fmtSec(p.seconds)}`)));
    await loadMotionPage(1);
  } catch (e) { sel.replaceChildren(); toast(e.message, true); }
}

async function loadMotionPage(index) {
  if (!motionFile) return;
  try {
    const d = await api(`/api/motion?file=${encodeURIComponent(motionFile)}&page=${index}`);
    if ($("motion-page")) $("motion-page").value = String(index);
    MotionViewer.load(d);
  } catch (e) { toast(e.message, true); }
}

function openMotion(name) {
  switchTab("motion");
  loadMotionFile(name);
}

function renderLstop(f) {
  const a = $("lstop-download");
  const has = f.others.includes("l_stop.stl");
  if (a) {
    a.href = has ? fileUrl("output/l_stop.stl") : "#";
    a.setAttribute("download", "l_stop.stl");
  }
  const img = $("lstop-preview");
  if (img && f.others.includes("l_stop_preview.png")) {
    img.src = fileUrl("output/l_stop_preview.png") + `&t=${Date.now()}`;
    img.classList.remove("hidden");
  }
  if (has && pendingLstopDownload && a) { pendingLstopDownload = false; window.location.href = a.href; }
}

// ------------------------------------------------------------ jobs / log
function appendLog(line) {
  const log = $("log");
  log.textContent += line + "\n";
  if (log.textContent.length > MAX_LOG * 120) log.textContent = log.textContent.slice(-MAX_LOG * 80);
  log.scrollTop = log.scrollHeight;
}

function renderJob(job) {
  const chip = $("chip-job");
  if (!job) { chip.textContent = "Свободно"; chip.className = "chip"; return; }
  const names = { running: "⏳ ", ok: "✓ ", failed: "✗ ", cancelled: "⨯ " };
  chip.textContent = names[job.state] + job.title;
  chip.className = "chip " + ({ running: "run", ok: "ok", failed: "bad", cancelled: "warn" }[job.state] || "");
  $("log-title").textContent = job.title;
  $("cancel").classList.toggle("hidden", job.state !== "running" || job.kind === "send");
  if (job.state === "running" && (!lastJob || lastJob.id !== job.id)) {
    const f = document.querySelector("footer");
    if (f?.classList.contains("collapsed")) { f.classList.remove("collapsed"); $("log-toggle").textContent = "Свернуть"; }
  }
  if (lastJob && lastJob.id === job.id && lastJob.state === "running" && job.state !== "running") {
    toast(`${job.title}: ${job.state === "ok" ? "готово" : "ошибка — см. лог"}`, job.state !== "ok");
    if (job.state === "ok" && job.kind === "build") showResult(job.title.split("→ ").pop(), true);
  }
  lastJob = job;
}

// ------------------------------------------------------------ printer
function renderPrinter(p) {
  const chip = $("chip-printer");
  const label = p.online ? (p.state || "на связи") + (p.state === "RUNNING" && p.percent != null ? ` ${p.percent}%` : "")
    : (p.configured ? "Принтер не на связи" : "Принтер не подключён");
  chip.textContent = label;
  chip.className = "chip " + (!p.online ? "" : p.state === "PAUSE" ? "warn" : p.state === "RUNNING" ? "run"
    : p.state === "FAILED" ? "bad" : "ok");
  $("p-state").textContent = p.online ? `${p.state || "—"}${p.stage ? " · " + p.stage : ""}` : "не подключён";
  $("p-bar").style.width = `${p.percent || 0}%`;
  const rows = [["Задание", p.job || "—"], ["Прогресс", p.percent != null ? `${p.percent}%` : "—"],
    ["Осталось", fmtMin(p.remaining_min)],
    ["Страница", p.total_layers ? `${p.layer || 0} из ${p.total_layers}` : "—"],
    ["Сопло / стол", `${p.nozzle ?? "—"}° / ${p.bed ?? "—"}°`],
    ["Developer Mode", p.developer_mode === true ? "включён" : p.developer_mode === false ? "выключен" : "?"],
    ["Отправлено отсюда", p.sent_job || "—"]];
  $("p-details").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
  $("p-devmode").classList.toggle("hidden", p.developer_mode !== false);
  $("p-error").classList.toggle("hidden", !p.error);
  $("p-error").textContent = p.error || "";
  renderPrinterControls(p);
  const g = p.guidance || "";
  $("banner").classList.toggle("hidden", !g);
  $("banner-text").textContent = g;
  if (g && g !== lastGuidance) notify(g);
  lastGuidance = g;
}

// The printer accepts a new job only from these states (PrinterLink.send_job); buttons follow them.
const SEND_STATES = ["IDLE", "FINISH", "FAILED"];
const ACTIVE_STATES = ["RUNNING", "PAUSE", "PREPARE", "SLICING"];

function renderPrinterControls(p) {
  const state = p.online ? p.state : null;
  const allowed = { pause: state === "RUNNING", resume: state === "PAUSE", stop: ACTIVE_STATES.includes(state) };
  document.querySelectorAll("[data-cmd]").forEach((b) => { b.disabled = !allowed[b.dataset.cmd]; });
  $("camera-btn").disabled = !p.configured;
  const canSend = SEND_STATES.includes(state);
  $("send").disabled = !canSend;
  $("send").title = canSend ? "" : p.online ? `Принтер занят (${state || "состояние неизвестно"})` : "Сначала подключитесь к принтеру";
}

function beep() {
  try {
    const ctx = new AudioContext();
    [523, 784].forEach((f, i) => {
      const o = ctx.createOscillator(); const gn = ctx.createGain();
      o.frequency.value = f; o.connect(gn); gn.connect(ctx.destination);
      gn.gain.value = 0.15; o.start(ctx.currentTime + i * 0.25); o.stop(ctx.currentTime + i * 0.25 + 0.2);
    });
  } catch (e) { /* audio may be blocked until the first click */ }
}

function notify(text) {
  beep();
  if ("Notification" in window && Notification.permission === "granted") new Notification("Pen plotter", { body: text });
}

function command(name) {
  if (name === "stop" && !confirm("Остановить печать? Задание нельзя будет продолжить.")) return;
  act("/api/printer/command", { name }, `Команда «${name}» отправлена`);
}

async function send() {
  const file = $("send-file").value;
  if (!file) return toast("Нет файла для отправки", true);
  const method = $("send-method").value;
  const force = $("send-force").checked;
  const msg = method === "upload" ? `Загрузить ${file} на принтер без запуска?`
    : `Запустить ${file} на принтере?\n\nДержатель должен быть снят: первая пауза — установка.`;
  if (!confirm(msg)) return;
  await act("/api/printer/send", { file, method, force, holder: $("holder").value }, "Отправка запущена — см. лог");
}

async function camera() {
  const img = $("camera");
  img.classList.remove("hidden");
  img.src = `/api/printer/camera?token=${encodeURIComponent(TOKEN)}&t=${Date.now()}`;
  img.onerror = () => { img.classList.add("hidden"); toast("Камера недоступна (нужен LAN-режим; в демо камеры нет)", true); };
}

// ------------------------------------------------------------ tabs & wiring
const TABS = ["build", "files", "motion", "calib", "printer"];

function switchTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  localStorage.setItem("tab", name);
  if (location.hash.slice(1) !== name) history.replaceState(null, "", `#${name}`);
  if (name === "motion" && !motionFile && $("motion-file")?.value) loadMotionFile($("motion-file").value);
}

function wire() {
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  window.addEventListener("hashchange", () => {
    const tab = location.hash.slice(1);
    if (TABS.includes(tab)) switchTab(tab);
  });
  SETTINGS.forEach((k) => $(k).addEventListener("change", saveSettings));
  $("pdf-upload").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    try {
      const r = await api("/api/pdf", undefined, { name: f.name, data: await f.arrayBuffer() });
      toast(`Загружен ${r.pdf}`);
      $("pdf").value = r.pdf; saveSettings();
    } catch (err) { toast(err.message, true); }
    e.target.value = "";
  });
  $("build").addEventListener("click", async () => {
    saveSettings();
    let r = null;
    try {
      r = await api("/api/build", settings());
    } catch (e) {
      if (e.status !== 409) return toast(e.message, true);
      if (!confirm(`${e.message}\n\nСтарый файл будет заменён (например, уже напечатанное задание).`)) return;
      r = await act("/api/build", { ...settings(), overwrite: true });
    }
    if (r) { toast("Сборка запущена"); $("build-out").textContent = `→ output/${r.output}`; }
  });
  document.querySelectorAll("[data-sheet]").forEach((b) => b.addEventListener("click", () =>
    act("/api/sheet", { ...settings(), kind: b.dataset.sheet, page: $("sheet-page").value },
      `Лист «${b.dataset.sheet}» собирается`)));
  $("motion-file")?.addEventListener("change", (e) => loadMotionFile(e.target.value));
  $("motion-page")?.addEventListener("change", (e) => loadMotionPage(Number(e.target.value) || 1));
  $("lstop-download")?.addEventListener("click", async (e) => {
    if (files.others.includes("l_stop.stl")) return;                 // real link: browser downloads
    e.preventDefault();
    pendingLstopDownload = true;
    await act("/api/lstop", { height: $("ls-height").value, magnet_d: $("ls-md").value,
      magnet_h: $("ls-mh").value }, "Собираю STL упора — скачается автоматически");
  });
  $("lstop").addEventListener("click", async () => {
    await act("/api/lstop", { height: $("ls-height").value, magnet_d: $("ls-md").value,
      magnet_h: $("ls-mh").value }, "STL собирается");
    $("lstop-links").replaceChildren(
      el("a", { href: fileUrl("output/l_stop.stl") }, "l_stop.stl"), " ",
      el("a", { href: fileUrl("output/l_stop_preview.png"), target: "_blank" }, "схема размещения"));
  });
  document.querySelectorAll("[data-cmd]").forEach((b) => b.addEventListener("click", () => command(b.dataset.cmd)));
  $("banner-resume").addEventListener("click", () => command("resume"));
  $("banner-stop").addEventListener("click", () => command("stop"));
  $("send").addEventListener("click", send);
  $("send-file").addEventListener("change", (e) => { e.target.dataset.touched = "1"; });
  $("camera-btn").addEventListener("click", camera);
  $("cancel").addEventListener("click", () => act("/api/job/cancel", {}, "Отмена отправлена"));
  $("log-toggle").addEventListener("click", () => {
    const f = document.querySelector("footer");
    f.classList.toggle("collapsed");
    $("log-toggle").textContent = f.classList.contains("collapsed") ? "Развернуть" : "Свернуть";
  });
  $("p-config").addEventListener("submit", (e) => e.preventDefault());
  $("p-save").addEventListener("click", async () => {
    const r = await act("/api/printer/config", { ip: $("p-ip").value, serial: $("p-serial").value,
      access_code: $("p-code").value }, "Настройки принтера сохранены");
    if (r) { $("p-code").value = ""; $("p-code").placeholder = "сохранён"; }
  });
  $("p-connect").addEventListener("click", () => act("/api/printer/connect", {}, "Подключено"));
  $("p-disconnect").addEventListener("click", () => act("/api/printer/disconnect", {}));
  $("notify-btn").addEventListener("click", async () => {
    if (!("Notification" in window)) return toast("Браузер не поддерживает уведомления", true);
    const p = await Notification.requestPermission();
    toast(p === "granted" ? "Уведомления включены" : "Уведомления запрещены в браузере", p !== "granted");
  });
}

function connectEvents() {
  const es = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}`);
  es.addEventListener("log", (e) => appendLog(JSON.parse(e.data).line));
  es.addEventListener("job", (e) => renderJob(JSON.parse(e.data)));
  es.addEventListener("printer", (e) => renderPrinter(JSON.parse(e.data)));
  es.addEventListener("files", (e) => renderFiles(JSON.parse(e.data)));
  es.addEventListener("reset", () => refreshState());
  es.onerror = () => { $("chip-job").textContent = "Нет связи с сервером"; $("chip-job").className = "chip bad"; };
  es.onopen = () => refreshState();
}

async function refreshState() {
  try {
    const st = await api("/api/state");
    renderFiles(st.files);
    renderPrinter(st.printer);
    renderJob(st.runner.job);
  } catch (e) { toast(e.message, true); }
}

async function init() {
  if (window.MotionViewer) MotionViewer.mount();
  wire();
  restoreSettings();
  const hashTab = location.hash.slice(1);
  switchTab(TABS.includes(hashTab) ? hashTab : localStorage.getItem("tab") || "build");
  const st = await api("/api/state");
  renderFiles(st.files);
  restoreSettings();
  if ($("tab-motion")?.classList.contains("active") && $("motion-file")?.value) loadMotionFile($("motion-file").value);
  $("chip-demo").classList.toggle("hidden", !st.demo);
  $("demo-note").classList.toggle("hidden", !st.demo);
  $("p-config").classList.toggle("hidden", st.demo);
  $("p-ip").value = st.printer_config.ip;
  $("p-serial").value = st.printer_config.serial;
  if (st.printer_config.has_code) $("p-code").placeholder = "сохранён";
  renderPrinter(st.printer);
  if (st.runner.job) renderJob(st.runner.job);
  st.runner.log.forEach(appendLog);
  connectEvents();
}

init().catch((e) => toast(e.message, true));
