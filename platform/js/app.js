// Flood Risk Atlas — main application
import { usd, pct, count, signedPct, metricValue } from "./format.js";
import { css, scenarioBars, epCurve, tableView } from "./charts.js";

const BASEMAPS = {
  light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  dark: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
};
const PERIL_LABEL = { combined: "All flood types", fluvial: "River (fluvial)", pluvial: "Rainfall (pluvial)", coastal: "Coastal" };
const PERIL_SHORT = { combined: "All", fluvial: "River", pluvial: "Rainfall", coastal: "Coastal" };
const SCEN_ORDER = ["baseline", "ssp245_2030", "ssp585_2030", "ssp245_2060", "ssp585_2060"];
const SCEN_CATS = ["Present", "2030\nSSP2-4.5", "2030\nSSP5-8.5", "2060\nSSP2-4.5", "2060\nSSP5-8.5"];
const NCOL = 7; // columns per ZIP in metric binaries

const S = { ssp: "present", year: "2030", def: "fixed", peril: "combined", metric: "aal_usd", view: "level", state: "", zip: null };
let META, Z, map, protocol, hoverId = null, selectedId = null;
const metricCache = new Map(); // `${scen}__${peril}` -> Float32Array
const rpCache = new Map();     // state fips -> json
const zipIndex = new Map();    // zip -> index

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);
async function fetchRetry(url, tries = 3) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url);
      if (r.ok) return r;
    } catch (_) { /* network hiccup -> retry */ }
    await new Promise((res) => setTimeout(res, 400 * (i + 1)));
  }
  throw new Error(`failed to load ${url}`);
}
const adaptSuffix = () => (S.def === "adapt" && (META.defences || []).some((d) => d.id === "adapt") ? "_adapt" : "");
const scenId = () => (S.ssp === "present" ? "baseline" : `${S.ssp}_${S.year}${adaptSuffix()}`);
// scenario order of the charts, in the selected defence scenario
const scenOrder = () => SCEN_ORDER.map((sid) => (sid === "baseline" ? sid : sid + adaptSuffix()));
function scenMeta(id) {
  const base = id.replace(/_adapt$/, "");
  const s = META.scenarios.find((x) => x.id === base);
  return id.endsWith("_adapt") ? { ...s, id, label: `${s.label} · raised defences` } : s;
}
const metricDef = (k = S.metric) => META.metrics.find((m) => m.key === k);
const family = (k = S.metric) => (k.startsWith("aal") ? "aal" : k.startsWith("l1000") ? "l1000" : k.startsWith("l100") ? "l100" : "pct_flooded");
const usdCol = { aal: 0, l100: 2, l1000: 4 };

async function getMetrics(scen, peril) {
  const key = `${scen}__${peril}`;
  if (!metricCache.has(key)) {
    const r = await fetchRetry(`data/m/${key}.bin`);
    metricCache.set(key, new Float32Array(await r.arrayBuffer()));
  }
  return metricCache.get(key);
}
async function getRP(st) {
  if (!rpCache.has(st)) rpCache.set(st, await (await fetchRetry(`data/rp/${st}.json`)).json());
  return rpCache.get(st);
}
function inRegion(i) {
  return !S.state || Z.st[i] === S.state;
}
function stateName(fips) {
  const s = META.states.find((x) => x.fips === fips);
  return s ? s.abbr : fips;
}

// ------------------------------------------------------------------ URL state
function readHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  for (const k of Object.keys(S)) if (h.has(k)) S[k] = h.get(k) || (k === "zip" ? null : S[k]);
}
function writeHash() {
  const h = new URLSearchParams();
  for (const [k, v] of Object.entries(S)) if (v) h.set(k, v);
  history.replaceState(null, "", "#" + h.toString());
}

// ------------------------------------------------------------------ theme
function currentTheme() {
  const t = document.documentElement.getAttribute("data-theme");
  if (t) return t;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("fra-theme"); } catch (_) { /* storage unavailable */ }
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  else document.documentElement.setAttribute("data-theme", currentTheme());
  $("themeToggle").addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("fra-theme", next); } catch (_) { /* ignore */ }
    map.setStyle(BASEMAPS[next]);
    map.once("style.load", () => { addDataLayers(); paintMap(); });
    renderPanel();
  });
}

