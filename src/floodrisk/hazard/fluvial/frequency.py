"""Flood-frequency analysis of annual-maximum series (vectorised log-Pearson III).

Follows the Bulletin 17B logic (USGS/IACWD 1982) in a form that scales to millions of
reaches:
  * zero / missing years are handled with the conditional-probability adjustment;
  * low outliers are screened with the Grubbs-Beck test (10 % significance);
  * station skew is weighted with a regional (generalised) skew using the B17B MSE formulae.
The regional skew is supplied by the caller (e.g. the median station skew per HUC-4).
"""
from __future__ import annotations

import numpy as np
from scipy import stats

MSE_REGIONAL_SKEW = 0.302  # Bulletin 17B national generalised-skew map


def _station_skew(x: np.ndarray, n: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Row-wise mean, std (ddof=1) and bias-corrected skew of log flows (NaN-aware)."""
    m = np.nanmean(x, axis=1)
    d = x - m[:, None]
    s = np.sqrt(np.nansum(d ** 2, axis=1) / np.maximum(n - 1, 1))
    with np.errstate(invalid="ignore", divide="ignore"):
        g = (n / ((n - 1) * (n - 2))) * np.nansum((d / s[:, None]) ** 3, axis=1)
    g[(n < 3) | ~np.isfinite(g)] = 0.0
    return m, s, g


def grubbs_beck_kn(n: np.ndarray) -> np.ndarray:
    """One-sided 10 % Grubbs-Beck critical value (B17B approximation)."""
    ln = np.log10(np.maximum(n, 3))
    return -0.9043 + 3.345 * np.sqrt(ln) - 0.4046 * ln


def mse_station_skew(g: np.ndarray, n: np.ndarray) -> np.ndarray:
    ag = np.abs(g)
    a = np.where(ag <= 0.9, -0.33 + 0.08 * ag, -0.52 + 0.30 * ag)
    b = np.where(ag <= 1.5, 0.94 - 0.26 * ag, 0.55)
    return 10 ** (a - b * np.log10(np.maximum(n, 3) / 10.0))


def lp3_fit(q: np.ndarray, regional_skew: np.ndarray | float = 0.0) -> dict[str, np.ndarray]:
    """Fit LP3 to each row of ``q`` (n_sites x n_years, m3/s; NaN = missing).

    Returns arrays: n, n_zero, n_low, mean, std, skew_station, skew_weighted, p_pos.
    ``p_pos`` is the probability that the annual peak is a (non-censored) positive flow.
    """
    q = np.asarray(q, dtype=np.float64)
    finite = np.isfinite(q)
    n_tot = finite.sum(axis=1).astype(np.float64)
    pos = finite & (q > 0)
    x = np.where(pos, np.log10(np.where(pos, q, 1.0)), np.nan)
    n = pos.sum(axis=1).astype(np.float64)

    # first pass -> Grubbs-Beck low-outlier screen
    m, s, _ = _station_skew(x, n)
    x_low = m - grubbs_beck_kn(n) * s
    low = pos & (x < x_low[:, None])
    x = np.where(low, np.nan, x)
    n2 = np.isfinite(x).sum(axis=1).astype(np.float64)

    m, s, g = _station_skew(x, n2)
    rg = np.broadcast_to(np.asarray(regional_skew, dtype=np.float64), g.shape)
    mse_s = mse_station_skew(g, n2)
    gw = (MSE_REGIONAL_SKEW * g + mse_s * rg) / (MSE_REGIONAL_SKEW + mse_s)
    gw = np.clip(gw, -2.0, 2.0)
    p_pos = np.where(n_tot > 0, n2 / np.maximum(n_tot, 1), 0.0)
    return {"n": n_tot, "n_zero": n_tot - n, "n_low": low.sum(axis=1).astype(np.float64),
            "mean": m, "std": s, "skew_station": g, "skew_weighted": gw, "p_pos": p_pos}


def lp3_quantiles(fit: dict[str, np.ndarray], return_periods: list[float]) -> np.ndarray:
    """Flow quantiles (n_sites x n_rp) for annual exceedance probabilities 1/T."""
    out = np.zeros((fit["mean"].shape[0], len(return_periods)), dtype=np.float64)
    p_pos = np.clip(fit["p_pos"], 1e-9, 1.0)
    for k, T in enumerate(return_periods):
        aep = 1.0 / T
        cond = aep / p_pos                      # conditional AEP among positive years
        ok = cond < 1.0
        kt = stats.pearson3.ppf(1.0 - np.clip(cond, 1e-12, 1 - 1e-12), fit["skew_weighted"])
        lq = fit["mean"] + kt * fit["std"]
        out[:, k] = np.where(ok & np.isfinite(lq), 10 ** lq, 0.0)
    # enforce monotonic increase with T (negative-skew upper bounds, numerical noise)
    return np.maximum.accumulate(np.nan_to_num(out, nan=0.0), axis=1)
