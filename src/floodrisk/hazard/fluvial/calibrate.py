"""Calibrate HAND synthetic rating curves to FEMA 1%-annual-chance flood zones.

HAND rating curves derived from a 30 m DEM miss most of the conveyance of narrow or engineered
channels and use a uniform Manning's n, so their stages are biased. For every fluvial reach we
find the 100-year stage that best reproduces FEMA's Special Flood Hazard Area (SFHA) among the
residential buildings draining to that reach:

    H*_r = argmax_H  CSI(H),   CSI = TP / (TP + FP + FN)
    TP = SFHA buildings with HAND_b < H,  FP = non-SFHA buildings with HAND_b < H,
    FN = SFHA buildings with HAND_b >= H

and a conveyance multiplier  k_r = Q100_eff / Q_SRC(H*_r),  so that the calibrated curve is
Q(H) = k_r * Q_SRC(H). Other return periods follow the NWM frequency curve and the SRC shape.
Q_SRC(H*) ("q_at") does not depend on the channel-capacity convention, so k can be recomputed
for any capacity parameters without redoing the CSI search (``recompute_k``).

Eligible buildings: on a mapped FIRM panel (zone A*, X; not V, D, unmapped or open water) and
not flooded by the coastal model at the 100-year level (coastal SFHA is not riverine).

Reach classes
  fema_csi     >= 25 eligible buildings, >= 1 in the SFHA: CSI-optimal stage (sparse SFHA included -
               a reach with 1-2 SFHA homes among hundreds is a low-risk reach, not an unknown one)
  fema_nosfha  >= 5 eligible buildings, none in the SFHA: stage just below the lowest building
  regional_da  all other reaches (few or no mapped buildings, e.g. unmapped panels): geometric median
               k of CSI-calibrated reaches of the same HUC-2 region and drainage-area class (k grows
               strongly with river size: ~0.15 for < 300 km2, ~1.5-2 for > 3,000 km2), national class
               median where the regional class has < 20 reaches.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from floodrisk.hazard.fluvial.capacity import capacity_flow

SFHA_ZONES = {"A", "AE", "AH", "AO", "A99", "AR"}
MAPPED_NON_SFHA = {"X"}
K_MIN, K_MAX = 0.1, 100.0
DEFAULT_K = 1.0
INFORMED = ("fema_csi", "fema_nosfha")
DA_BINS = np.array([0, 150, 500, 1500, 5000, 15000, 50000, np.inf])
MIN_PER_CELL = 20
COLS = ["comid", "k", "method", "n_elig", "n_sfha", "csi", "h100", "q_at"]


@lru_cache(maxsize=1)
def drainage_area() -> pd.Series:
    from floodrisk.config import path
    a = pd.read_parquet(path("interim", "nhdplus", "flowline_attributes.parquet"), columns=["comid", "totdasqkm"])
    return a.drop_duplicates("comid").set_index("comid").totdasqkm


def da_bin(da: np.ndarray) -> np.ndarray:
    return np.clip(np.digitize(np.nan_to_num(np.asarray(da, float), nan=50.0), DA_BINS) - 1, 0, len(DA_BINS) - 2)


def _best_stage(hand: np.ndarray, sfha: np.ndarray) -> tuple[float, float]:
    """Stage maximising CSI between {hand < H} and the SFHA flag."""
    o = np.argsort(hand)
    h, s = hand[o], sfha[o]
    n_s = s.sum()
    tp = np.cumsum(s)                 # including building i
    fp = np.cumsum(~s)
    fn = n_s - tp
    csi = tp / np.maximum(tp + fp + fn, 1)
    i = int(np.argmax(csi))
    if csi[i] <= 0:
        return float(h[0] - 0.1), 0.0
    nxt = h[i + 1] if i + 1 < len(h) else h[i] + 0.5
    return float(0.5 * (h[i] + nxt)), float(csi[i])


def _qcap(flows_idx: pd.DataFrame, comids, reach_dev, rps, tn, tu) -> np.ndarray:
    qT = flows_idx.loc[comids, [f"q{T}" for T in rps]].to_numpy(np.float64)
    dev = (reach_dev.reindex(comids).fillna(0.0).to_numpy(np.float64) if reach_dev is not None
           else np.zeros(len(comids)))
    return np.array([capacity_flow(dict(zip(rps, row)), dv, tn, tu) for row, dv in zip(qT, dev)])


def calibrate_reaches(fluvial_bld: pd.DataFrame, exposure: pd.DataFrame, src: pd.DataFrame,
                      flows: pd.DataFrame, qmean: pd.Series | None, coastal_bld: pd.DataFrame | None = None,
                      min_bld: int = 25, min_sfha: int = 1, reach_dev: pd.Series | None = None,
                      rps: list[int] | None = None, min_bld_nosfha: int = 5,
                      tc_natural: float | None = None, tc_urban: float | None = None,
                      fallback: "FallbackTable | None" = None, huc2: str | None = None,
                      informed_only: bool = False) -> pd.DataFrame:
    """Per-reach conveyance multipliers (comid, k, method, n_elig, n_sfha, csi, h100, q_at)."""
    if fluvial_bld is None or src is None or len(src) == 0:
        return pd.DataFrame(columns=COLS)
    rps = rps or [2, 5, 10, 25, 50, 100, 250, 500, 1000]
    d = fluvial_bld.merge(exposure[["fd_id", "firmzone", "zone_sub"]], on="fd_id", how="left")
    fz = d.firmzone.fillna("").str.upper().str.strip()
    zs = d.zone_sub.fillna("").str.upper()
    mapped = fz.isin(SFHA_ZONES | MAPPED_NON_SFHA)
    elig = mapped & (d.comid > 0) & np.isfinite(d.hand_b_m)
    if coastal_bld is not None and "baseline_rp100" in coastal_bld:
        cz = d.fd_id.map(coastal_bld.drop_duplicates("fd_id").set_index("fd_id")["baseline_rp100"]).fillna(0.0)
        elig &= cz <= 0
    d = d[elig].copy()
    d["sfha"] = fz[elig].isin(SFHA_ZONES).to_numpy() & ~zs[elig].str.contains("REDUCED FLOOD RISK DUE TO LEVEE").to_numpy()

    fl = flows.drop_duplicates("feature_id").set_index("feature_id")
    src_g = {c: (g.q_cms.to_numpy(), g.stage_m.to_numpy()) for c, g in src.groupby("comid")}
    rows = []
    for cid, g in d.groupby("comid"):
        if cid not in src_g or cid not in fl.index:
            continue
        hand = g.hand_b_m.to_numpy(np.float64)
        sf = g.sfha.to_numpy(bool)
        if sf.sum() >= min_sfha and len(g) >= min_bld:
            method = "fema_csi"
            h_star, csi = _best_stage(hand, sf)
        elif sf.sum() == 0 and len(g) >= min_bld_nosfha:
            method = "fema_nosfha"                                   # FEMA: no residential SFHA here
            h_star, csi = float(np.min(hand) - 0.1), np.nan
        else:
            continue
        qs, hs = src_g[cid]
        q_at = float(np.interp(max(h_star, 0.0), hs, qs))
        rows.append((cid, np.nan, method, len(g), int(sf.sum()), csi, h_star, q_at))
    cal = pd.DataFrame(rows, columns=COLS)
    cal = recompute_k(cal, flows, reach_dev, rps, tc_natural, tc_urban)
    if informed_only:
        return cal
    others = np.array(sorted(set(src.comid.unique()) - set(cal.comid)), dtype=np.int64)
    if len(others):
        if fallback is not None:
            k_o, meth = fallback.k_for(others, huc2), "regional_da"
        else:
            good = cal[cal.method.isin(INFORMED)]
            k_reg = float(np.exp(np.median(np.log(good.k)))) if len(good) >= 3 else DEFAULT_K
            k_o, meth = np.full(len(others), k_reg), ("huc8_median" if len(good) >= 3 else "default")
        cal = pd.concat([cal, pd.DataFrame({"comid": others, "k": k_o, "method": meth, "n_elig": 0, "n_sfha": 0,
                                            "csi": np.nan, "h100": np.nan, "q_at": np.nan})], ignore_index=True)
    return cal


def recompute_k(cal: pd.DataFrame, flows: pd.DataFrame, reach_dev: pd.Series | None, rps,
                tc_natural: float | None, tc_urban: float | None) -> pd.DataFrame:
    """k = (Q100 - Q_cap(Tn, Tu)) / Q_SRC(H*) for FEMA-informed reaches (vectorised over reaches)."""
    cal = cal.copy()
    m = cal.method.isin(INFORMED).to_numpy() & cal.q_at.notna().to_numpy()
    if not m.any():
        return cal
    fl = flows.drop_duplicates("feature_id").set_index("feature_id")
    comids = cal.comid.to_numpy()[m]
    q100 = fl.loc[comids, "q100"].to_numpy(np.float64)
    qcap = np.nan_to_num(_qcap(fl, comids, reach_dev, rps, tc_natural, tc_urban))
    qeff = np.clip(q100 - qcap, 0.0, None)
    q_at = cal.q_at.to_numpy(np.float64)[m]
    k = np.where(q_at > 0, qeff / np.maximum(q_at, 1e-9), K_MAX)
    k = np.where(qeff <= 0, K_MAX, k)                 # all floods stay in the channel
    cal.loc[m, "k"] = np.clip(k, K_MIN, K_MAX)
    return cal


class FallbackTable:
    """Geometric-median k of FEMA-informed reaches by (HUC-2, drainage-area class)."""

    def __init__(self, informed: pd.DataFrame):
        """informed: national table with comid, k, method, huc2 (all FEMA-informed reaches)."""
        # CSI reaches only: a no-SFHA reach gives just a lower bound on k (smaller the higher its homes sit)
        g = informed[(informed.method == "fema_csi") & np.isfinite(informed.k)].copy()
        g["bin"] = da_bin(drainage_area().reindex(g.comid).to_numpy())
        g["lk"] = np.log(g.k.clip(K_MIN, K_MAX))
        reg = g.groupby(["huc2", "bin"]).lk.agg(["median", "size"])
        nat = g.groupby("bin").lk.median()
        self.national = {int(b): float(np.exp(v)) for b, v in nat.items()}
        self.regional = {(str(h), int(b)): float(np.exp(r["median"])) for (h, b), r in reg.iterrows()
                         if r["size"] >= MIN_PER_CELL}
        self.summary = (reg.assign(k_med=np.exp(reg["median"])).drop(columns="median").reset_index())

    def k_for(self, comids, huc2: str | None) -> np.ndarray:
        b = da_bin(drainage_area().reindex(np.asarray(comids)).to_numpy())
        return np.array([self.regional.get((str(huc2), int(x)), self.national.get(int(x), DEFAULT_K)) for x in b])


def complete_calibration(cal_informed: pd.DataFrame, src_comids, huc2: str, table: FallbackTable) -> pd.DataFrame:
    """Add regional drainage-area fallback k for the reaches without FEMA information."""
    others = np.array(sorted(set(np.asarray(src_comids, np.int64)) - set(cal_informed.comid)), dtype=np.int64)
    if not len(others):
        return cal_informed
    add = pd.DataFrame({"comid": others, "k": table.k_for(others, huc2), "method": "regional_da", "n_elig": 0,
                        "n_sfha": 0, "csi": np.nan, "h100": np.nan, "q_at": np.nan})
    return pd.concat([cal_informed, add], ignore_index=True)


@lru_cache(maxsize=1)
def informed_national() -> pd.DataFrame:
    """All FEMA-informed reaches (output of scripts/09a_calibrate_fluvial.py)."""
    from floodrisk.config import path
    fp = path("interim", "hazard", "fluvial", "calib_raw_national.parquet")
    return pd.read_parquet(fp)


@lru_cache(maxsize=1)
def reach_dev_national() -> pd.Series:
    import glob
    from floodrisk.config import path
    files = glob.glob(str(path("interim", "hazard", "fluvial", "reaches") / "*.parquet"))
    r = pd.concat([pd.read_parquet(f, columns=["comid", "dev_frac"]) for f in files], ignore_index=True)
    return r.drop_duplicates("comid").set_index("comid").dev_frac


@lru_cache(maxsize=8)
def fallback_table(tc_natural: float, tc_urban: float) -> FallbackTable:
    from floodrisk import pipeline
    from floodrisk.config import return_periods
    cal = recompute_k(informed_national(), pipeline.flood_quantiles(), reach_dev_national(), return_periods(),
                      tc_natural, tc_urban)
    return FallbackTable(cal)