// ------------------------------------------------------------------ map
function classColors() {
  return S.view === "change"
    ? ["--div-n3", "--div-n2", "--div-n1", "--div-0", "--div-p1", "--div-p2", "--div-p3"].map(css)
    : ["--seq-1", "--seq-2", "--seq-3", "--seq-4", "--seq-5", "--seq-6", "--seq-7"].map(css);
}
function fillExpression() {
  const cols = classColors();
  const zero = S.view === "change" ? css("--div-0") : css("--seq-zero");
  return ["match", ["coalesce", ["feature-state", "c"], -1],
    -1, "rgba(0,0,0,0)", 0, zero,
    1, cols[0], 2, cols[1], 3, cols[2], 4, cols[3], 5, cols[4], 6, cols[5], 7, cols[6],
    "rgba(0,0,0,0)"];
}
function firstSymbolLayer() {
  // insert the choropleth above roads/bridges but below place & road labels
  const ls = map.getStyle().layers;
  let lastRoad = -1;
  ls.forEach((x, i) => { if (x.type !== "symbol" && /road|bridge|tunnel|rail|aeroway|building/i.test(x.id)) lastRoad = i; });
  const after = ls.slice(lastRoad + 1).find((x) => x.type === "symbol");
  return after ? after.id : undefined;
}
function addDataLayers() {
  if (!map.getSource("zcta")) {
    map.addSource("zcta", { type: "vector", url: "pmtiles://" + new URL("data/zcta.pmtiles", location.href).href, promoteId: "zip" });
  }
  const before = firstSymbolLayer();
  map.addLayer({ id: "zcta-fill", type: "fill", source: "zcta", "source-layer": "zcta",
    paint: { "fill-color": fillExpression(),
             "fill-opacity": ["interpolate", ["linear"], ["zoom"], 9, 0.88, 13, 0.62] } }, before);
  map.addLayer({ id: "zcta-line", type: "line", source: "zcta", "source-layer": "zcta", minzoom: 7,
    paint: { "line-color": css("--surface-1"), "line-width": ["interpolate", ["linear"], ["zoom"], 7, 0.2, 12, 0.9], "line-opacity": 0.55 } }, before);
  map.addLayer({ id: "zcta-hover", type: "line", source: "zcta", "source-layer": "zcta",
    paint: { "line-color": css("--text-primary"), "line-width": ["case", ["boolean", ["feature-state", "hover"], false], 1.4, 0] } }, before);
  map.addLayer({ id: "zcta-sel", type: "line", source: "zcta", "source-layer": "zcta",
    paint: { "line-color": css("--accent"), "line-width": ["case", ["boolean", ["feature-state", "sel"], false], 2.6, 0] } }, before);
}

let classes = null; // Int8Array per ZIP
async function computeClasses() {
  const md = metricDef();
  const cur = await getMetrics(scenId(), S.peril);
  const out = new Int8Array(Z.zip.length);
  if (S.view === "change") {
    const base = await getMetrics("baseline", S.peril);
    const b = META.change_breaks; // percent
    for (let i = 0; i < Z.zip.length; i++) {
      const v = cur[i * NCOL + md.col], v0 = base[i * NCOL + md.col];
      if (!(Z.nb[i] > 0)) { out[i] = -1; continue; }
      if (!(v0 > 0) && !(v > 0)) { out[i] = 0; continue; }
      const p = v0 > 0 ? (v / v0 - 1) * 100 : Infinity;
      let k = 0; while (k < b.length && p >= b[k]) k++;
      out[i] = k + 1;
    }
  } else {
    const b = META.breaks[md.key];
    for (let i = 0; i < Z.zip.length; i++) {
      const v = cur[i * NCOL + md.col];
      if (!(Z.nb[i] > 0)) { out[i] = -1; continue; }
      if (!(v > 0)) { out[i] = 0; continue; }
      let k = 0; while (k < b.length && v >= b[k]) k++;
      out[i] = k + 1;
    }
  }
  classes = out;
}
function applyClasses() {
  if (!map.getSource("zcta") || !classes) return;
  for (let i = 0; i < Z.zip.length; i++) {
    map.setFeatureState({ source: "zcta", sourceLayer: "zcta", id: Z.zip[i] }, { c: classes[i] });
  }
  if (selectedId) map.setFeatureState({ source: "zcta", sourceLayer: "zcta", id: selectedId }, { sel: true });
}
async function paintMap() {
  await computeClasses();
  if (map.getLayer("zcta-fill")) map.setPaintProperty("zcta-fill", "fill-color", fillExpression());
  applyClasses();
  renderLegend();
}

