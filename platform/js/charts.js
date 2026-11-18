// Chart builders (ECharts) following the dataviz mark specs:
// thin bars (<=24px, 4px rounded data-end), 2px lines, hairline solid grid,
// emphasis = selected bar in accent, others de-emphasised; text never wears series color.
import { usd } from "./format.js";

export function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function base() {
  return {
    animationDuration: 350,
    textStyle: { fontFamily: css("--font") || "Inter, system-ui, sans-serif", color: css("--text-secondary") },
    grid: { left: 52, right: 14, top: 18, bottom: 34, containLabel: false },
    tooltip: {
      backgroundColor: css("--glass-strong"), borderColor: css("--border"), borderWidth: 1,
      padding: [8, 10], textStyle: { color: css("--text-primary"), fontSize: 12 },
      extraCssText: "border-radius:10px; box-shadow:" + css("--shadow") + ";",
    },
  };
}

const charts = new Map();
function chart(el) {
  let c = charts.get(el);
  if (!c) {
    c = echarts.init(el, null, { renderer: "svg" });
    charts.set(el, c);
    new ResizeObserver(() => c.resize()).observe(el);
  }
  return c;
}

/** Emphasis column chart: one series, selected bar in accent, rest de-emphasised. */
export function scenarioBars(el, cats, values, selectedIdx, fmt = usd) {
  const c = chart(el);
  const accent = css("--accent"), de = css("--deemph");
  const opt = base();
  opt.grid = { left: 56, right: 10, top: 22, bottom: 44 };
  opt.xAxis = {
    type: "category", data: cats,
    axisLine: { lineStyle: { color: css("--baseline") } }, axisTick: { show: false },
    axisLabel: { color: css("--text-secondary"), fontSize: 10.5, interval: 0, lineHeight: 13 },
  };
  opt.yAxis = {
    type: "value", splitNumber: 3,
    axisLabel: { color: css("--text-muted"), fontSize: 10.5, formatter: (v) => fmt(v) },
    splitLine: { lineStyle: { color: css("--grid"), width: 1, type: "solid" } },
  };
  opt.tooltip.trigger = "item";
  opt.tooltip.formatter = (p) =>
    `<div style="font-weight:650;font-size:14px">${fmt(p.value)}</div><div style="color:${css("--text-secondary")}">${p.name.replace("\n", " · ")}</div>`;
  opt.series = [{
    type: "bar", barMaxWidth: 24, barCategoryGap: "45%",
    data: values.map((v, i) => ({
      value: v,
      itemStyle: { color: i === selectedIdx ? accent : de, borderRadius: [4, 4, 0, 0] },
      label: { show: i === selectedIdx, position: "top", color: css("--text-primary"), fontWeight: 600, fontSize: 11, formatter: (p) => fmt(p.value) },
    })),
    emphasis: { itemStyle: { opacity: 0.85 } },
  }];
  c.setOption(opt, true);
  return c;
}

/** Loss-exceedance curve: loss vs return period (log axis), perils + combined. */
export function epCurve(el, rps, seriesDefs) {
  const c = chart(el);
  const opt = base();
  opt.grid = { left: 56, right: 70, top: 30, bottom: 36 };
  // a single series needs no legend box (the card title names it)
  opt.legend = {
    show: seriesDefs.length > 1, top: 0, left: 0, itemWidth: 14, itemHeight: 3, icon: "rect",
    textStyle: { color: css("--text-secondary"), fontSize: 11 }, data: seriesDefs.map((s) => s.name),
  };
  if (seriesDefs.length <= 1) opt.grid.top = 14;
  opt.xAxis = {
    type: "log", logBase: 10, min: 2, max: 1000, name: "Return period (years)", nameLocation: "middle", nameGap: 22,
    nameTextStyle: { color: css("--text-muted"), fontSize: 10.5 },
    axisLine: { lineStyle: { color: css("--baseline") } },
    axisLabel: { color: css("--text-muted"), fontSize: 10.5, formatter: (v) => ([2, 10, 100, 1000].includes(Math.round(v)) ? Math.round(v) : "") },
    splitLine: { show: false }, minorTick: { show: false },
  };
  opt.yAxis = {
    type: "value", splitNumber: 3,
    axisLabel: { color: css("--text-muted"), fontSize: 10.5, formatter: (v) => usd(v) },
    splitLine: { lineStyle: { color: css("--grid"), width: 1, type: "solid" } },
  };
  opt.tooltip.trigger = "axis";
  opt.tooltip.axisPointer = { type: "line", lineStyle: { color: css("--baseline"), width: 1 } };
  opt.tooltip.formatter = (ps) => {
    const T = Math.round(ps[0].axisValue);
    const rows = ps.map((p) =>
      `<div style="display:flex;align-items:center;gap:8px;margin-top:3px"><i style="width:12px;height:2px;background:${p.color};display:inline-block;border-radius:1px"></i><b style="min-width:58px">${usd(p.value[1])}</b><span style="color:${css("--text-secondary")}">${p.seriesName}</span></div>`).join("");
    return `<div style="color:${css("--text-muted")};font-size:11px">1-in-${T}-year flood</div>${rows}`;
  };
  // direct end-labels only where they do not collide (the legend carries the rest)
  const ends = seriesDefs.map((s) => s.values[s.values.length - 1] || 0);
  const ymax = Math.max(...ends, 1);
  const order = ends.map((v, i) => [v, i]).sort((a, b) => b[0] - a[0]);
  const showLabel = new Array(seriesDefs.length).fill(false);
  let last = Infinity;
  for (const [v, i] of order) {
    if (last - v > 0.07 * ymax) { showLabel[i] = true; last = v; }
  }
  opt.series = seriesDefs.map((s, i) => ({
    name: s.name, type: "line", data: rps.map((T, k) => [T, s.values[k]]),
    lineStyle: { width: 2, color: s.color }, itemStyle: { color: s.color, borderColor: css("--surface-1"), borderWidth: 2 },
    showSymbol: false, symbolSize: 8, smooth: false, z: s.z || 2,
    endLabel: { show: showLabel[i], color: css("--text-secondary"), fontSize: 10.5, formatter: s.name, distance: 6 },
    emphasis: { focus: "series" },
  }));
  c.setOption(opt, true);
  return c;
}

export function rerenderAll() {
  for (const c of charts.values()) c.resize();
}

/** Simple table view for the accessibility twin of a chart. */
export function tableView(container, headers, rows, numericCols = []) {
  const t = document.createElement("table");
  const thead = t.createTHead().insertRow();
  headers.forEach((h, i) => {
    const th = document.createElement("th");
    th.textContent = h;
    if (numericCols.includes(i)) th.className = "num";
    thead.appendChild(th);
  });
  const tb = t.createTBody();
  rows.forEach((r) => {
    const tr = tb.insertRow();
    r.forEach((v, i) => {
      const td = tr.insertCell();
      td.textContent = v;
      if (numericCols.includes(i)) td.className = "num";
    });
  });
  container.replaceChildren(t);
}
