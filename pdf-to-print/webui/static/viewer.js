"use strict";
// Motion preview: one page of a job replayed on a to-scale Bambu P1S bed.
// Data comes from /api/motion?file=&page= (pen frame = bed millimetres, times from the planner
// model). Layers: plate (procedural textured PEI), notebook sheet, L-stop jig, ghost of the whole
// page, ink drawn so far, travel moves, then the live overlay (pen, holder footprint, nozzle, Z).

const MotionViewer = (() => {
  // mm: bed + nozzle overshoot, rulers on the left/front, a strip on the right for the Z gauge.
  // .bed in style.css keeps the same aspect ratio (346 / 294).
  const VIEW = { x0: -30, y0: -28, x1: 316, y1: 266 };
  const BED_RIGHT_MM = 272;
  const COLORS = {
    ink: "#13203f", ghost: "rgba(19,32,63,0.13)", travel: "rgba(220,38,38,0.45)",
    holder: "rgba(234,88,12,0.16)", holderLine: "rgba(234,88,12,0.9)", pen: "#2563eb",
    jig: "rgba(37,99,235,0.30)", jigLine: "rgba(29,78,216,0.95)", grid: "rgba(96,140,200,0.28)",
    issue: "#dc2626", pause: "#d97706", stripDraw: "#3b82f6", stripTravel: "#f43f5e",
    stripZ: "#f59e0b",
  };
  const el = {};
  const st = {
    data: null, t: 0, playing: false, speed: 60, last: 0, zoom: 1, panX: 0, panY: 0,
    showTravel: true, showHolder: true, showGhost: true, follow: false,
    inkIdx: 0, travelIdx: 0, viewKey: "", drag: null,
  };
  let base, ink, noise;

  // ---------------------------------------------------------------- geometry helpers
  function layout() {
    const r = el.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(200, Math.round(r.width * dpr));
    const h = Math.max(200, Math.round(r.height * dpr));
    for (const c of [el.canvas, base, ink]) {
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    }
    const fit = Math.min(w / (VIEW.x1 - VIEW.x0), h / (VIEW.y1 - VIEW.y0));
    const s = fit * st.zoom;
    const cx = (VIEW.x0 + VIEW.x1) / 2 + st.panX, cy = (VIEW.y0 + VIEW.y1) / 2 + st.panY;
    return { w, h, s, dpr, ox: w / 2 - cx * s, oy: h / 2 + cy * s };
  }
  const px = (v, x, y) => [v.ox + x * v.s, v.oy - y * v.s];

  function indexAt(t) {
    const ts = st.data.t;
    let lo = 0, hi = ts.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (ts[mid] <= t) lo = mid; else hi = mid - 1; }
    return lo;                                   // vertex index reached at time t
  }

  function fmt(sec) {
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
  }

  // ---------------------------------------------------------------- static layers
  function noisePattern(ctx) {
    if (!noise) {
      noise = document.createElement("canvas");
      noise.width = noise.height = 160;
      const n = noise.getContext("2d");
      const img = n.createImageData(160, 160);
      let seed = 7;
      for (let i = 0; i < img.data.length; i += 4) {
        seed = (seed * 16807) % 2147483647;
        const r = seed / 2147483647;
        const v = r > 0.9 ? 205 : r > 0.55 ? 150 : 95;              // gold-ish speckle of textured PEI
        img.data[i] = v; img.data[i + 1] = v * 0.78; img.data[i + 2] = v * 0.42;
        img.data[i + 3] = r > 0.55 ? 70 : 35;
      }
      n.putImageData(img, 0, 0);
    }
    return ctx.createPattern(noise, "repeat");
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  function drawBed(ctx, v, g) {
    const [bw, bh] = g.bed;
    ctx.fillStyle = "#1b1d22";                                       // heat bed / frame
    let [x, y] = px(v, -7, bh + 7);
    roundRect(ctx, x, y, (bw + 14) * v.s, (bh + 14) * v.s, 10 * v.s); ctx.fill();
    [x, y] = px(v, 0, bh);
    const grad = ctx.createLinearGradient(x, y, x + bw * v.s, y + bh * v.s);
    grad.addColorStop(0, "#5b4a2b"); grad.addColorStop(0.5, "#6d5832"); grad.addColorStop(1, "#4d3d22");
    ctx.fillStyle = grad;
    roundRect(ctx, x, y, bw * v.s, bh * v.s, 6 * v.s); ctx.fill();
    ctx.save(); ctx.clip();
    ctx.fillStyle = noisePattern(ctx); ctx.fillRect(x, y, bw * v.s, bh * v.s);
    ctx.restore();
    const [tx, ty] = px(v, bw / 2 - 26, 0);                          // front handle tab
    ctx.fillStyle = "#4a3b21";
    roundRect(ctx, tx, ty - 1, 52 * v.s, 9 * v.s, 3 * v.s); ctx.fill();
    ctx.strokeStyle = "rgba(255,255,255,0.18)"; ctx.lineWidth = Math.max(1, v.s * 0.4);
    roundRect(ctx, x, y, bw * v.s, bh * v.s, 6 * v.s); ctx.stroke();
    drawRulers(ctx, v, g);
  }

  function drawRulers(ctx, v, g) {
    const [bw, bh] = g.bed;
    ctx.strokeStyle = "rgba(90,96,110,0.9)"; ctx.fillStyle = "rgba(70,76,90,0.95)";
    ctx.lineWidth = 1; ctx.font = `${Math.max(10, 3.2 * v.s)}px -apple-system, system-ui, sans-serif`;
    for (let m = 0; m <= bw; m += 10) {
      const [x, y] = px(v, m, -8);
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, y + (m % 50 ? 1.6 : 3.2) * v.s); ctx.stroke();
      if (m % 50 === 0) ctx.fillText(String(m), x + 2, y + 7 * v.s);
    }
    for (let m = 0; m <= bh; m += 10) {
      const [x, y] = px(v, -8, m);
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - (m % 50 ? 1.6 : 3.2) * v.s, y); ctx.stroke();
      if (m % 50 === 0) { ctx.textAlign = "right"; ctx.fillText(String(m), x - 4 * v.s, y + 3); ctx.textAlign = "left"; }
    }
    const [lx, ly] = px(v, bw / 2 - 40, -25);
    ctx.fillText("передний край стола · дверца · мм", lx, ly);
  }

  function drawPaper(ctx, v, g) {
    const [x0, y0, x1, y1] = g.paper;
    const [x, y] = px(v, x0, y1);
    const w = (x1 - x0) * v.s, h = (y1 - y0) * v.s;
    ctx.save(); ctx.shadowColor = "rgba(0,0,0,0.45)"; ctx.shadowBlur = 6 * v.s; ctx.shadowOffsetY = 1.5 * v.s;
    ctx.fillStyle = "#fbfbf7"; ctx.fillRect(x, y, w, h); ctx.restore();
    ctx.strokeStyle = COLORS.grid; ctx.lineWidth = Math.max(0.5, v.s * 0.12);
    ctx.beginPath();
    for (let gx = x0 + 5; gx < x1; gx += 5) { const [a] = px(v, gx, 0); ctx.moveTo(a, y); ctx.lineTo(a, y + h); }
    for (let gy = y0 + 5; gy < y1; gy += 5) { const [, b] = px(v, 0, gy); ctx.moveTo(x, b); ctx.lineTo(x + w, b); }
    ctx.stroke();
  }

  function drawJig(ctx, v, g) {
    if (!g.jig || !g.jig.length) return;
    ctx.beginPath();
    g.jig.forEach(([mx, my], i) => { const [a, b] = px(v, mx, my); i ? ctx.lineTo(a, b) : ctx.moveTo(a, b); });
    ctx.closePath();
    ctx.fillStyle = COLORS.jig; ctx.fill();
    ctx.strokeStyle = COLORS.jigLine; ctx.lineWidth = Math.max(1, v.s * 0.35); ctx.stroke();
    const [a, b] = px(v, 4, 30);
    ctx.fillStyle = "#e8efff"; ctx.font = `600 ${Math.max(10, 3.4 * v.s)}px system-ui, sans-serif`;
    ctx.fillText("L-упор", a, b);
  }

  function strokeRange(ctx, v, from, to, kindWanted, style, width) {
    const d = st.data;
    ctx.strokeStyle = style; ctx.lineWidth = width; ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.beginPath();
    let open = false;
    for (let i = from; i < to; i++) {
      if (d.k[i] !== kindWanted) { open = false; continue; }
      const [a, b] = px(v, d.x[i], d.y[i]);
      const [c, e] = px(v, d.x[i + 1], d.y[i + 1]);
      if (!open) { ctx.moveTo(a, b); open = true; }
      ctx.lineTo(c, e);
    }
    ctx.stroke();
  }

  function renderBase(v) {
    const ctx = base.getContext("2d");
    ctx.clearRect(0, 0, base.width, base.height);
    const g = st.data.geometry;
    drawBed(ctx, v, g);
    drawJig(ctx, v, g);
    drawPaper(ctx, v, g);
    if (st.showGhost) strokeRange(ctx, v, 0, st.data.k.length, "d", COLORS.ghost, Math.max(1, 0.5 * v.s));
  }

  // ---------------------------------------------------------------- dynamic layers
  function syncInk(v, idx, viewChanged) {
    const ctx = ink.getContext("2d");
    if (viewChanged || idx < st.inkIdx) {
      ctx.clearRect(0, 0, ink.width, ink.height);
      st.inkIdx = 0;
    }
    if (idx > st.inkIdx) {
      if (st.showTravel) strokeRange(ctx, v, st.inkIdx, idx, "t", COLORS.travel, Math.max(1, v.dpr));
      strokeRange(ctx, v, st.inkIdx, idx, "d", COLORS.ink, Math.max(1.2, 0.5 * v.s));
      st.inkIdx = idx;
    }
  }

  function penAt(t) {
    const d = st.data, i = indexAt(t);
    if (i >= d.k.length) return { x: d.x[i], y: d.y[i], z: d.z[i], kind: "", i };
    const t0 = d.t[i], t1 = d.t[i + 1], f = t1 > t0 ? (t - t0) / (t1 - t0) : 1;
    const lerp = (a) => a[i] + (a[i + 1] - a[i]) * Math.min(1, Math.max(0, f));
    return { x: lerp(d.x), y: lerp(d.y), z: lerp(d.z), kind: d.k[i], i };
  }

  function drawOverlay(ctx, v, pen) {
    const g = st.data.geometry;
    const d = st.data;
    if (pen.kind === "d") {                                           // segment in progress
      const [a, b] = px(v, d.x[pen.i], d.y[pen.i]);
      const [c, e] = px(v, pen.x, pen.y);
      ctx.strokeStyle = COLORS.ink; ctx.lineWidth = Math.max(1.2, 0.5 * v.s);
      ctx.beginPath(); ctx.moveTo(a, b); ctx.lineTo(c, e); ctx.stroke();
    }
    const [x, y] = px(v, pen.x, pen.y);
    const down = pen.z < g.z_contact;
    if (st.showHolder) {
      ctx.fillStyle = COLORS.holder; ctx.strokeStyle = COLORS.holderLine; ctx.lineWidth = Math.max(1, v.dpr);
      ctx.beginPath(); ctx.arc(x, y, g.holder_radius * v.s, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      const [nx, ny] = px(v, pen.x - g.offset[0], pen.y - g.offset[1]);     // nozzle = pen − offset
      ctx.setLineDash([4 * v.dpr, 4 * v.dpr]);
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(nx, ny); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = "#374151"; ctx.fillRect(nx - 3 * v.dpr, ny - 3 * v.dpr, 6 * v.dpr, 6 * v.dpr);
    }
    ctx.beginPath(); ctx.arc(x, y, Math.max(3 * v.dpr, 1.1 * v.s), 0, Math.PI * 2);
    ctx.fillStyle = down ? COLORS.pen : "#ffffff"; ctx.fill();
    ctx.lineWidth = 2 * v.dpr; ctx.strokeStyle = COLORS.pen; ctx.stroke();
    for (const is of d.issues) {
      if (is.x == null) continue;
      const [a, b] = px(v, is.x, is.y);
      ctx.beginPath(); ctx.arc(a, b, 7 * v.dpr, 0, Math.PI * 2);
      ctx.fillStyle = COLORS.issue; ctx.fill();
      ctx.fillStyle = "#fff"; ctx.font = `700 ${10 * v.dpr}px system-ui`; ctx.fillText("!", a - 2 * v.dpr, b + 4 * v.dpr);
    }
    drawZGauge(ctx, v, pen, g);
  }

  function drawZGauge(ctx, v, pen, g) {
    const d = v.dpr, lo = g.z_pen - 2, hi = g.z_clear + 2;
    // right of the bed; pinned to the canvas edge when zoomed in
    const boxW = 56 * d, H = 140 * d, W = 12 * d;
    const bx = Math.min(el.canvas.width - boxW - 6 * d, px(v, BED_RIGHT_MM, 0)[0] + 6 * d), by = 10 * d;
    const x = bx + (boxW - W) / 2, y = by + 34 * d, mid = bx + boxW / 2;
    const zy = (z) => y + H - ((Math.min(hi, Math.max(lo, z)) - lo) / (hi - lo)) * H;
    ctx.save();
    ctx.fillStyle = "rgba(17,24,39,0.82)"; roundRect(ctx, bx, by, boxW, H + 60 * d, 8 * d); ctx.fill();
    ctx.fillStyle = "#374151"; ctx.fillRect(x, y, W, H);
    ctx.fillStyle = "rgba(37,99,235,0.55)"; ctx.fillRect(x, zy(g.z_contact), W, y + H - zy(g.z_contact));
    ctx.fillStyle = "#fbbf24"; ctx.fillRect(x - 5 * d, zy(pen.z) - 1.5 * d, W + 10 * d, 3 * d);
    ctx.textAlign = "center";
    ctx.fillStyle = "#9ca3af"; ctx.font = `${10 * d}px system-ui`; ctx.fillText("Z, мм", mid, by + 13 * d);
    ctx.fillStyle = "#fbbf24"; ctx.font = `600 ${11 * d}px system-ui`; ctx.fillText(pen.z.toFixed(1), mid, by + 27 * d);
    ctx.fillStyle = "#e5e7eb"; ctx.font = `${10 * d}px system-ui`;
    ctx.fillText(pen.z < g.z_contact ? "пишет" : pen.kind === "z" ? "подъём" : "в воздухе", mid, y + H + 17 * d);
    ctx.restore();
  }

  // ---------------------------------------------------------------- frame
  function frame() {
    if (!st.data) return;
    const v = layout();
    const pen = penAt(st.t);
    if (st.follow) followPen(v, pen);
    const v2 = layout();
    const key = [v2.w, v2.h, v2.s.toFixed(4), v2.ox.toFixed(1), v2.oy.toFixed(1), st.showGhost, st.showTravel].join();
    const viewChanged = key !== st.viewKey;
    if (viewChanged) { renderBase(v2); st.viewKey = key; }
    syncInk(v2, Math.min(pen.i, st.data.k.length), viewChanged);
    const ctx = el.canvas.getContext("2d");
    ctx.clearRect(0, 0, el.canvas.width, el.canvas.height);
    ctx.drawImage(base, 0, 0); ctx.drawImage(ink, 0, 0);
    drawOverlay(ctx, v2, pen);
    drawStrip();
    updateTexts(pen);
  }

  function followPen(v, pen) {
    st.zoom = Math.max(st.zoom, 3.5);
    const cx = (VIEW.x0 + VIEW.x1) / 2 + st.panX, cy = (VIEW.y0 + VIEW.y1) / 2 + st.panY;
    const half = (VIEW.x1 - VIEW.x0) / (2 * st.zoom);
    if (Math.abs(pen.x - cx) > half * 0.55 || Math.abs(pen.y - cy) > half * 0.55) {
      st.panX = pen.x - (VIEW.x0 + VIEW.x1) / 2;
      st.panY = pen.y - (VIEW.y0 + VIEW.y1) / 2;
    }
  }

  function drawStrip() {
    const c = el.strip;
    if (!c) return;
    const r = c.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    c.width = Math.max(100, Math.round(r.width * dpr)); c.height = Math.max(20, Math.round(r.height * dpr));
    const ctx = c.getContext("2d");
    if (!st.strip || st.strip.w !== c.width) st.strip = buildStrip(c.width);
    const { cols } = st.strip;
    for (let i = 0; i < cols.length; i++) {
      const [d, t, z] = cols[i], sum = d + t + z || 1;
      let y = c.height;
      for (const [share, color] of [[d / sum, COLORS.stripDraw], [z / sum, COLORS.stripZ], [t / sum, COLORS.stripTravel]]) {
        const h = share * c.height; ctx.fillStyle = color; ctx.fillRect(i, y - h, 1, h); y -= h;
      }
    }
    const T = total();
    ctx.fillStyle = COLORS.pause;
    for (const p of st.data.pauses) ctx.fillRect((p.t / T) * c.width - 1.5 * dpr, 0, 3 * dpr, c.height);
    ctx.fillStyle = COLORS.issue;
    for (const is of st.data.issues) if (is.t != null) ctx.fillRect((is.t / T) * c.width - 1 * dpr, 0, 2 * dpr, c.height);
    ctx.fillStyle = "rgba(255,255,255,0.35)"; ctx.fillRect(0, 0, (st.t / T) * c.width, c.height);
    ctx.fillStyle = "#2563eb"; ctx.fillRect((st.t / T) * c.width - 1 * dpr, 0, 2 * dpr, c.height);
  }

  function buildStrip(w) {
    const d = st.data, T = total(), cols = Array.from({ length: w }, () => [0, 0, 0]);
    for (let i = 0; i < d.k.length; i++) {
      const dt = d.t[i + 1] - d.t[i], col = Math.min(w - 1, Math.floor((d.t[i] / T) * w));
      cols[col][d.k[i] === "d" ? 0 : d.k[i] === "t" ? 1 : 2] += dt;
    }
    return { w, cols };
  }

  function timeShares() {
    if (!st.timeShares) {
      const d = st.data, sums = { d: 0, t: 0, z: 0 };
      for (let i = 0; i < d.k.length; i++) sums[d.k[i]] += d.t[i + 1] - d.t[i];
      const T = total(), pct = (x) => `${Math.round((100 * x) / T)}%`;
      st.timeShares = `${pct(sums.d)} / ${pct(sums.z)} / ${pct(sums.t)}`;
    }
    return st.timeShares;
  }

  const total = () => (st.data ? st.data.t[st.data.t.length - 1] || 1 : 1);

  function updateTexts(pen) {
    const T = total();
    if (el.time) el.time.textContent = `${fmt(st.t)} / ${fmt(T)}`;
    if (el.slider && !st.sliding) el.slider.value = String(Math.round((st.t / T) * 1000));
    if (el.play) el.play.textContent = st.playing ? "❚❚ Пауза" : "▶ Пуск";
    if (!el.stats) return;
    const s = st.data.stats, d = st.data;
    const rows = [
      ["Страница", `${d.page} (шаг ${d.index} из ${d.count})`], ["Время страницы", fmt(s.seconds)],
      ["Подъёмов пера", String(s.pen_downs)], ["Чернила", `${(s.ink_mm / 1000).toFixed(2)} м`],
      ["Холостые ходы", `${(s.travel_mm / 1000).toFixed(2)} м`],
      ["Время: пишет / подъёмы / переезды", timeShares()],
      ["Сейчас", pen.z < d.geometry.z_contact ? "пишет" : pen.kind === "z" ? "подъём/опускание" : "переезд"],
      ["Перо, мм", `X ${pen.x.toFixed(1)} · Y ${pen.y.toFixed(1)} · Z ${pen.z.toFixed(2)}`],
    ];
    if (!el.stats.dataset.ready || el.stats.childElementCount !== rows.length * 2) {
      el.stats.replaceChildren(...rows.flatMap(([k]) => [Object.assign(document.createElement("dt"), { textContent: k }), document.createElement("dd")]));
      el.stats.dataset.ready = "1";
    }
    rows.forEach(([, val], i) => { el.stats.children[i * 2 + 1].textContent = val; });
  }

  function renderSide() {
    if (el.legend) {
      const items = [["чернила (уже)", COLORS.ink], ["вся страница", "rgba(19,32,63,0.25)"],
        ["холостой ход", COLORS.travel], ["держатель (радиус)", COLORS.holderLine],
        ["L-упор", COLORS.jigLine], ["пауза", COLORS.pause], ["проблема", COLORS.issue],
        ["полоса: пишет", COLORS.stripDraw], ["полоса: подъём/опускание", COLORS.stripZ],
        ["полоса: переезд", COLORS.stripTravel]];
      el.legend.replaceChildren(...items.map(([label, color]) => {
        const row = document.createElement("div"); row.className = "legend-item";
        const sw = document.createElement("span"); sw.className = "swatch"; sw.style.background = color;
        row.append(sw, document.createTextNode(label)); return row;
      }));
    }
    if (el.warnings) {
      const d = st.data, list = [];
      for (const is of d.issues) list.push(["issue", `${is.code}: ${is.message}`, is.t]);
      for (const p of d.pauses) list.push(["pause", `Пауза: ${p.label}`, p.t]);
      if (!d.issues.length) list.unshift(["ok", "Проверка на модели: проблем на этой странице нет", null]);
      el.warnings.replaceChildren(...list.map(([kind, text, t]) => {
        const b = document.createElement("button"); b.type = "button"; b.className = `warn-item ${kind}`;
        b.textContent = text;
        if (t != null) b.addEventListener("click", () => { seek(Math.max(0, t - 2)); });
        else b.disabled = true;
        return b;
      }));
    }
  }

  // ---------------------------------------------------------------- playback & input
  function tick(now) {
    if (st.playing) {
      const dt = st.last ? (now - st.last) / 1000 : 0;
      st.t = Math.min(total(), st.t + dt * st.speed);
      if (st.t >= total()) st.playing = false;
    }
    st.last = now;
    frame();
    if (st.playing) requestAnimationFrame(tick); else st.last = 0;
  }

  function play(on) {
    st.playing = on === undefined ? !st.playing : on;
    if (st.playing) { if (st.t >= total()) st.t = 0; requestAnimationFrame(tick); } else frame();
  }

  function seek(t) { st.t = Math.max(0, Math.min(total(), t)); frame(); }

  function onWheel(e) {
    e.preventDefault();
    const v = layout(), r = el.canvas.getBoundingClientRect();
    const mx = ((e.clientX - r.left) * v.dpr - v.ox) / v.s, my = (v.oy - (e.clientY - r.top) * v.dpr) / v.s;
    const z = Math.min(40, Math.max(1, st.zoom * (e.deltaY < 0 ? 1.2 : 1 / 1.2)));
    const k = st.zoom / z;
    const cx = (VIEW.x0 + VIEW.x1) / 2 + st.panX, cy = (VIEW.y0 + VIEW.y1) / 2 + st.panY;
    st.panX = mx + (cx - mx) * k - (VIEW.x0 + VIEW.x1) / 2;
    st.panY = my + (cy - my) * k - (VIEW.y0 + VIEW.y1) / 2;
    st.zoom = z;
    if (z === 1) { st.panX = 0; st.panY = 0; }
    frame();
  }

  function onDrag(e) {
    if (e.type === "pointerdown") { st.drag = { x: e.clientX, y: e.clientY, px: st.panX, py: st.panY }; el.canvas.setPointerCapture(e.pointerId); return; }
    if (e.type !== "pointermove" || !st.drag) { st.drag = null; return; }
    const v = layout();
    st.panX = st.drag.px - ((e.clientX - st.drag.x) * v.dpr) / v.s;
    st.panY = st.drag.py + ((e.clientY - st.drag.y) * v.dpr) / v.s;
    frame();
  }

  function mount() {
    const ids = { canvas: "motion-canvas", play: "motion-play", speed: "motion-speed", slider: "motion-slider",
      time: "motion-time", strip: "motion-strip", stats: "motion-stats", legend: "motion-legend",
      warnings: "motion-warnings", travel: "motion-show-travel", holder: "motion-show-holder",
      ghost: "motion-show-ghost", follow: "motion-follow" };
    for (const [k, id] of Object.entries(ids)) el[k] = document.getElementById(id);
    if (!el.canvas) return false;
    base = document.createElement("canvas"); ink = document.createElement("canvas");
    el.play?.addEventListener("click", () => play());
    el.speed?.addEventListener("change", () => { st.speed = Number(el.speed.value) || 60; });
    if (el.speed) st.speed = Number(el.speed.value) || 60;
    el.slider?.addEventListener("input", () => { st.sliding = true; seek((Number(el.slider.value) / 1000) * total()); });
    el.slider?.addEventListener("change", () => { st.sliding = false; });
    el.strip?.addEventListener("click", (e) => {
      const r = el.strip.getBoundingClientRect(); seek(((e.clientX - r.left) / r.width) * total());
    });
    const bind = (box, key) => box?.addEventListener("change", () => { st[key] = box.checked; st.viewKey = ""; frame(); });
    bind(el.travel, "showTravel"); bind(el.holder, "showHolder"); bind(el.ghost, "showGhost"); bind(el.follow, "follow");
    for (const [box, key] of [[el.travel, "showTravel"], [el.holder, "showHolder"], [el.ghost, "showGhost"], [el.follow, "follow"]])
      if (box) box.checked = st[key];
    el.canvas.addEventListener("wheel", onWheel, { passive: false });
    for (const ev of ["pointerdown", "pointermove", "pointerup", "pointercancel"]) el.canvas.addEventListener(ev, onDrag);
    el.canvas.addEventListener("dblclick", () => { st.zoom = 1; st.panX = st.panY = 0; frame(); });
    new ResizeObserver(() => { st.viewKey = ""; st.strip = null; frame(); }).observe(el.canvas);
    return true;
  }

  function load(data) {
    st.data = data; st.t = 0; st.playing = false; st.inkIdx = 0; st.viewKey = ""; st.strip = null; st.timeShares = null;
    if (!st.follow) { st.zoom = 1; st.panX = st.panY = 0; }
    renderSide();
    frame();
  }

  return { mount, load, play, seek, get time() { return st.t; } };
})();

window.MotionViewer = MotionViewer;