function renderLegend() {
  const md = metricDef();
  const el = $("legend");
  const cols = classColors();
  let labels;
  if (S.view === "change") {
    labels = META.change_breaks.map((b) => (b > 0 ? "+" : b < 0 ? "−" : "") + Math.abs(b) + "%");
  } else {
    labels = META.breaks[md.key].map((b) => metricValue(b, md.unit));
  }
  const scen = scenMeta(scenId());
  const title = S.view === "change" ? `Change in ${md.short.toLowerCase()} vs present` : md.label;
  el.innerHTML = "";
  const t = document.createElement("div"); t.className = "legend-title"; t.textContent = title;
  const sub = document.createElement("div"); sub.className = "legend-sub";
  sub.textContent = `${PERIL_LABEL[S.peril]} · ${scen.label}`;
  const sw = document.createElement("div"); sw.className = "legend-swatches";
  cols.forEach((c) => { const i = document.createElement("i"); i.style.background = c; sw.appendChild(i); });
  const lb = document.createElement("div"); lb.className = "legend-labels";
  labels.forEach((l, k) => {
    const s = document.createElement("span");
    s.textContent = l;
    s.style.left = `${((k + 1) / 7) * 100}%`;
    lb.appendChild(s);
  });
  const ex = document.createElement("div"); ex.className = "legend-extra";
  ex.innerHTML = `<span><i style="background:${S.view === "change" ? css("--div-0") : css("--seq-zero")}"></i>No modeled loss</span><span><i class="hatch"></i>No residential data</span>`;
  el.append(t, sub, sw, lb, ex);
}

