"""Building-level flood losses -> AAL and return-period losses, per peril and scenario-year.

For every residential building b, scenario s and return period T:
  fluvial  : Q = q_T(reach) * f5_s ;  Q_eff = max(Q - Q_mean, 0) ;  H = SRC_reach^-1(Q_eff)
             d = max(0, H - HAND_b)
  pluvial  : P* = P_T(b) * f1_s ; d = interp(P*, P_runs(b), d_runs(b))   (11 GPU runs)
  coastal  : d = depth table from the connected-bathtub model (storm tide + SLR_s)
  combined : d = max over perils (damage curve of the peril giving the max depth)
Flood defences (fluvial, coastal): behind a defence of standard T_p the building stays dry while the
depth does not exceed the baseline depth of the T_p event (fixed crest = no adaptation); beyond that
it floods as if undefended. With ``adapt_variant`` the raised-crest variant (the standard T_p is kept
in every scenario) is computed as well for the defended buildings.
Loss = DDF_struct(d_ff) * V_struct + DDF_cont(d_ff) * V_cont, with d_ff = d[ft] - first-floor height,
zero loss when d = 0, and no below-first-floor damage while the ground depth is < below_ffe_min_depth_m.
Post-FIRM elevation (``postfirm_elevation``): an SFHA home is post-FIRM with probability p_post (NFIP
policies, scripts/04b); post-FIRM homes have no basement and their first floor at BFE + e, with e in
three equally likely classes of (lowest floor - BFE); the loss is the expectation over both states.
Damage ratios are scaled by the national vulnerability factor and, with ``regional_factors``, by the
state calibration factor (config/regional_factors.csv, scripts/14_calibrate_nfip.py --regional).
Values are full replacement costs (NSI, USD Aug-2025 price level).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from floodrisk.config import PROJECT_ROOT, path, return_periods, settings
from floodrisk.hazard.climate import precip_factor, sample_grid, scenario_list
from floodrisk.hazard.fluvial.capacity import ANCHOR_NATURAL, ANCHOR_URBAN, capacity_flow
from floodrisk.risk.metrics import aal_trapezoid
from floodrisk.vulnerability.ddf import DamageFunctions

log = logging.getLogger(__name__)
FT = 3.280839895
PERILS = ("fluvial", "pluvial", "coastal")
PLUVIAL_MIN_DEPTH_M = 0.10
ATLAS_RPS = [2, 5, 10, 25, 50, 100, 200, 500, 1000]
GPU_DRAIN_RP = 5                     # storm-drain standard already applied inside the GPU runs


@dataclass
class RiskParams:
    """Global model parameters (config: vulnerability / risk.protection / risk.calibration)."""
    fluvial_tc_natural: float = 2.0
    fluvial_tc_urban: float = 10.0
    pluvial_drain_rp: float = 5.0
    below_ffe_min_depth_m: float = 0.0
    vulnerability_factor: float = 1.0        # global multiplier on damage ratios (NFIP-calibrated)
    fema_levee_rp: float = 100.0
    nld_accredited_rp: float = 100.0
    nld_other_rp: float = 25.0
    coastal_implicit_rp: float = 100.0
    coastal_implicit_min_depth_m: float = 0.5
    adapt_with_climate: bool = False
    adapt_variant: bool = False              # also compute the raised-crest variant (future scenarios)
    postfirm_elevation: bool = False         # post-FIRM SFHA homes elevated to the BFE (NFIP rule)
    regional_factors: bool = False           # state calibration factors (config/regional_factors.csv)

    @classmethod
    def from_settings(cls, **override) -> "RiskParams":
        s = settings()
        r = s.get("risk", {})
        kw = {}
        kw.update({k: v for k, v in (r.get("calibration") or {}).items() if k in cls.__dataclass_fields__})
        kw.update({k: v for k, v in (r.get("protection") or {}).items() if k in cls.__dataclass_fields__})
        v = s.get("vulnerability", {})
        for key in ("below_ffe_min_depth_m", "vulnerability_factor", "postfirm_elevation"):
            if key in v:
                kw[key] = v[key]
        kw.update(override)
        return cls(**kw)


def load_elevation_classes() -> dict | None:
    """zone ('A'/'V') -> (elevation of post-FIRM lowest floors above the BFE (ft), weights); scripts/04b."""
    fp = path("interim", "postfirm", "elevation_classes.json")
    if not fp.exists():
        return None
    m = json.loads(fp.read_text())["classes"]
    return {z: (np.asarray(v["elev_ft"], float), np.asarray(v["weight"], float)) for z, v in m.items()}


def load_regional_factors() -> pd.DataFrame | None:
    """state_fips -> c_inland (fluvial + pluvial), c_coastal; written by scripts/14 --regional --apply."""
    fp = PROJECT_ROOT / "config" / "regional_factors.csv"
    if not fp.exists():
        return None
    t = pd.read_csv(fp, dtype={"state_fips": str})
    return t.set_index("state_fips")[["c_inland", "c_coastal"]].astype(float)


def _interp_log_T(D: np.ndarray, rps, T: np.ndarray) -> np.ndarray:
    """Per-row interpolation of D (n, nrp) at return period T (n,) in log(T)."""
    lt = np.log(np.asarray(rps, float))
    x = np.log(np.clip(T, rps[0], rps[-1]))
    j = np.clip(np.searchsorted(lt, x, side="right") - 1, 0, len(lt) - 2)
    w = (x - lt[j]) / (lt[j + 1] - lt[j])
    r = np.arange(len(x))
    return D[r, j] * (1 - w) + D[r, j + 1] * w


def apply_protection(D: np.ndarray, T_prot: np.ndarray | None, rps, base_idx: int = 0,
                     adapt: bool = False) -> np.ndarray:
    """Zero depths that stay below the defence crest (= depth of the T_prot event)."""
    if T_prot is None:
        return D
    T_prot = np.asarray(T_prot, float)
    has = np.isfinite(T_prot) & (T_prot > 0)
    if not has.any():
        return D
    D = D.copy()
    idx = np.flatnonzero(has)
    if adapt:          # crest raised with the hazard: same standard in every scenario
        for j in range(D.shape[1]):
            hp = _interp_log_T(D[idx, j, :], rps, T_prot[idx])
            sub = D[idx, j, :]
            D[idx, j, :] = np.where(sub <= hp[:, None] + 1e-6, 0.0, sub)
    else:              # crest fixed at today's design level
        hp = _interp_log_T(D[idx, base_idx, :], rps, T_prot[idx])
        sub = D[idx]
        D[idx] = np.where(sub <= hp[:, None, None] + 1e-6, 0.0, sub)
    return D


def defence_failure_p(Du: np.ndarray, T_prot: np.ndarray, rps, base_idx: int = 0, adapt: bool = False,
                      floor_rp: float = 2.0) -> np.ndarray:
    """Annual exceedance probability at which the defence of each building fails, per scenario.

    Du: (k, ns, nr) undefended depths. The crest is the baseline (or, with ``adapt``, the scenario) depth of
    the T_prot event; failure happens where the depth curve first exceeds the crest (linear in AEP between
    return periods, consistent with the trapezoidal AAL). Returns p (k, ns): 1/floor_rp = no protection,
    0 = holds beyond the largest return period."""
    rps = np.asarray(rps, float)
    p = 1.0 / rps
    k, ns, nr = Du.shape
    T_prot = np.asarray(T_prot, float)
    out = np.full((k, ns), 1.0 / floor_rp)
    has = np.isfinite(T_prot) & (T_prot > 0)
    if not has.any():
        return out
    for j in range(ns):
        ref = Du[:, j if adapt else base_idx, :]
        crest = _interp_log_T(ref, rps, np.where(has, T_prot, rps[0]))           # (k,)
        d = Du[:, j, :]
        above = d > crest[:, None] + 1e-6                                       # (k, nr), T ascending
        first = np.where(above.any(axis=1), above.argmax(axis=1), nr)          # first RP index above crest
        pe = np.zeros(k)
        mid = (first > 0) & (first < nr)
        i1 = np.clip(first, 1, nr - 1)
        i0 = i1 - 1
        r = np.arange(k)
        d0, d1 = d[r, i0], d[r, i1]
        w = np.clip((crest - d0) / np.maximum(d1 - d0, 1e-9), 0, 1)
        pe = np.where(mid, p[i0] + w * (p[i1] - p[i0]), pe)
        pe = np.where(first == 0, 1.0 / floor_rp, pe)                           # fails already at the 2-yr
        out[:, j] = np.where(has, np.minimum(pe, 1.0 / floor_rp), 1.0 / floor_rp)
    return out


def aal_defended(rps, floor_rp: float, curves: dict, p_fail: dict) -> dict:
    """Exact AAL behind defences: piecewise-linear loss curves in AEP with a jump where a defence fails.

    curves: peril -> (D, L) undefended depth and loss (k, ns, nr) at the return periods;
    p_fail: peril -> (k, ns) failure AEP for defended perils (perils not listed are undefended).
    Returns peril -> AAL (k, ns) for every peril in ``curves`` plus "combined" (max-depth peril)."""
    T = np.asarray(rps, float)
    keep = T >= floor_rp
    pn = (1.0 / T[keep])[::-1]                                  # ascending AEP (rare -> frequent)
    P0 = np.concatenate([[0.0], pn])                             # p = 0 anchors the hold-constant tail
    perils = list(curves)
    any_c = next(iter(curves.values()))[0]
    k, ns = any_c.shape[:2]

    def nodes(X):
        X = X[..., keep][..., ::-1]
        return np.concatenate([X[..., :1], X], axis=-1)          # (k, ns, m+1)

    eps = 1e-9
    extra = []
    for q, pf in p_fail.items():
        pf = np.clip(pf, 0.0, P0[-1])
        extra += [pf * (1 - eps), np.minimum(pf * (1 + eps) + (pf == 0) * eps * 1e-3, P0[-1])]
    base = np.broadcast_to(P0, (k, ns, len(P0)))
    Pall = np.concatenate([base] + [e[..., None] for e in extra], axis=-1) if extra else base.copy()
    j = np.clip(np.searchsorted(P0, Pall, side="right") - 1, 0, len(P0) - 2)
    w = (Pall - P0[j]) / (P0[j + 1] - P0[j])

    def at(X):
        V = nodes(X)
        a = np.take_along_axis(V, j, axis=-1)
        b = np.take_along_axis(V, j + 1, axis=-1)
        return a * (1 - w) + b * w

    order = np.argsort(Pall, axis=-1, kind="stable")
    Ps = np.take_along_axis(Pall, order, axis=-1)
    dP = np.diff(Ps, axis=-1)
    Ds, Ls = {}, {}
    for q in perils:
        D, L = at(curves[q][0]), at(curves[q][1])
        if q in p_fail:
            off = Pall > p_fail[q][..., None]                    # more frequent than failure -> defended
            D, L = np.where(off, 0.0, D), np.where(off, 0.0, L)
        Ds[q] = np.take_along_axis(D, order, axis=-1)
        Ls[q] = np.take_along_axis(L, order, axis=-1)
    trap = lambda Lc: np.sum(0.5 * (Lc[..., :-1] + Lc[..., 1:]) * dP, axis=-1)  # noqa: E731
    out = {q: trap(Ls[q]) for q in perils}
    st = np.stack([Ds[q] for q in perils])
    arg = st.argmax(axis=0)
    Lc = np.take_along_axis(np.stack([Ls[q] for q in perils]), arg[None], axis=0)[0]
    out["combined"] = trap(Lc)
    return out


@dataclass
class Components:
    exposure: pd.DataFrame                    # residential buildings of the HUC-8
    fluvial_bld: pd.DataFrame | None = None   # fd_id, comid, hand_b_m
    src: pd.DataFrame | None = None           # comid, stage_m, q_cms
    flows: pd.DataFrame | None = None         # feature_id, q2..q1000
    qmean: pd.Series | None = None            # comid -> mean annual flow (m3/s)
    pluvial_bld: pd.DataFrame | None = None   # fd_id, d_T*, p_T*
    coastal_bld: pd.DataFrame | None = None   # fd_id, {scen}_rp{T}
    protect_T: np.ndarray | None = None       # per-building defence standard (yr) from FEMA levee zones / NLD
    src_k: pd.Series | None = None            # comid -> rating-curve conveyance multiplier (FEMA-calibrated)
    reach_dev: pd.Series | None = None        # comid -> developed fraction of the reach catchment
    params: RiskParams = field(default_factory=RiskParams)


def _align(df: pd.DataFrame | None, fd_id: np.ndarray) -> pd.DataFrame | None:
    if df is None or len(df) == 0:
        return None
    return df.drop_duplicates("fd_id").set_index("fd_id").reindex(fd_id)


def fluvial_depths(c: Components, scen, rps, lon, lat) -> np.ndarray:
    n = len(c.exposure)
    out = np.zeros((n, len(scen), len(rps)), dtype=np.float32)
    fb = _align(c.fluvial_bld, c.exposure.fd_id.values)
    if fb is None or c.src is None or c.flows is None or len(c.src) == 0:
        return out
    comid = fb.comid.fillna(0).to_numpy(np.int64)
    hand = fb.hand_b_m.to_numpy(np.float64)
    flows = c.flows.set_index("feature_id")
    qcols = [f"q{T}" for T in rps]
    f5 = np.stack([sample_grid(precip_factor("rx5day", ssp, yr), lon, lat) for _, ssp, yr in scen], axis=1)
    src_g = {k: (g.q_cms.to_numpy(), g.stage_m.to_numpy()) for k, g in c.src.groupby("comid")}
    for cid in np.unique(comid[comid > 0]):
        if cid not in src_g or cid not in flows.index:
            continue
        idx = np.flatnonzero(comid == cid)
        qsrc, hsrc = src_g[cid]
        qT = flows.loc[cid, qcols].to_numpy(np.float64)                 # (nrp,)
        # effective channel capacity Q(Tc), Tc = Tn (natural) .. Tu (urban), NFIP-calibrated; k is anchored
        # at the physical capacity (capacity.ANCHOR_*), so a larger Tc lowers every stage without touching k
        dev = float(c.reach_dev.get(cid, 0.0)) if c.reach_dev is not None else 0.0
        qm = capacity_flow(dict(zip(rps, qT)), dev, c.params.fluvial_tc_natural, c.params.fluvial_tc_urban)
        qm = qm if np.isfinite(qm) else 0.0
        fac = np.nanmean(f5[idx], axis=0)                                # (nscen,)
        k = float(c.src_k.get(cid, 1.0)) if c.src_k is not None else 1.0
        Q = np.clip(fac[:, None] * qT[None, :] - qm, 0.0, None) / k      # (nscen, nrp) on the raw SRC
        H = np.interp(Q, qsrc, hsrc)
        d = H[None, :, :] - hand[idx][:, None, None]
        out[idx] = np.clip(np.nan_to_num(d, nan=0.0), 0, None)
    return out


def pluvial_depths(c: Components, scen, rps, lon, lat) -> np.ndarray:
    n = len(c.exposure)
    out = np.zeros((n, len(scen), len(rps)), dtype=np.float32)
    pb = _align(c.pluvial_bld, c.exposure.fd_id.values)
    if pb is None:
        return out
    atlas = ATLAS_RPS
    P = pb[[f"p_T{T}" for T in atlas]].to_numpy(np.float64)            # (n, 9)
    D = pb[[c_ for c_ in pb.columns if c_.startswith("d_T")]].to_numpy(np.float64)  # (n, 11)
    n0 = np.zeros((n, 1))
    # (0 mm, 0 m) anchors the depth-rainfall curve below the 2-yr run (np.interp would clamp to d_T2)
    Pruns = np.concatenate([n0, P, P[:, -1:] * 1.15, P[:, -1:] * 1.30], axis=1)
    Druns = np.concatenate([n0, np.nan_to_num(D)], axis=1)
    lt_a, lt = np.log(atlas), np.log(np.asarray(rps, float))
    Pn = np.nan_to_num(P)
    PT = np.stack([np.interp(lt, lt_a, row) for row in Pn])            # (n, nrp) baseline P at our RPs
    # storm drains above the 5-yr standard of the GPU runs: remove the extra design rainfall
    td = float(c.params.pluvial_drain_rp)
    if td > GPU_DRAIN_RP:
        dP = np.array([np.interp(np.log(td), lt_a, row) for row in Pn]) - Pn[:, atlas.index(GPU_DRAIN_RP)]
    else:
        dP = np.zeros(n)
    f1 = np.stack([sample_grid(precip_factor("rx1day", ssp, yr), lon, lat) for _, ssp, yr in scen], axis=1)
    ok = np.isfinite(P).all(axis=1) & (P[:, 0] > 0)
    for i in np.flatnonzero(ok):
        tgt = f1[i][:, None] * PT[i][None, :] - dP[i]                   # (nscen, nrp)
        out[i] = np.interp(tgt, Pruns[i], Druns[i])
    # the wet/dry threshold must also hold after interpolating between runs (and climate scaling)
    out[out < PLUVIAL_MIN_DEPTH_M] = 0.0
    return out


def coastal_depths(c: Components, scen, rps) -> np.ndarray:
    n = len(c.exposure)
    out = np.zeros((n, len(scen), len(rps)), dtype=np.float32)
    cb = _align(c.coastal_bld, c.exposure.fd_id.values)
    if cb is None:
        return out
    for j, (sid, _, _) in enumerate(scen):
        cols = [f"{sid}_rp{T}" for T in rps]
        if all(col in cb.columns for col in cols):
            out[:, j, :] = np.nan_to_num(cb[cols].to_numpy(np.float32))
    return out


def coastal_mode(firmzone: pd.Series, zone_sub: pd.Series) -> np.ndarray:
    fz = firmzone.fillna("").str.upper()
    zs = zone_sub.fillna("").str.upper()
    return np.where(fz.str.startswith("V"), "coastal_v",
                    np.where(zs.str.contains("COASTAL"), "coastal_a", "coastal_still"))


class LossModel:
    def __init__(self, params: RiskParams | None = None):
        self.ddf = DamageFunctions()
        self.rps = return_periods()
        self.scen = scenario_list()
        r = settings()["risk"]
        self.floor_rp = float(r["frequent_event_floor_rp"])
        self.params = params or RiskParams.from_settings()
        self.elev = load_elevation_classes() if self.params.postfirm_elevation else None
        if self.params.postfirm_elevation and self.elev is None:
            log.warning("postfirm_elevation is on but data/interim/postfirm/elevation_classes.json is missing "
                        "(run scripts/04b_postfirm.py) - no post-FIRM elevation applied")
        self.reg = load_regional_factors() if self.params.regional_factors else None
        self.last_protect: dict = {}
        self.last_adapt: pd.DataFrame | None = None

    # ------------------------------------------------------------------ post-FIRM elevation
    def _bfe_depth_ft(self, c: Components, j: np.ndarray) -> np.ndarray:
        """BFE above ground (ft) for buildings j: NFHL static BFE - ground (NSI) where available, otherwise
        the model's 100-year depth at reference parameters (fluvial at the FEMA-anchored channel capacity,
        pluvial at the 5-year drain standard of the GPU runs, coastal storm tide), all undefended. The
        reference is fixed so that the BFE does not move with the NFIP-calibrated parameters."""
        ex = c.exposure
        sb = (pd.to_numeric(ex.static_bfe_ft, errors="coerce") - pd.to_numeric(ex.ground_elv_ft, errors="coerce")
              ).to_numpy(float)[j]
        valid = np.isfinite(sb) & (sb > -3.0) & (sb < 30.0)
        out = np.clip(np.where(valid, sb, 0.0), 0.0, 25.0)
        need = np.flatnonzero(~valid)
        if len(need) and 100 in self.rps:
            sub = ex.iloc[j[need]].reset_index(drop=True)
            cc = replace(c, exposure=sub, params=replace(self.params, fluvial_tc_natural=ANCHOR_NATURAL,
                                                         fluvial_tc_urban=ANCHOR_URBAN,
                                                         pluvial_drain_rp=GPU_DRAIN_RP))
            base = [s for s in self.scen if s[0] == "baseline"] or self.scen[:1]
            i100 = self.rps.index(100)
            lon, lat = sub.x.to_numpy(), sub.y.to_numpy()
            d = np.maximum.reduce([fluvial_depths(cc, base, self.rps, lon, lat)[:, 0, i100],
                                   pluvial_depths(cc, base, self.rps, lon, lat)[:, 0, i100],
                                   coastal_depths(cc, base, self.rps)[:, 0, i100]])
            out[need] = np.clip(d * FT, 0.0, 25.0)
        return out

    def prepare_postfirm(self, c: Components) -> pd.DataFrame:
        """Add the post-FIRM columns to c.exposure (in place): p_post (0 outside the SFHA), fh_pf{k} (first-floor
        height of elevation class k, ft), occ_pf (no-basement occupancy), ft_pf (V zone: open pile foundation)."""
        ex = c.exposure
        n = len(ex)
        p = (pd.to_numeric(ex["p_post"], errors="coerce").to_numpy(float) if "p_post" in ex.columns
             else np.zeros(n))
        p = np.nan_to_num(p, nan=0.0)
        from floodrisk.exposure.zones import elevation_rule, is_v
        sfha = elevation_rule(ex.firmzone)                  # A, AE, AH, AO, V, VE (not A99 / AR / unmapped)
        if self.elev is None:
            p[:] = 0.0
        p = np.where(sfha, np.clip(p, 0.0, 1.0), 0.0)
        ex["p_post"] = p
        if self.elev is None:
            return ex
        fh0 = ex.found_ht.fillna(0).to_numpy(np.float64)
        ea, wa = self.elev["A"]
        ev, _ = self.elev.get("V", self.elev["A"])
        self.elev_w = wa
        j = np.flatnonzero(p > 0)
        bfe = self._bfe_depth_ft(c, j) if len(j) else np.zeros(0)
        isv = is_v(ex.firmzone)[j]
        for k in range(len(wa)):
            fk = fh0.copy()
            if len(j):
                fk[j] = np.maximum(fh0[j], bfe + np.where(isv, ev[k], ea[k]))
            ex[f"fh_pf{k}"] = fk
        occ = ex.occtype.fillna("RES1-1SNB").astype(str)
        ex["occ_pf"] = np.where(occ.str.startswith("RES1"), occ.str.replace("WB", "NB", regex=False), occ)
        ft = ex.found_type.fillna("").astype(str).to_numpy()
        vz = is_v(ex.firmzone)
        ex["ft_pf"] = np.where(vz, "P", np.where(ft == "B", "C", ft))
        return ex

    # ------------------------------------------------------------------ losses
    def _core(self, occ, ft, ns, fh, vs, vc, depth_m: np.ndarray, modes) -> np.ndarray:
        """Undamped loss (USD) for one first-floor configuration; depth_m (n, ns, nr)."""
        dff = depth_m * FT - fh[:, None, None]
        loss = np.zeros(depth_m.shape, dtype=np.float64)
        keys = pd.DataFrame({"occ": occ, "ft": np.where(np.isin(ft, ["P", "I"]), ft, ""),
                             "ns": np.clip(ns, 1, 6), "mode": modes})
        for (o, f, s, m), g in keys.groupby(["occ", "ft", "ns", "mode"]).groups.items():
            idx = np.asarray(g)
            ks = self.ddf.curve_key(o, f or None, int(s), "structure", m)
            kc = self.ddf.curve_key(o, f or None, int(s), "contents", m)
            loss[idx] = (self.ddf.ratio(ks, dff[idx]) * vs[idx, None, None]
                         + self.ddf.ratio(kc, dff[idx]) * vc[idx, None, None])
        thr = float(self.params.below_ffe_min_depth_m)
        if thr > 0:      # water below the first floor needs some ground depth to reach openings / basements
            loss[(depth_m < thr) & (dff < 0)] = 0.0
        return loss

    def damage_factor(self, ex: pd.DataFrame, peril: str | None) -> np.ndarray:
        """National vulnerability factor x state calibration factor (inland or coastal) per building."""
        lam = float(self.params.vulnerability_factor)
        if peril is None or self.reg is None:
            return np.full(len(ex), lam)
        col = "c_coastal" if peril == "coastal" else "c_inland"
        st = ex.state_fips.astype(str) if "state_fips" in ex.columns else ex.cbfips.astype(str).str[:2]
        return lam * st.map(self.reg[col]).fillna(1.0).to_numpy(float)

    def _losses(self, ex: pd.DataFrame, depth_m: np.ndarray, modes: np.ndarray, peril: str | None = None
                ) -> np.ndarray:
        """depth_m (n, ns, nr) -> loss (n, ns, nr) USD (expected over the post-FIRM state)."""
        occ = ex.occtype.fillna("RES1-1SNB").to_numpy()
        ft = ex.found_type.fillna("").to_numpy()
        ns = ex.num_story.fillna(1).to_numpy()
        fh = ex.found_ht.fillna(0).to_numpy(np.float64)
        vs = ex.fullrep.fillna(0).to_numpy(np.float64)
        vc = ex.cont_fullrep.fillna(0).to_numpy(np.float64)
        modes = np.asarray(modes)
        loss = self._core(occ, ft, ns, fh, vs, vc, depth_m, modes)
        if "fh_pf0" in ex.columns:
            p = ex.p_post.to_numpy(np.float64)
            j = np.flatnonzero(p > 0)
            if len(j):
                j = j[(depth_m[j] > 0).reshape(len(j), -1).any(axis=1)]
            if len(j):
                occ_p, ft_p = ex.occ_pf.to_numpy()[j], ex.ft_pf.fillna("").to_numpy()[j]
                mix = np.zeros((len(j),) + depth_m.shape[1:])
                for k, w in enumerate(self.elev_w):
                    fk = ex[f"fh_pf{k}"].to_numpy(np.float64)[j]
                    mix += w * self._core(occ_p, ft_p, ns[j], fk, vs[j], vc[j], depth_m[j], modes[j])
                pj = p[j][:, None, None]
                loss[j] = (1.0 - pj) * loss[j] + pj * mix
        loss *= self.damage_factor(ex, peril)[:, None, None]
        loss[depth_m <= 0] = 0.0
        return loss

    def defended_aal(self, ex, Du: dict, Dp, Lp, tp, tpc, riv, cmode, bi: int, chunk: int = 200_000,
                     adapt: bool | None = None):
        """Exact AAL (fluvial, coastal, combined) for buildings behind defences; (rows, dict) or (rows, None)."""
        rows = np.flatnonzero((np.asarray(tp) > 0) | (np.asarray(tpc) > 0))
        if not len(rows):
            return rows, None
        ad = self.params.adapt_with_climate if adapt is None else adapt
        res = {q: [] for q in ("fluvial", "coastal", "combined")}
        for s0 in range(0, len(rows), chunk):
            r = rows[s0:s0 + chunk]
            sub = ex.iloc[r]
            Duf, Duc = Du["fluvial"][r], Du["coastal"][r]
            Luf = self._losses(sub, Duf, riv[r], "fluvial")
            Luc = self._losses(sub, Duc, cmode[r], "coastal")
            pf = {"fluvial": defence_failure_p(Duf, tp[r], self.rps, bi, ad, self.floor_rp),
                  "coastal": defence_failure_p(Duc, tpc[r], self.rps, bi, ad, self.floor_rp)}
            a = aal_defended(self.rps, self.floor_rp,
                             {"fluvial": (Duf, Luf), "pluvial": (Dp[r], Lp[r]), "coastal": (Duc, Luc)}, pf)
            for q in res:
                res[q].append(a[q])
        return rows, {q: np.concatenate(v, axis=0) for q, v in res.items()}

    # ------------------------------------------------------------------ driver
    def run(self, c: Components) -> tuple[pd.DataFrame, pd.DataFrame]:
        ex = c.exposure.reset_index(drop=True)
        c.exposure = ex
        n = len(ex)
        lon, lat = ex.x.to_numpy(), ex.y.to_numpy()
        c.params = self.params
        self.prepare_postfirm(c)
        D = {"fluvial": fluvial_depths(c, self.scen, self.rps, lon, lat),
             "pluvial": pluvial_depths(c, self.scen, self.rps, lon, lat),
             "coastal": coastal_depths(c, self.scen, self.rps)}
        # flood defences: FEMA levee zones / NLD leveed areas (fluvial + coastal) and implicit coastal
        # defences (homes today's 2-yr storm tide would reach are protected by dikes / pumps)
        sids = [s for s, _, _ in self.scen]
        bi = sids.index("baseline") if "baseline" in sids else 0
        prm = self.params
        tp = np.zeros(n) if c.protect_T is None else np.nan_to_num(np.asarray(c.protect_T, float))
        Du = {"fluvial": D["fluvial"], "coastal": D["coastal"]}          # undefended depths
        D["fluvial"] = apply_protection(D["fluvial"], tp, self.rps, bi, prm.adapt_with_climate)
        tpc = tp.copy()
        if prm.coastal_implicit_rp > 0:
            i2 = self.rps.index(2) if 2 in self.rps else 0
            deep2 = D["coastal"][:, bi, i2] >= prm.coastal_implicit_min_depth_m      # baseline 2-yr event
            tpc = np.where(deep2, np.maximum(tpc, prm.coastal_implicit_rp), tpc)
        D["coastal"] = apply_protection(D["coastal"], tpc, self.rps, bi, prm.adapt_with_climate)
        self.last_protect = {"fluvial": tp, "coastal": tpc}
        riv = np.full(n, "riverine")
        cmode = coastal_mode(ex.firmzone, ex.zone_sub)
        L = {"fluvial": self._losses(ex, D["fluvial"], riv, "fluvial"),
             "pluvial": self._losses(ex, D["pluvial"], riv, "pluvial"),
             "coastal": self._losses(ex, D["coastal"], cmode, "coastal")}
        stack = np.stack([D[p] for p in PERILS])                         # (3, n, ns, nr)
        arg = stack.argmax(axis=0)
        Lstack = np.stack([L[p] for p in PERILS])
        L["combined"] = np.take_along_axis(Lstack, arg[None], axis=0)[0]
        D["combined"] = stack.max(axis=0)
        del stack, Lstack, arg

        rows, aal_ex = self.defended_aal(ex, Du, D["pluvial"], L["pluvial"], tp, tpc, riv, cmode, bi)

        rp = np.asarray(self.rps)
        i100, i1000 = int(np.flatnonzero(rp == 100)[0]), int(np.flatnonzero(rp == 1000)[0])
        out = {"fd_id": ex.fd_id.values}
        for p in (*PERILS, "combined"):
            aal = aal_trapezoid(L[p], self.rps, floor_rp=self.floor_rp)  # (n, ns)
            if aal_ex is not None and p in aal_ex:                       # exact AAL behind defences
                aal[rows] = aal_ex[p]
            for j, (sid, _, _) in enumerate(self.scen):
                out[f"aal_{p}_{sid}"] = aal[:, j].astype(np.float32)
                out[f"l100_{p}_{sid}"] = L[p][:, j, i100].astype(np.float32)
                out[f"l1000_{p}_{sid}"] = L[p][:, j, i1000].astype(np.float32)
                out[f"d100_{p}_{sid}"] = D[p][:, j, i100].astype(np.float32)
        bld = pd.DataFrame(out)
        if "p_post" in ex.columns:
            bld["p_postfirm"] = ex.p_post.to_numpy(np.float32)

        # ZCTA partial sums of losses at every return period (for loss-exceedance curves)
        z = ex.zcta.fillna("").to_numpy()
        uz, inv = np.unique(z, return_inverse=True)
        sums = {}
        recs = []
        for p in (*PERILS, "combined"):
            s_ = np.zeros((len(uz),) + L[p].shape[1:])
            np.add.at(s_, inv, L[p])
            sums[p] = s_
            for j, (sid, _, _) in enumerate(self.scen):
                for k, T in enumerate(self.rps):
                    recs.append(pd.DataFrame({"zcta": uz, "peril": p, "scenario_id": sid, "rp": int(T),
                                              "loss_usd": s_[:, j, k]}))

        # raised-crest variant: defences keep their standard in every scenario (defended buildings only)
        self.last_adapt = None
        if prm.adapt_variant and not prm.adapt_with_climate and len(rows):
            fut = [j for j, s in enumerate(sids) if s != "baseline"]
            sub = ex.iloc[rows]
            Da = {"fluvial": apply_protection(Du["fluvial"][rows], tp[rows], self.rps, bi, True),
                  "pluvial": D["pluvial"][rows],
                  "coastal": apply_protection(Du["coastal"][rows], tpc[rows], self.rps, bi, True)}
            La = {"fluvial": self._losses(sub, Da["fluvial"], riv[rows], "fluvial"),
                  "pluvial": L["pluvial"][rows],
                  "coastal": self._losses(sub, Da["coastal"], cmode[rows], "coastal")}
            st = np.stack([Da[p] for p in PERILS])
            La["combined"] = np.take_along_axis(np.stack([La[p] for p in PERILS]), st.argmax(axis=0)[None],
                                                axis=0)[0]
            Da["combined"] = st.max(axis=0)
            _, aal_a = self.defended_aal(ex, Du, D["pluvial"], L["pluvial"], tp, tpc, riv, cmode, bi, adapt=True)
            oa = {"fd_id": ex.fd_id.values[rows]}
            for p in ("fluvial", "coastal", "combined"):
                for j in fut:
                    sid = sids[j]
                    oa[f"aal_{p}_{sid}"] = aal_a[p][:, j].astype(np.float32)
                    oa[f"l100_{p}_{sid}"] = La[p][:, j, i100].astype(np.float32)
                    oa[f"l1000_{p}_{sid}"] = La[p][:, j, i1000].astype(np.float32)
                    oa[f"d100_{p}_{sid}"] = Da[p][:, j, i100].astype(np.float32)
            self.last_adapt = pd.DataFrame(oa)
            # ZCTA sums of the variant = fixed-crest sums + (raised - fixed) over the defended buildings
            zr = inv[rows]
            uzr, invr = np.unique(zr, return_inverse=True)
            for p in (*PERILS, "combined"):
                s_ = sums[p][uzr].copy()
                np.add.at(s_, invr, La[p] - L[p][rows])
                for j in fut:
                    for k, T in enumerate(self.rps):
                        recs.append(pd.DataFrame({"zcta": uz[uzr], "peril": p, "scenario_id": f"{sids[j]}_adapt",
                                                  "rp": int(T), "loss_usd": s_[:, j, k]}))
        zrp = pd.concat(recs, ignore_index=True)
        zrp = zrp[zrp.zcta != ""]
        return bld, zrp
