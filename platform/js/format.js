// Number formatting helpers (compact USD, percentages, counts)

export function usd(v, { digits = 1 } = {}) {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  const sign = v < 0 ? "−" : "";
  if (a >= 1e12) return `${sign}$${trim((a / 1e12).toFixed(digits))}T`;
  if (a >= 1e9) return `${sign}$${trim((a / 1e9).toFixed(digits))}B`;
  if (a >= 1e6) return `${sign}$${trim((a / 1e6).toFixed(digits))}M`;
  if (a >= 1e3) return `${sign}$${trim((a / 1e3).toFixed(digits))}K`;
  if (a === 0) return "$0";
  return `${sign}$${Math.round(a)}`;
}

export function count(v) {
  if (v == null || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return `${trim((v / 1e9).toFixed(1))}B`;
  if (a >= 1e6) return `${trim((v / 1e6).toFixed(1))}M`;
  if (a >= 1e4) return `${trim((v / 1e3).toFixed(0))}K`;
  return Math.round(v).toLocaleString("en-US");
}

/** fraction (0.0012) -> "0.12%" with adaptive precision */
export function pct(f, { min = 1, max = 3 } = {}) {
  if (f == null || !isFinite(f)) return "—";
  const p = f * 100;
  const a = Math.abs(p);
  if (a === 0) return "0%";
  let d = a >= 10 ? 0 : a >= 1 ? 1 : a >= 0.1 ? 2 : 3;
  d = Math.max(min === 0 ? 0 : Math.min(d, max), Math.min(d, max));
  if (a < 0.001) return "<0.001%";
  return `${trim(p.toFixed(d))}%`;
}

export function signedPct(f) {
  if (f == null || !isFinite(f)) return "—";
  const p = f * 100;
  const s = p > 0 ? "+" : p < 0 ? "−" : "±";
  const a = Math.abs(p);
  return `${s}${a >= 10 ? a.toFixed(0) : a.toFixed(1)}%`;
}

export function metricValue(v, unit) {
  return unit === "usd" ? usd(v) : unit === "pct" ? pct(v) : count(v);
}

function trim(s) {
  return s.includes(".") ? s.replace(/\.?0+$/, "") : s;
}