function initMap() {
  protocol = new pmtiles.Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);
  const st = META.default_view || { center: [-96.5, 38.5], zoom: 3.6 };
  map = new maplibregl.Map({
    container: "map", style: BASEMAPS[currentTheme()], center: st.center, zoom: st.zoom,
    minZoom: 2.5, maxZoom: 13, attributionControl: { compact: true }, dragRotate: false, pitchWithRotate: false,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  map.on("load", async () => {
    addDataLayers();
    await paintMap();
    $("loading").classList.add("done");
    if (S.zip) selectZip(S.zip, { fly: true });
  });
  const tip = $("maptip");
  map.on("mousemove", "zcta-fill", (e) => {
    const f = e.features && e.features[0];
    if (!f) return;
    const id = f.id ?? f.properties.zip;
    if (hoverId !== id) {
      if (hoverId) map.setFeatureState({ source: "zcta", sourceLayer: "zcta", id: hoverId }, { hover: false });
      hoverId = id;
      map.setFeatureState({ source: "zcta", sourceLayer: "zcta", id }, { hover: true });
    }
    map.getCanvas().style.cursor = "pointer";
    showTip(id, e.point);
  });
  map.on("mouseleave", "zcta-fill", () => {
    if (hoverId) map.setFeatureState({ source: "zcta", sourceLayer: "zcta", id: hoverId }, { hover: false });
    hoverId = null; tip.hidden = true; map.getCanvas().style.cursor = "";
  });
  map.on("click", "zcta-fill", (e) => {
    const f = e.features && e.features[0];
    if (f) selectZip(f.id ?? f.properties.zip, { fly: false });
  });
}

async function showTip(zip, pt) {
  const tip = $("maptip");
  const i = zipIndex.get(String(zip));
  tip.replaceChildren();
  const z = document.createElement("div"); z.className = "t-zip";
  const v = document.createElement("div"); v.className = "t-val";
  const l = document.createElement("div"); l.className = "t-lab";
  if (i === undefined) {
    z.textContent = `ZIP ${zip}`; v.textContent = "—"; l.textContent = "No residential data";
  } else {
    const md = metricDef();
    const cur = await getMetrics(scenId(), S.peril);
    z.textContent = `ZIP ${zip} · ${Z.cty[i]}, ${stateName(Z.st[i])}`;
    if (S.view === "change") {
      const base = await getMetrics("baseline", S.peril);
      const a = cur[i * NCOL + md.col], b = base[i * NCOL + md.col];
      v.textContent = b > 0 ? signedPct(a / b - 1) : a > 0 ? "new risk" : "—";
      l.textContent = `${md.short} change vs present`;
    } else {
      v.textContent = metricValue(cur[i * NCOL + md.col], md.unit);
      l.textContent = md.label;
    }
  }
  tip.append(z, v, l);
  tip.style.left = pt.x + "px"; tip.style.top = pt.y + "px";
  tip.hidden = false;
}

// ------------------------------------------------------------------ controls
function syncControls() {
  document.querySelectorAll(".seg").forEach((seg) => {
    const key = seg.dataset.key;
    seg.querySelectorAll("button").forEach((b) => {
      b.setAttribute("aria-checked", String(String(S[key]) === b.dataset.v));
      b.tabIndex = String(S[key]) === b.dataset.v ? 0 : -1;
    });
  });
  document.querySelectorAll('.seg[data-key="year"] button').forEach((b) => (b.disabled = S.ssp === "present"));
  document.querySelectorAll('.seg[data-key="def"] button').forEach((b) => (b.disabled = S.ssp === "present"));
  document.querySelectorAll('.seg[data-key="view"] button[data-v="change"]').forEach((b) => (b.disabled = S.ssp === "present"));
  $("metricSel").value = S.metric;
  $("stateSel").value = S.state;
}
function initControls() {
  const ms = $("metricSel");
  META.metrics.forEach((m) => { const o = document.createElement("option"); o.value = m.key; o.textContent = m.label; ms.appendChild(o); });
  const ss = $("stateSel");
  META.states.slice().sort((a, b) => a.name.localeCompare(b.name)).forEach((s) => {
    const o = document.createElement("option"); o.value = s.fips; o.textContent = s.name; ss.appendChild(o);
  });
  document.querySelectorAll(".seg").forEach((seg) => {
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b || b.disabled) return;
      S[seg.dataset.key] = b.dataset.v;
      if (S.ssp === "present") S.view = "level";
      update();
    });
    seg.addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
      const bs = [...seg.querySelectorAll("button:not([disabled])")];
      const i = bs.findIndex((b) => b.getAttribute("aria-checked") === "true");
      const n = bs[(i + (e.key === "ArrowRight" ? 1 : bs.length - 1)) % bs.length];
      n.click(); n.focus();
    });
  });
  ms.addEventListener("change", () => { S.metric = ms.value; update(); });
  ss.addEventListener("change", () => { S.state = ss.value; zoomToRegion(); update(); });
  const zs = $("zipSearch");
  zs.addEventListener("input", () => {
    zs.value = zs.value.replace(/\D/g, "").slice(0, 5);
    $("searchHint").textContent = "";
    if (zs.value.length === 5) trySearch(zs.value);
  });
  zs.addEventListener("keydown", (e) => { if (e.key === "Enter") trySearch(zs.value); });
  $("backBtn").addEventListener("click", () => clearZip());
  document.querySelectorAll(".tv").forEach((b) => b.addEventListener("click", () => {
    const card = $(b.dataset.target);
    const on = b.getAttribute("aria-pressed") !== "true";
    b.setAttribute("aria-pressed", String(on));
    b.textContent = on ? "Chart" : "Table";
    card.querySelector(".tablev").hidden = !on;
    const ch = card.querySelector(".chart, .segbar");
    if (ch) ch.style.display = on ? "none" : "";
    const lg = card.querySelector(".seglegend"); if (lg) lg.style.display = on ? "none" : "";
  }));
}
function trySearch(z) {
  if (zipIndex.has(z)) { selectZip(z, { fly: true }); $("searchHint").textContent = ""; }
  else $("searchHint").textContent = "not in coverage";
}
function zoomToRegion() {
  if (!S.state) { const v = META.default_view; map.flyTo({ center: v.center, zoom: v.zoom, duration: 900 }); return; }
  const s = META.states.find((x) => x.fips === S.state);
  if (s && s.bbox) map.fitBounds([[s.bbox[0], s.bbox[1]], [s.bbox[2], s.bbox[3]]], { padding: { top: 40, bottom: 40, left: 40, right: 460 }, duration: 900 });
}

async function update() {
  syncControls();
  writeHash();
  document.querySelectorAll(".card").forEach((c) => c.classList.add("refetch"));
  await paintMap();
  await renderPanel();
  document.querySelectorAll(".card").forEach((c) => c.classList.remove("refetch"));
}

