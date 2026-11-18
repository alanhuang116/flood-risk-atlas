// Shared script for the document pages (theme + markdown rendering + data page)
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("fra-theme"); } catch (_) { /* storage unavailable */ }
  const t = saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", t);
  document.addEventListener("DOMContentLoaded", () => {
    const b = document.getElementById("themeToggle");
    if (!b) return;
    b.addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("fra-theme", next); } catch (_) { /* ignore */ }
    });
  });
})();

function slug(s) {
  return s.toLowerCase().replace(/[^\w]+/g, "-").replace(/^-|-$/g, "");
}

async function loadDoc(url) {
  const doc = document.getElementById("doc");
  const toc = document.getElementById("toc");
  const md = await (await fetch(url)).text();
  // markdown authored in this repo (trusted); rendered with marked
  doc.innerHTML = marked.parse(md, { mangle: false, headerIds: false });
  const heads = doc.querySelectorAll("h2, h3");
  const title = document.createElement("div");
  title.className = "toc-title"; title.textContent = "Contents";
  toc.appendChild(title);
  heads.forEach((h) => {
    h.id = slug(h.textContent);
    const a = document.createElement("a");
    a.href = "#" + h.id; a.textContent = h.textContent; a.className = h.tagName.toLowerCase();
    toc.appendChild(a);
  });
  const links = [...toc.querySelectorAll("a")];
  const obs = new IntersectionObserver((es) => {
    es.forEach((e) => {
      if (e.isIntersecting) {
        links.forEach((l) => l.classList.toggle("active", l.getAttribute("href") === "#" + e.target.id));
      }
    });
  }, { rootMargin: "0px 0px -70% 0px" });
  heads.forEach((h) => obs.observe(h));
}

function fmtBytes(n) {
  if (!(n > 0)) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i += 1; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
}

// data/downloads.json (scripts/15_package_release.py) lists the product files on the GitHub release;
// without it (local build) the data page links the CSV copied into data/downloads/.
async function loadRelease() {
  try {
    const r = await fetch("data/downloads.json");
    return r.ok ? await r.json() : null;
  } catch (_) { return null; }
}

async function buildDataPage() {
  const [meta, release] = await Promise.all([fetch("data/meta.json").then((r) => r.json()), loadRelease()]);
  const grid = document.getElementById("dlGrid");
  const items = release
    ? release.files.map((f) => [release.base_url + f.file, f.title,
        `${f.desc} (${[f.format, fmtBytes(f.bytes)].filter(Boolean).join(", ")})`, "Download"])
    : [["data/downloads/zip_flood_risk_long.csv", "ZIP flood risk — full table", "All ZIPs × 2 defence assumptions × 5 scenario-years × 4 flood types (CSV)", "Download CSV"]];
  items.push(
    ["data/downloads/data_dictionary.csv", "Data dictionary", "Field names, definitions and units (CSV)", "Download CSV"],
    ["methodology.html", "Methodology", "Hazard modelling, damage calculation, aggregation, assumptions", "Read"],
    ["docs/methodology.md", "Methodology (Markdown)", "Plain-text source of the methodology document", "Download"],
  );
  if (release) items.push([release.repo_url, "Source code", "Pipeline, model and platform code on GitHub", "Open"]);
  items.forEach(([href, t, d, c]) => {
    const a = document.createElement("a"); a.className = "dl"; a.href = href;
    const b = document.createElement("b"); b.textContent = t;
    const s = document.createElement("span"); s.textContent = d;
    const e = document.createElement("em"); e.textContent = c + " →";
    a.append(b, s, e); grid.appendChild(a);
  });
  const relTable = document.getElementById("release");
  [["Version", meta.version], ["Generated", meta.generated], ["Coverage", meta.coverage_label],
   ["ZIP codes", meta.zip_count.toLocaleString("en-US")], ["Exposure", meta.exposure], ["Price level", meta.price_level],
   ["Scenarios", meta.scenarios.map((s) => s.label).join(" · ")],
   ["Flood defences", (meta.defences || [{ label: "Fixed crest" }]).map((d) => d.label).join(" · ")],
   ["Return periods", meta.rps.join(", ") + " years"]]
    .forEach(([k, v]) => { const tr = relTable.insertRow(); tr.insertCell().textContent = k; tr.insertCell().textContent = v; });
  if (release) {
    const tr = relTable.insertRow();
    tr.insertCell().textContent = "Release files";
    const a = document.createElement("a");
    a.href = release.release_url; a.textContent = `GitHub release ${release.release} (all files, checksums, notes)`;
    tr.insertCell().appendChild(a);
  }
  try {
    const csv = await (await fetch("data/downloads/data_dictionary.csv")).text();
    const rows = csv.trim().split(/\r?\n/).map((l) => l.split(/,(?=(?:[^"]*"[^"]*")*[^"]*$)/).map((x) => x.replace(/^"|"$/g, "")));
    const t = document.createElement("table");
    const h = t.createTHead().insertRow();
    rows[0].forEach((x) => { const th = document.createElement("th"); th.textContent = x; h.appendChild(th); });
    const tb = t.createTBody();
    rows.slice(1).forEach((r) => { const tr = tb.insertRow(); r.forEach((x) => (tr.insertCell().textContent = x)); });
    document.getElementById("dict").appendChild(t);
  } catch (_) { /* dictionary optional */ }
  document.getElementById("cite").textContent =
    `Flood Risk Atlas (${meta.generated.slice(0, 4)}). US residential flood risk — climate-adjusted, ZIP-level estimates, version ${meta.version}. Exposure: USACE NSI 2026; prices: ${meta.price_level}.`;
}