// ------------------------------------------------------------------ panel
function regionSums(arr, col) {
  let s = 0;
  for (let i = 0; i < Z.zip.length; i++) if (inRegion(i)) { const v = arr[i * NCOL + col]; if (v > 0) s += v; }
  return s;
}
function regionExposure() {
  let nb = 0, nu = 0, val = 0;
  for (let i = 0; i < Z.zip.length; i++) if (inRegion(i)) { nb += Z.nb[i]; nu += Z.nu[i]; val += Z.val[i]; }
  return { nb, nu, val };
}
function familyValue(arr, fam, exp) {
  if (fam === "pct_flooded") {
    let fl = 0;
    for (let i = 0; i < Z.zip.length; i++) if (inRegion(i)) fl += (arr[i * NCOL + 6] || 0) * Z.nb[i];
    return { usd: null, n: fl, ratio: exp.nb ? fl / exp.nb : 0 };
  }
  const u = regionSums(arr, usdCol[fam]);
  return { usd: u, ratio: exp.val ? u / exp.val : 0 };
}
const FAM_LABEL = {
  aal: "Expected annual loss",
  l100: "Loss from 1-in-100-year floods",
  l1000: "Loss from 1-in-1,000-year floods",
  pct_flooded: "Homes flooded in a 1-in-100-year flood",
};

async function renderPanel() {
  if (S.zip) return renderZip();
  $("viewOverview").hidden = false; $("viewZip").hidden = true;
  const fam = family();
  const exp = regionExposure();
  const scen = scenMeta(scenId());
  $("ctxRegion").textContent = S.state ? META.states.find((s) => s.fips === S.state).name : META.coverage_label;
  $("ctxScenario").textContent = `${scen.label} · ${PERIL_SHORT[S.peril]}`;

  const cur = familyValue(await getMetrics(scenId(), S.peril), fam, exp);
  const base = familyValue(await getMetrics("baseline", S.peril), fam, exp);
  $("heroLabel").textContent = FAM_LABEL[fam] + (fam === "l100" || fam === "l1000" ? " (sum of local losses)" : "");
  $("heroValue").textContent = fam === "pct_flooded" ? count(cur.n) : usd(cur.usd);
  const sub = $("heroSub"); sub.replaceChildren();
  const r = document.createElement("span");
  r.textContent = fam === "pct_flooded" ? `${pct(cur.ratio)} of residential buildings` : `${pct(cur.ratio)} of replacement value`;
  sub.appendChild(r);
  if (scenId() !== "baseline") {
    const a = fam === "pct_flooded" ? cur.n : cur.usd, b = fam === "pct_flooded" ? base.n : base.usd;
    if (b > 0) {
      const d = document.createElement("span");
      d.className = "delta " + (a >= b ? "up" : "down");
      d.textContent = `${signedPct(a / b - 1)} vs present`;
      sub.appendChild(d);
    }
  }
  renderTiles($("tiles"), [
    ["Buildings", count(exp.nb), "residential"],
    ["Housing units", count(exp.nu), ""],
    ["Value", usd(exp.val), "structure + contents"],
  ]);

  // scenario chart (emphasis)
  const vals = [];
  const order = scenOrder();
  for (const sid of order) {
    const v = familyValue(await getMetrics(sid, S.peril), fam, exp);
    vals.push(fam === "pct_flooded" ? v.n : v.usd);
  }
  const fmt = fam === "pct_flooded" ? count : usd;
  $("scenTitle").textContent = `${FAM_LABEL[fam]} by scenario` + (adaptSuffix() ? " · raised defences" : "");
  scenarioBars($("chartScenario"), SCEN_CATS, vals, order.indexOf(scenId()), fmt);
  tableView($("cardScenario").querySelector(".tablev"), ["Scenario", "Year", FAM_LABEL[fam]],
    order.map((sid, i) => { const s = scenMeta(sid); return [s.ssp === "present" ? "Present climate" : s.ssp.toUpperCase().replace("SSP", "SSP").replace(/(\d)(\d)(\d)/, "$1-$2.$3"), String(s.year), fmt(vals[i])]; }), [2]);

  // peril shares (tall segmented bar)
  const pv = [];
  for (const p of ["fluvial", "pluvial", "coastal"]) {
    const v = familyValue(await getMetrics(scenId(), p), fam, exp);
    pv.push({ p, v: fam === "pct_flooded" ? v.n : v.usd });
  }
  renderSegbar(pv, fmt, cur);

  // ranking
  await renderRanking();
}

function renderTiles(el, items) {
  el.replaceChildren();
  for (const [l, v, s] of items) {
    const d = document.createElement("div"); d.className = "tile";
    const a = document.createElement("div"); a.className = "tile-label"; a.textContent = l;
    const b = document.createElement("div"); b.className = "tile-value"; b.textContent = v;
    d.append(a, b);
    if (s) { const c = document.createElement("div"); c.className = "tile-sub"; c.textContent = s; d.appendChild(c); }
    el.appendChild(d);
  }
}

function renderSegbar(pv, fmt, combined) {
  const total = pv.reduce((a, b) => a + (b.v || 0), 0);
  const bar = $("segbar"), lg = $("seglegend");
  bar.replaceChildren(); lg.replaceChildren();
  const colorVar = { fluvial: "--fluvial", pluvial: "--pluvial", coastal: "--coastal" };
  pv.forEach(({ p, v }) => {
    const share = total > 0 ? v / total : 0;
    if (share > 0) {
      const b = document.createElement("div");
      b.className = "blk"; b.tabIndex = 0;
      b.style.flex = `${share} 1 0`; b.style.background = css(colorVar[p]);
      b.title = `${PERIL_LABEL[p]}: ${fmt(v)} (${pct(share)})`;
      b.setAttribute("aria-label", b.title);
      if (share >= 0.14) b.textContent = `${Math.round(share * 100)}%`;
      bar.appendChild(b);
    }
    const s = document.createElement("span");
    const i = document.createElement("i"); i.style.background = css(colorVar[p]);
    s.append(i, document.createTextNode(PERIL_SHORT[p]));
    lg.appendChild(s);
  });
  bar.setAttribute("aria-label", pv.map(({ p, v }) => `${PERIL_SHORT[p]} ${pct(total ? v / total : 0)}`).join(", "));
  $("perilNote").textContent = total > 0
    ? "Shares of the sum of single-peril losses. Where perils overlap, the combined estimate takes the deepest flood, so it can be below the sum."
    : "No modeled loss for this selection.";
  tableView($("cardPeril").querySelector(".tablev"), ["Flood type", "Loss", "Share"],
    pv.map(({ p, v }) => [PERIL_LABEL[p], fmt(v), pct(total ? v / total : 0)]), [1, 2]);
}

async function renderRanking() {
  const md = metricDef();
  const cur = await getMetrics(scenId(), S.peril);
  const idx = [];
  for (let i = 0; i < Z.zip.length; i++) if (inRegion(i) && cur[i * NCOL + md.col] > 0) idx.push(i);
  idx.sort((a, b) => cur[b * NCOL + md.col] - cur[a * NCOL + md.col]);
  const top = idx.slice(0, 10);
  const max = top.length ? cur[top[0] * NCOL + md.col] : 1;
  $("rankTitle").textContent = `Highest ${md.short.toLowerCase()} — top ZIP codes`;
  const t = $("rankTable"); t.replaceChildren();
  const tb = t.createTBody();
  top.forEach((i) => {
    const tr = tb.insertRow();
    tr.tabIndex = 0;
    const v = cur[i * NCOL + md.col];
    const c1 = tr.insertCell(); c1.className = "r-zip"; c1.textContent = Z.zip[i];
    const c2 = tr.insertCell(); c2.className = "r-place"; c2.textContent = `${Z.cty[i]}, ${stateName(Z.st[i])}`;
    const c3 = tr.insertCell(); c3.className = "r-bar"; const bi = document.createElement("i"); bi.style.width = `${Math.max(4, (v / max) * 100)}%`; c3.appendChild(bi);
    const c4 = tr.insertCell(); c4.className = "r-val"; c4.textContent = metricValue(v, md.unit);
    const go = () => selectZip(Z.zip[i], { fly: true });
    tr.addEventListener("click", go);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  });
  if (!top.length) { const tr = tb.insertRow(); const c = tr.insertCell(); c.colSpan = 4; c.textContent = "No ZIP codes with modeled loss for this selection."; }
}

// ------------------------------------------------------------------ ZIP detail
async function selectZip(zip, { fly }) {
  zip = String(zip);
  const i = zipIndex.get(zip);
  if (selectedId) map.setFeatureState({ source: "zcta", sourceLayer: "zcta", id: selectedId }, { sel: false });
  selectedId = zip;
  map.setFeatureState({ source: "zcta", sourceLayer: "zcta", id: zip }, { sel: true });
  S.zip = i === undefined ? null : zip;
  if (fly && i !== undefined) map.flyTo({ center: [Z.lon[i], Z.lat[i]], zoom: Math.max(map.getZoom(), 10.5), duration: 1000, padding: { right: 420 } });
  writeHash();
  await renderPanel();
}
function clearZip() {
  if (selectedId) map.setFeatureState({ source: "zcta", sourceLayer: "zcta", id: selectedId }, { sel: false });
  selectedId = null; S.zip = null; writeHash(); renderPanel();
}

async function renderZip() {
  $("viewOverview").hidden = true; $("viewZip").hidden = false;
  const i = zipIndex.get(S.zip);
  const scen = scenMeta(scenId());
  $("zipCode").textContent = S.zip;
  $("zipPlace").textContent = `${Z.cty[i]}, ${META.states.find((s) => s.fips === Z.st[i])?.name || Z.st[i]} · ${scen.label}`;
  const m = await getMetrics(scenId(), S.peril);
  const at = (c) => m[i * NCOL + c];
  renderTiles($("zipTiles"), [
    ["Annual loss", usd(at(0)), `${pct(at(1))} of value`],
    ["1-in-100 loss", usd(at(2)), `${pct(at(3))} of value`],
    ["1-in-1,000 loss", usd(at(4)), `${pct(at(5))} of value`],
  ]);

  // loss-exceedance curves
  const rp = await getRP(Z.st[i]);
  const rec = rp[S.zip] && (rp[S.zip][scenId()] || rp[S.zip][scenId().replace(/_adapt$/, "")]);
  const defs = [];
  if (rec) {
    if (S.peril === "combined") {
      defs.push({ name: "All", values: rec.combined, color: css("--combined"), z: 5 });
      ["fluvial", "pluvial", "coastal"].forEach((p) => { if (rec[p].some((v) => v > 0)) defs.push({ name: PERIL_SHORT[p], values: rec[p], color: css(`--${p}`) }); });
    } else {
      defs.push({ name: PERIL_SHORT[S.peril], values: rec[S.peril], color: css(`--${S.peril}`) });
    }
  }
  $("epTitle").textContent = `Loss by return period · ${PERIL_SHORT[S.peril]}`;
  epCurve($("chartEP"), META.rps, defs);
  tableView($("cardEP").querySelector(".tablev"), ["Return period", ...defs.map((d) => d.name)],
    META.rps.map((T, k) => [`1-in-${T}`, ...defs.map((d) => usd(d.values[k]))]), defs.map((_, k) => k + 1));

  // AAL by scenario for this ZIP
  const vals = [];
  const order = scenOrder();
  for (const sid of order) vals.push((await getMetrics(sid, S.peril))[i * NCOL + 0]);
  $("zipScenTitle").textContent = "Expected annual loss by scenario" + (adaptSuffix() ? " · raised defences" : "");
  scenarioBars($("chartZipScen"), SCEN_CATS, vals, order.indexOf(scenId()), usd);
  tableView($("cardZipScen").querySelector(".tablev"), ["Scenario", "Expected annual loss"],
    SCEN_ORDER.map((sid, k) => [SCEN_CATS[k].replace("\n", " · "), usd(vals[k])]), [1]);

  const f = $("zipFacts"); f.replaceChildren();
  const add = (k, v) => { const a = document.createElement("span"); a.textContent = k; const b = document.createElement("b"); b.textContent = v; f.append(a, b); };
  add("Residential buildings", count(Z.nb[i]));
  add("Housing units", count(Z.nu[i]));
  add("Replacement value (structure + contents)", usd(Z.val[i]));
  add("Homes flooded at 1-in-100 (this view)", pct(at(6)));
}

// ------------------------------------------------------------------ boot
async function boot() {
  initTheme();
  readHash();
  [META, Z] = await Promise.all([fetchRetry("data/meta.json").then((r) => r.json()),
                                 fetchRetry("data/zips.json").then((r) => r.json())]);
  Z.zip.forEach((z, i) => zipIndex.set(z, i));
  $("versionChip").textContent = `v${META.version}`;
  if (!(META.defences || []).some((d) => d.id === "adapt")) { const g = $("grpDefence"); if (g) g.hidden = true; S.def = "fixed"; }
  initControls();
  syncControls();
  initMap();
  renderPanel();
}
boot().catch((e) => {
  console.error(e);
  const l = $("loading"); l.querySelector("span:last-child").textContent = "Could not load data — see console";
});
