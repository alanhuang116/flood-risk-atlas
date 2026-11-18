"""Step 14 — calibrate the global loss-model parameters against NFIP claims (county level).

Parameters (risk.calibration / vulnerability in config/settings.yaml):
  fluvial_tc_natural, fluvial_tc_urban : effective channel-capacity return periods (natural / urban
                                         channels); the rating curves stay anchored to FEMA at the
                                         physical capacity (hazard/fluvial/capacity.py)
  pluvial_drain_rp                     : effective storm-drain standard
  below_ffe_min_depth_m                : ground depth before below-first-floor damage counts
  vulnerability_factor                 : global damage multiplier (closed form for every parameter set)
Flood defences (FEMA levee zones, NLD, implicit coastal) are applied as configured.

  --grid         : baseline AAL per county x SFHA for every parameter combination (parallel over HUC-8s,
                   resumable) -> data/interim/calibration/grid_v3/{huc8}.parquet
  --grid-missing : print the number of HUC-8s with pluvial output but no grid file (last line)
  --fit          : compare with NFIP claims (1980-2024, 2025 USD), two-fold cross-validation by state
                   -> data/outputs/validation/nfip_calibration.{json,csv}
  --apply        : write the best parameters into config/settings.yaml
  --regional     : state calibration factors on top of the national fit (empirical Bayes over NOAA climate
                   regions, year-split validation) -> data/outputs/validation/nfip_state_factors.{csv,json};
                   with --apply also config/regional_factors.csv (used by the risk engine)

Target and objective: see ``fit``.
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from floodrisk.config import path  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402

def GRID_DIR():
    # v4 (model v1.1): explicit post-FIRM elevation of SFHA homes (vulnerability.postfirm_elevation).
    # v3: reach multipliers k anchored at the physical channel capacity (v2 recomputed k for every
    # capacity combination, which degenerates as the urban capacity approaches 100 years)
    return path("interim", "calibration", "grid_v4", mkdir=True)


GRID = {"fluvial": [(2, 10), (2, 25), (2, 50), (2, 100), (5, 25), (5, 50), (5, 100), (10, 50), (10, 100)],
        "td": [5, 10, 25, 50],
        "thr": [0.0, 0.3]}
CAUSE_WEIGHT = 0.5          # weight of the flood-cause terms (NFIP cause codes are only indicative)
EXCLUDE_STATES = {"22"}     # Louisiana: claims history dominated by the 2005 levee failures (pre-HSDRRS)


def grid_one(huc8: str) -> dict:
    from floodrisk.risk.engine import (LossModel, RiskParams, apply_protection, coastal_depths, coastal_mode,
                                       fluvial_depths, pluvial_depths)
    from floodrisk.risk.inputs import fluvial_k, load_components
    from floodrisk.risk.metrics import aal_trapezoid
    from floodrisk.risk.protection import protection_T
    from floodrisk.utils.http import atomic_parquet
    t0 = time.time()
    try:
        ex, comps = load_components(huc8)
        if len(ex) == 0 or comps.pluvial_bld is None:
            return {"huc8": huc8, "status": "skip"}
        ex = ex.reset_index(drop=True)
        n = len(ex)
        # lambda and the state factors are fitted afterwards; post-FIRM elevation as configured
        base = replace(RiskParams.from_settings(), vulnerability_factor=1.0, regional_factors=False,
                       adapt_variant=False)
        m = LossModel(base)
        m.scen = [s for s in m.scen if s[0] == "baseline"]
        rps = m.rps
        lon, lat = ex.x.to_numpy(), ex.y.to_numpy()
        tp = protection_T(ex, base)
        # coastal (+ implicit defences)
        Dc = coastal_depths(comps, m.scen, rps)
        deep2 = Dc[:, 0, rps.index(2)] >= base.coastal_implicit_min_depth_m
        tpc = np.where(deep2, np.maximum(tp, base.coastal_implicit_rp), tp)
        Dcu = Dc
        Dc = apply_protection(Dc, tpc, rps, 0, False)
        # fluvial per effective channel-capacity combination; k is anchored at the physical capacity and
        # therefore computed once (undefended depths kept for the exact defended AAL)
        comps.reach_dev, comps.src_k = fluvial_k(huc8, comps, save=False)
        comps.exposure, comps.params = ex, base
        m.prepare_postfirm(comps)                   # post-FIRM columns (BFE at reference parameters)
        ex = comps.exposure
        Df, Dfu = {}, {}
        for tn, tu in GRID["fluvial"]:
            comps.params = replace(base, fluvial_tc_natural=tn, fluvial_tc_urban=tu)
            Dfu[(tn, tu)] = fluvial_depths(comps, m.scen, rps, lon, lat)
            Df[(tn, tu)] = apply_protection(Dfu[(tn, tu)], tp, rps, 0, False)
        # pluvial per drainage standard
        Dp = {}
        for td in GRID["td"]:
            comps.params = replace(base, pluvial_drain_rp=td)
            Dp[td] = pluvial_depths(comps, m.scen, rps, lon, lat)
        riv = np.full(n, "riverine")
        cmode = coastal_mode(ex.firmzone, ex.zone_sub)
        from floodrisk.exposure.zones import is_sfha
        sfha = is_sfha(ex.firmzone)            # exact codes ("AREA NOT INCLUDED" is unmapped, not an A zone)
        cty = (ex.cbfips.fillna("").str[:5] + np.where(sfha, "|1", "|0")).to_numpy()
        uc, inv = np.unique(cty, return_inverse=True)
        agg = lambda v: np.bincount(inv, weights=v, minlength=len(uc))  # noqa: E731
        aal = lambda L: aal_trapezoid(L, rps, floor_rp=m.floor_rp)[:, 0]  # noqa: E731
        recs = []
        for thr in GRID["thr"]:
            m.params = replace(base, below_ffe_min_depth_m=thr)
            Lf = {k: m._losses(ex, v, riv) for k, v in Df.items()}
            Lp = {k: m._losses(ex, v, riv) for k, v in Dp.items()}
            Lc = m._losses(ex, Dc, cmode)
            a_c = agg(aal(Lc))
            a_f = {k: agg(aal(v)) for k, v in Lf.items()}
            a_p = {k: agg(aal(v)) for k, v in Lp.items()}
            for fk, td in itertools.product(Df, Dp):
                st = np.stack([Df[fk], Dp[td], Dc])
                arg = st.argmax(axis=0)
                L = np.take_along_axis(np.stack([Lf[fk], Lp[td], Lc]), arg[None], axis=0)[0]
                a_comb = aal(L)
                af_b = aal(Lf[fk])
                ac_b = aal(Lc)
                rows, ex_aal = m.defended_aal(ex, {"fluvial": Dfu[fk], "coastal": Dcu}, Dp[td], Lp[td], tp, tpc,
                                              riv, cmode, 0)
                if ex_aal is not None:                      # exact AAL behind defences (as in the risk step)
                    a_comb[rows] = ex_aal["combined"][:, 0]
                    af_b[rows] = ex_aal["fluvial"][:, 0]
                    ac_b[rows] = ex_aal["coastal"][:, 0]
                recs.append(pd.DataFrame({"county": uc, "tn": fk[0], "tu": fk[1], "td": td, "thr": thr,
                                          "aal_f": agg(af_b), "aal_p": a_p[td], "aal_c": agg(ac_b),
                                          "aal": agg(a_comb)}))
        out = pd.concat(recs, ignore_index=True)
        out["sfha"] = out.county.str[-1].astype(int)
        out["county"] = out.county.str[:5]
        out["huc8"] = huc8
        nb = pd.DataFrame({"county": uc, "n_bld": np.bincount(inv, minlength=len(uc)),
                           "value": agg(ex.fullrep.fillna(0).to_numpy() + ex.cont_fullrep.fillna(0).to_numpy())})
        nb["sfha"] = nb.county.str[-1].astype(int)
        nb["county"] = nb.county.str[:5]
        out = out.merge(nb, on=["county", "sfha"])
        atomic_parquet(out, GRID_DIR() / f"{huc8}.parquet")
        return {"huc8": huc8, "status": "ok", "n": n, "sec": round(time.time() - t0, 1)}
    except Exception as exc:  # noqa: BLE001
        return {"huc8": huc8, "status": f"error {exc!r}", "trace": traceback.format_exc(limit=6)}


# ----------------------------------------------------------------------------------- fit
def claims_by_county(y0: int = 1980, y1: int = 2024) -> pd.DataFrame:
    import importlib.util
    spec = importlib.util.spec_from_file_location("v12", Path(__file__).with_name("12_validate_nfip.py"))
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)
    cl = pd.concat([pd.read_parquet(f) for f in glob.glob(str(path("raw", "nfip", "state") / "*.parquet"))])
    cl = cl[cl.occupancyType.isin(v.RES_OCC) & cl.yearOfLoss.between(y0, y1)].copy()
    f = cl.yearOfLoss.map(lambda y: v.CPI[2025] / v.CPI.get(int(y), v.CPI[2025]))
    cl["dmg"] = (cl.buildingDamageAmount.fillna(0) + cl.contentsDamageAmount.fillna(0)) * f
    cl["peril"] = cl.causeOfDamage.astype(str).map(v.CAUSE).fillna("other")
    nyr = y1 - y0 + 1
    c = cl.pivot_table(index="countyCode", columns="peril", values="dmg", aggfunc="sum", fill_value=0) / nyr
    c["claims"] = c.sum(axis=1)
    pen = pd.read_parquet(path("raw", "nfip", "residential_penetration.parquet"))
    pen = pen.set_index("fipsCode")[["resPenetrationRate", "totalResStructures"]].astype(float)
    c = c.join(pen, how="left")
    return c.reset_index().rename(columns={"countyCode": "county"})


def assemble(min_pen: float = 0.0) -> tuple[pd.DataFrame, set]:
    """County x parameter-set table of model (insured-equivalent) AAL and NFIP claims (see ``fit``)."""
    files = glob.glob(str(GRID_DIR() / "*.parquet"))
    g = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    keys = ["tn", "tu", "td", "thr"]
    g = g.groupby(["county", "sfha", *keys], as_index=False)[["aal_f", "aal_p", "aal_c", "aal", "n_bld", "value"]].sum()
    import duckdb
    tot = duckdb.sql(f"""select substr(cbfips,1,5) county, count(*) n_all from
          read_parquet('{path("processed", "exposure").as_posix()}/state=*/residential.parquet') group by 1""").df()
    first = g[(g.tn == g.tn.iloc[0]) & (g.tu == g.tu.iloc[0]) & (g.td == g.td.iloc[0]) & (g.thr == g.thr.iloc[0])]
    nb = first.groupby("county").n_bld.sum().rename("n_grid").reset_index().merge(tot, on="county")
    complete = set(nb.county[nb.n_grid >= 0.99 * nb.n_all])
    sh = first.pivot_table(index="county", columns="sfha", values="n_bld", aggfunc="sum", fill_value=0)
    sh = (sh.get(1, 0) / sh.sum(axis=1)).rename("sfha_share").reset_index()

    c = claims_by_county()
    c = c[~c.county.str.startswith("09")]                      # CT: claims on legacy county codes
    pen = pd.read_parquet(path("raw", "nfip", "residential_penetration.parquet")).astype(
        {"resContractsInForceSfha": float, "resContractsInForce": float, "totalResStructuresSfha": float,
         "totalResStructures": float, "resPenetrationRateSfha": float, "resPenetrationRate": float})
    pen["pen_1"] = pen.resPenetrationRateSfha.clip(0, 1)
    pen["pen_0"] = ((pen.resContractsInForce - pen.resContractsInForceSfha) /
                    (pen.totalResStructures - pen.totalResStructuresSfha).clip(lower=1)).clip(0, 1)
    pen = pen.drop(columns=["county"], errors="ignore").rename(columns={"fipsCode": "county"})[
        ["county", "pen_0", "pen_1", "resPenetrationRate"]]
    g = g[g.county.isin(complete)].merge(pen, on="county", how="inner")
    g["pen_z"] = np.where(g.sfha == 1, g.pen_1, g.pen_0)
    for col in ("aal", "aal_f", "aal_p", "aal_c"):
        g[col + "_ins"] = g[col] * g.pen_z
    agg = g.groupby(["county", *keys], as_index=False)[["aal", "aal_f", "aal_p", "aal_c", "aal_ins", "aal_f_ins",
                                                       "aal_p_ins", "aal_c_ins", "n_bld"]].sum()
    agg = agg.merge(c[["county", "claims", "coastal", "fluvial", "pluvial"]].rename(
        columns={"coastal": "cl_c", "fluvial": "cl_f", "pluvial": "cl_p"}), on="county", how="inner")
    agg = agg.merge(pen[["county", "resPenetrationRate"]], on="county").merge(sh, on="county")
    agg = agg[(agg.n_bld >= 1000) & (agg.resPenetrationRate >= min_pen) & ~agg.county.str[:2].isin(EXCLUDE_STATES)]
    one = agg.drop_duplicates("county")[["county", "sfha_share"]].copy()
    one["stratum"] = pd.qcut(one.sfha_share.rank(method="first"), 5, labels=False)
    states = sorted(one.county.str[:2].unique())
    train_states = set(states[::2])
    one["split"] = np.where(one.county.str[:2].isin(train_states), "train", "valid")
    agg = agg.merge(one[["county", "stratum", "split"]], on="county")
    return agg, train_states


def fit(log, min_pen: float = 0.0, tag: str = "") -> pd.DataFrame:
    """Compare insured-equivalent model losses with NFIP claims per county.

    model_insured = AAL(SFHA homes) x SFHA penetration + AAL(other homes) x non-SFHA penetration
    (penetration by county and zone, OpenFEMA 2026), against claims = NFIP residential building +
    contents damage per year (1980-2024, 2025 USD). Comparing insured-equivalent losses removes the
    adverse-selection bias of dividing claims by an average penetration rate.
    Strata: quintiles of the county share of homes in the SFHA (independent of both the claims history
    and the model parameters - stratifying by realised claims would build regression to the mean into
    the objective). Objective = log(overall ratio)^2 + mean over strata of log(stratum ratio)^2,
    plus CAUSE_WEIGHT x mean of log(cause ratio)^2 for river, rainfall and tidal claims, on the training
    states; the other states validate. Louisiana is excluded (claims dominated by the 2005 levee failures,
    before the rebuilt defence system) and reported separately. The overall level is carried by a global
    vulnerability factor lambda (closed-form optimum on the training states for every parameter set); the
    physical parameters are chosen by the remaining shape mismatch (balance across strata).
    """
    agg, train_states = assemble(min_pen)
    keys = ["tn", "tu", "td", "thr"]
    from scipy.stats import spearmanr

    def metrics(d: pd.DataFrame) -> dict:
        r_all = d.aal_ins.sum() / d.claims.sum()
        rq = d.groupby("stratum").apply(lambda x: x.aal_ins.sum() / max(x.claims.sum(), 1.0), include_groups=False)
        lr, w = terms(d)
        loss = float(np.sum(w * lr ** 2))
        rho = spearmanr(d.aal_ins, d.claims).statistic
        return {"ratio": r_all, "loss": loss, "spearman": rho, **{f"r_q{int(k)}": v for k, v in rq.items()},
                "ratio_f": d.aal_f_ins.sum() / max(d.cl_f.sum(), 1), "ratio_p": d.aal_p_ins.sum() / max(d.cl_p.sum(), 1),
                "ratio_c": d.aal_c_ins.sum() / max(d.cl_c.sum(), 1), "model_total_bn": d.aal.sum() / 1e9,
                "model_ins_bn": d.aal_ins.sum() / 1e9, "claims_bn": d.claims.sum() / 1e9, "n_county": int(len(d))}

    def terms(d: pd.DataFrame):
        """log-ratio terms and weights of the objective: overall, 5 strata, 3 flood causes."""
        r_all = d.aal_ins.sum() / d.claims.sum()
        rq = d.groupby("stratum").apply(lambda x: x.aal_ins.sum() / max(x.claims.sum(), 1.0), include_groups=False)
        rp = [d.aal_f_ins.sum() / max(d.cl_f.sum(), 1), d.aal_p_ins.sum() / max(d.cl_p.sum(), 1),
              d.aal_c_ins.sum() / max(d.cl_c.sum(), 1)]
        lr = np.log(np.clip(np.concatenate([[r_all], rq.to_numpy(), rp]), 1e-3, None))
        w = np.concatenate([[1.0], np.full(len(rq), 1.0 / len(rq)), np.full(3, CAUSE_WEIGHT / 3)])
        return lr, w

    def lam_star(d: pd.DataFrame) -> float:
        """Global vulnerability factor minimising the weighted objective (closed form in log space)."""
        lr, w = terms(d)
        return float(np.exp(-np.sum(w * lr) / np.sum(w)))

    cols_scale = ("aal", "aal_f", "aal_p", "aal_c", "aal_ins", "aal_f_ins", "aal_p_ins", "aal_c_ins")

    def scaled(d, lam):
        d = d.copy()
        for col in cols_scale:
            d[col] = d[col] * lam
        return d

    rows = []
    for k, d in agg.groupby(keys):
        A, B = d[d.split == "train"], d[d.split == "valid"]
        lam_a, lam_b = lam_star(A), lam_star(B)
        v_b = metrics(scaled(B, lam_a))["loss"]          # fit on A, test on B
        v_a = metrics(scaled(A, lam_b))["loss"]          # fit on B, test on A
        lam = lam_star(d)                                 # final factor from all calibration states
        dl = scaled(d, lam)
        rows.append({**dict(zip(keys, k)), "lambda": lam, "cv_loss": 0.5 * (v_a + v_b),
                     "lambda_a": lam_a, "lambda_b": lam_b,
                     **{f"train_{a}": b for a, b in metrics(scaled(A, lam_a)).items()},
                     **{f"valid_{a}": b for a, b in metrics(scaled(B, lam_a)).items()},
                     **{f"all_{a}": b for a, b in metrics(dl).items()},
                     "raw_all_ratio": d.aal_ins.sum() / d.claims.sum()})
    res = pd.DataFrame(rows).sort_values("cv_loss")
    od = path("outputs", "validation", mkdir=True)
    res.to_csv(od / f"nfip_calibration_grid{tag}.csv", index=False)
    best = res.iloc[0].to_dict()
    (od / f"nfip_calibration{tag}.json").write_text(json.dumps({"best": best, "grid": GRID,
                                                          "n_counties": int(agg.county.nunique()),
                                                          "train_states": sorted(train_states)}, indent=2, default=float))
    bk = {kk: best[kk] for kk in keys}
    sel = agg[np.logical_and.reduce([agg[kk] == bk[kk] for kk in keys])].copy()
    sel["lambda"] = best["lambda"]
    sel.to_csv(od / f"nfip_calibration_best_by_county{tag}.csv", index=False)
    pd.set_option("display.width", 260)
    show = ["tn", "tu", "td", "thr", "lambda", "lambda_a", "lambda_b", "cv_loss", "raw_all_ratio", "train_loss", "valid_loss", "all_ratio", "valid_ratio", "all_spearman",
            "all_r_q0", "all_r_q1", "all_r_q2", "all_r_q3", "all_r_q4", "all_ratio_f", "all_ratio_p", "all_ratio_c",
            "all_model_total_bn", "all_claims_bn"]
    log.info("best 12 parameter sets:\n%s", res[show].head(12).round(3).to_string(index=False))
    log.info("counties: %d (train states %d), min penetration %.3f", agg.county.nunique(), len(train_states), min_pen)
    return res


# ----------------------------------------------------------------------------------- state factors
STATE_ABBR = {"01": "AL", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
              "12": "FL", "13": "GA", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
              "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO",
              "30": "MT", "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC",
              "38": "ND", "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
              "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
              "56": "WY"}
# NOAA NCEI climate regions (Karl & Koss 1984) - a standard, climate-based grouping of the states
NOAA_REGIONS = {"Northeast": "CT DE ME MD MA NH NJ NY PA RI VT DC", "Upper Midwest": "IA MI MN WI",
                "Ohio Valley": "IL IN KY MO OH TN WV", "Southeast": "AL FL GA NC SC VA",
                "Northern Rockies and Plains": "MT NE ND SD WY", "South": "AR KS LA MS OK TX",
                "Southwest": "AZ CO NM UT", "Northwest": "ID OR WA", "West": "CA NV"}
REGION_OF = {ab: r for r, s in NOAA_REGIONS.items() for ab in s.split()}
MIN_GROUP_USD = 1.0e6       # a state enters a peril group's estimation with >= $1 M/yr of model and claims
N_BOOT = 4000
FACTOR_BOUNDS = (0.25, 4.0)


def normalise(c_in: np.ndarray, c_co: np.ndarray, m_in: np.ndarray, m_co: np.ndarray) -> float:
    """Scale kappa that keeps the national (calibration-state) model total: the state factors move losses
    between states, the national level stays with the national calibration. Shrinkage is asymmetric -
    under-predicting states are mostly single-catastrophe records (high variance, strong shrinkage), while
    over-predicting states are often records without a rare event (low variance) - so the unscaled factors
    would pull the national level down."""
    return float((m_in.sum() + m_co.sum()) / (m_in * c_in + m_co * c_co).sum())


def claims_by_county_year(y0: int = 1980, y1: int = 2024) -> pd.DataFrame:
    """Residential NFIP damage (2025 USD) per county x loss year x peril group (coastal = tidal claims)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("v12", Path(__file__).with_name("12_validate_nfip.py"))
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)
    cl = pd.concat([pd.read_parquet(f) for f in glob.glob(str(path("raw", "nfip", "state") / "*.parquet"))])
    cl = cl[cl.occupancyType.isin(v.RES_OCC) & cl.yearOfLoss.between(y0, y1)].copy()
    f = cl.yearOfLoss.map(lambda y: v.CPI[2025] / v.CPI.get(int(y), v.CPI[2025]))
    cl["dmg"] = (cl.buildingDamageAmount.fillna(0) + cl.contentsDamageAmount.fillna(0)) * f
    cl["grp"] = np.where(cl.causeOfDamage.astype(str).str.strip() == "1", "coastal", "inland")
    return (cl.groupby(["countyCode", "yearOfLoss", "grp"]).dmg.sum().reset_index()
            .rename(columns={"countyCode": "county", "yearOfLoss": "year"}))


def boot_var_log_mean(C: np.ndarray, rng, n_boot: int = N_BOOT) -> np.ndarray:
    """Variance of log(mean annual claims) per row of C (states x years), by resampling the loss years."""
    ny = C.shape[1]
    idx = rng.integers(0, ny, size=(n_boot, ny))
    m = C[:, idx].mean(axis=2)                                    # (states, boot)
    return np.var(np.log(np.maximum(m, 1.0)), axis=1)


def eb_fit(delta: np.ndarray, sig2: np.ndarray, region: np.ndarray, tau_grid=None):
    """Hierarchical normal model delta_s = a_r + b_s + e_s (a ~ N(0, tR2), b ~ N(0, tS2), e ~ N(0, sig2_s)),
    shrunk towards the national model (mean 0). Hyper-parameters by maximum marginal likelihood on a grid;
    returns (posterior a_r + b_s per state, {region: a_r}, (tR2, tS2))."""
    tau_grid = np.linspace(0.0, 1.5, 61) if tau_grid is None else tau_grid
    regs = np.unique(region)
    parts = [(r, delta[region == r], sig2[region == r]) for r in regs]

    def loglik(tR2, tS2):
        ll = 0.0
        for _, dv, s2 in parts:
            S = tR2 * np.ones((len(dv), len(dv))) + np.diag(tS2 + s2)
            _, logdet = np.linalg.slogdet(S)
            ll += -0.5 * (logdet + dv @ np.linalg.solve(S, dv))
        return ll

    _, tR2, tS2 = max(((loglik(a * a, b * b), a * a, b * b) for a in tau_grid for b in tau_grid),
                      key=lambda x: x[0])
    post = np.zeros(len(delta))
    reff = {}
    for r, dv, s2 in parts:
        m = region == r
        S = tR2 * np.ones((len(dv), len(dv))) + np.diag(tS2 + s2)
        w = np.linalg.solve(S, dv)
        reff[r] = float(tR2 * w.sum())
        post[m] = reff[r] + tS2 * w
    return post, reff, (float(tR2), float(tS2))


def regional(log, apply: bool = False) -> pd.DataFrame:
    """State calibration factors on top of the national calibration (empirical Bayes, NOAA climate regions).

    For each state and peril group (inland = river + rainfall + other claims, coastal = tidal claims), the
    log ratio delta = log(claims / model) of the nationally calibrated model is shrunk towards the national
    model through a two-level hierarchy (climate region, state). The sampling variance of each state's
    claims average comes from resampling the 45 loss years, so records dominated by one catastrophe
    (Harvey, Katrina, Sandy) are shrunk strongly and long multi-event records keep their own signal. The
    hierarchy is validated by splitting the loss years (odd / even): factors estimated on one half are
    scored on the other, against the national model and unshrunk state ratios."""
    od = path("outputs", "validation", mkdir=True)
    best = json.loads((od / "nfip_calibration.json").read_text())["best"]
    lam = float(best["lambda"])
    agg, _ = assemble(0.0)
    d = agg[(agg.tn == best["tn"]) & (agg.tu == best["tu"]) & (agg.td == best["td"]) & (agg.thr == best["thr"])].copy()
    tot = (d.aal_f_ins + d.aal_p_ins + d.aal_c_ins).replace(0, np.nan)
    d["m_coastal"] = lam * d.aal_ins * (d.aal_c_ins / tot).fillna(0.0)
    d["m_inland"] = lam * d.aal_ins - d.m_coastal
    d["st"] = d.county.str[:2]
    cy = claims_by_county_year()
    cy = cy[cy.county.isin(set(d.county))]
    years = np.arange(1980, 2025)
    states = sorted(d.st.unique())
    M = d.groupby("st")[["m_inland", "m_coastal"]].sum().reindex(states)
    C = {}
    for g in ("inland", "coastal"):
        t = cy[cy.grp == g].assign(st=lambda x: x.county.str[:2]).pivot_table(
            index="st", columns="year", values="dmg", aggfunc="sum", fill_value=0.0)
        C[g] = t.reindex(index=states, columns=years, fill_value=0.0).fillna(0.0).to_numpy()
    region = np.array([REGION_OF[STATE_ABBR[s]] for s in states])
    rng = np.random.default_rng(20261118)

    def estimate(Cg, Mg, yr_mask):
        cm = Cg[:, yr_mask].mean(axis=1)
        ok = (cm >= MIN_GROUP_USD) & (Mg >= MIN_GROUP_USD)
        delta = np.log(np.where(ok, cm, 1.0) / np.where(ok, Mg, 1.0))
        s2 = boot_var_log_mean(Cg[:, yr_mask], rng)
        post = np.zeros(len(Mg))
        if ok.sum() >= 3:
            p, reff, tau = eb_fit(delta[ok], s2[ok], region[ok])
            post[ok] = p
            post[~ok] = [reff.get(r, 0.0) for r in region[~ok]]
        else:
            reff, tau = {}, (0.0, 0.0)
        return dict(delta=np.where(ok, delta, np.nan), s2=s2, ok=ok, post=post, reff=reff, tau=tau, cm=cm)

    allyr = np.ones(len(years), bool)
    est = {g: estimate(C[g], M[f"m_{g}"].to_numpy(), allyr) for g in ("inland", "coastal")}
    Ct = C["inland"] + C["coastal"]
    Mt = M.m_inland.to_numpy() + M.m_coastal.to_numpy()
    est["all"] = estimate(Ct, Mt, allyr)

    # ---- validation: split the loss years, score the total (inland + coastal) of every state
    scores = {k: [] for k in ("national", "raw_two_group", "eb_one_factor", "eb_two_group")}
    wts = []
    for h in (1, 0):
        tr, te = (years % 2 == h), (years % 2 != h)
        e2 = {g: estimate(C[g], M[f"m_{g}"].to_numpy(), tr) for g in ("inland", "coastal")}
        e1 = estimate(Ct, Mt, tr)
        cte = Ct[:, te].mean(axis=1)
        ok = (cte >= MIN_GROUP_USD) & (Mt >= MIN_GROUP_USD)

        def err(c_in, c_co):
            pred = M.m_inland.to_numpy() * c_in + M.m_coastal.to_numpy() * c_co
            return np.log(cte[ok] / pred[ok])

        raw = {g: np.exp(np.nan_to_num(e2[g]["delta"], nan=0.0)) for g in e2}
        mi, mc = M.m_inland.to_numpy(), M.m_coastal.to_numpy()

        def nerr(a, b):
            k = normalise(a, b, mi, mc)
            return err(a * k, b * k)

        scores["national"].append(err(1.0, 1.0))
        scores["raw_two_group"].append(nerr(raw["inland"], raw["coastal"]))
        scores["eb_one_factor"].append(nerr(np.exp(e1["post"]), np.exp(e1["post"])))
        scores["eb_two_group"].append(nerr(np.exp(e2["inland"]["post"]), np.exp(e2["coastal"]["post"])))
        wts.append(Mt[ok] / Mt[ok].sum())
    cv = {}
    for k, v in scores.items():
        e = np.concatenate(v)
        w = np.concatenate(wts) / 2
        cv[k] = {"rmse_log": float(np.sqrt(np.mean(e ** 2))), "w_rmse_log": float(np.sqrt(np.sum(w * e ** 2))),
                 "median_abs_log": float(np.median(np.abs(e)))}
    log.info("state factors - year-split validation (log ratio of held-out half):\n%s",
             pd.DataFrame(cv).T.round(3).to_string())
    two = cv["eb_two_group"]["w_rmse_log"] <= cv["eb_one_factor"]["w_rmse_log"]
    c_in = np.exp(est["inland"]["post"] if two else est["all"]["post"])
    c_co = np.exp(est["coastal"]["post"] if two else est["all"]["post"])
    kappa = normalise(c_in, c_co, M.m_inland.to_numpy(), M.m_coastal.to_numpy())
    c_in, c_co = c_in * kappa, c_co * kappa
    clip_in, clip_co = np.clip(c_in, *FACTOR_BOUNDS), np.clip(c_co, *FACTOR_BOUNDS)
    tab = pd.DataFrame({
        "state_fips": states, "abbr": [STATE_ABBR[s] for s in states], "region": region,
        "c_inland": np.round(clip_in, 4), "c_coastal": np.round(clip_co, 4),
        "model_inland_musd": np.round(M.m_inland.to_numpy() / 1e6, 2),
        "claims_inland_musd": np.round(est["inland"]["cm"] / 1e6, 2),
        "raw_inland": np.round(np.exp(est["inland"]["delta"]), 3), "se_inland": np.round(np.sqrt(est["inland"]["s2"]), 3),
        "model_coastal_musd": np.round(M.m_coastal.to_numpy() / 1e6, 2),
        "claims_coastal_musd": np.round(est["coastal"]["cm"] / 1e6, 2),
        "raw_coastal": np.round(np.exp(est["coastal"]["delta"]), 3),
        "se_coastal": np.round(np.sqrt(est["coastal"]["s2"]), 3),
        "raw_all": np.round(np.exp(est["all"]["delta"]), 3), "c_all": np.round(np.exp(est["all"]["post"]), 4)})
    # states without calibration counties (Louisiana, Connecticut): their climate region's effect
    extra = []
    for s in sorted(EXCLUDE_STATES | {"09"}):
        if s in states or s not in STATE_ABBR:
            continue
        r = REGION_OF[STATE_ABBR[s]]
        g_in = est["inland" if two else "all"]["reff"].get(r, 0.0)
        g_co = est["coastal" if two else "all"]["reff"].get(r, 0.0)
        extra.append({"state_fips": s, "abbr": STATE_ABBR[s], "region": r,
                      "c_inland": round(float(np.clip(kappa * np.exp(g_in), *FACTOR_BOUNDS)), 4),
                      "c_coastal": round(float(np.clip(kappa * np.exp(g_co), *FACTOR_BOUNDS)), 4)})
    tab = pd.concat([tab, pd.DataFrame(extra)], ignore_index=True).sort_values("state_fips")
    summ = {"design": "two peril groups" if two else "one factor", "cv": cv, "lambda": lam, "kappa": kappa,
            "tau": {g: {"region": float(np.sqrt(est[g]["tau"][0])), "state": float(np.sqrt(est[g]["tau"][1]))}
                    for g in ("inland", "coastal", "all")},
            "region_effects": {g: {r: round(float(np.exp(v)), 3) for r, v in est[g]["reff"].items()}
                               for g in ("inland", "coastal", "all")},
            "clipped": int((clip_in != c_in).sum() + (clip_co != c_co).sum())}
    tab.to_csv(od / "nfip_state_factors.csv", index=False)
    (od / "nfip_state_factors.json").write_text(json.dumps(summ, indent=2))
    log.info("design: %s; tau %s", summ["design"], summ["tau"])
    pd.set_option("display.width", 260)
    log.info("state factors:\n%s", tab[["abbr", "region", "c_inland", "c_coastal", "raw_inland", "se_inland",
                                        "raw_coastal", "se_coastal", "model_inland_musd",
                                        "claims_inland_musd"]].to_string(index=False))
    # effect on the calibration counties
    d["c_in"] = d.st.map(dict(zip(tab.state_fips, tab.c_inland))).fillna(1.0)
    d["c_co"] = d.st.map(dict(zip(tab.state_fips, tab.c_coastal))).fillna(1.0)
    d["m_adj"] = d.m_inland * d.c_in + d.m_coastal * d.c_co
    from scipy.stats import spearmanr
    st_tab = d.groupby("st").agg(model=("m_adj", "sum"), model0=("aal_ins", "sum"), claims=("claims", "sum"))
    st_tab["model0"] *= lam
    w0 = (st_tab.model0 / st_tab.claims).between(0.5, 2).mean()
    w1 = (st_tab.model / st_tab.claims).between(0.5, 2).mean()
    log.info("calibration counties: ratio %.3f -> %.3f; county Spearman %.3f -> %.3f; states within 2x %.0f%% -> %.0f%%",
             lam * d.aal_ins.sum() / d.claims.sum(), d.m_adj.sum() / d.claims.sum(),
             spearmanr(d.aal_ins, d.claims).statistic, spearmanr(d.m_adj, d.claims).statistic, 100 * w0, 100 * w1)
    if apply:
        from floodrisk.config import PROJECT_ROOT
        out = PROJECT_ROOT / "config" / "regional_factors.csv"
        tab[["state_fips", "abbr", "region", "c_inland", "c_coastal"]].to_csv(out, index=False)
        log.info("state factors written to %s", out)
    return tab


def apply_best(log) -> dict:
    """Write the cross-validated best parameters into config/settings.yaml (deterministic rule)."""
    import re
    from floodrisk.config import PROJECT_ROOT
    best = json.loads((path("outputs", "validation") / "nfip_calibration.json").read_text())["best"]
    vals = {"fluvial_tc_natural": best["tn"], "fluvial_tc_urban": best["tu"], "pluvial_drain_rp": best["td"],
            "below_ffe_min_depth_m": best["thr"], "vulnerability_factor": round(float(best["lambda"]), 3)}
    fp = PROJECT_ROOT / "config" / "settings.yaml"
    txt = fp.read_text(encoding="utf-8")
    for k, v in vals.items():
        v = int(float(v)) if k in ("fluvial_tc_natural", "fluvial_tc_urban", "pluvial_drain_rp") else float(v)
        txt, n = re.subn(rf"(^\s*{k}:\s*)[-0-9.]+", rf"\g<1>{v}", txt, flags=re.M)
        if n != 1:
            raise RuntimeError(f"settings key {k} not found exactly once")
    fp.write_text(txt, encoding="utf-8")
    log.info("calibrated parameters written to %s: %s", fp, vals)
    return vals


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--huc8", nargs="*")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--min-pen", type=float, default=0.0)
    ap.add_argument("--apply", action="store_true", help="write the best parameters into config/settings.yaml")
    ap.add_argument("--regional", action="store_true",
                    help="state calibration factors on top of the national fit (with --apply: config/regional_factors.csv)")
    ap.add_argument("--grid-missing", action="store_true",
                    help="print the number of HUC-8s with pluvial output but no grid file")
    args = ap.parse_args()
    log = setup("14_calibrate_nfip")
    if args.grid_missing:
        from floodrisk import pipeline
        hucs = pipeline.huc8_list().huc8.tolist()
        pdir = path("interim", "hazard", "pluvial", "buildings")
        no_pl = [h for h in hucs if not (pdir / f"{h}.parquet").exists()]
        miss = [h for h in hucs if (pdir / f"{h}.parquet").exists() and not (GRID_DIR() / f"{h}.parquet").exists()]
        if no_pl:
            log.warning("%d HUC8 without pluvial output (not in the grid): %s", len(no_pl), no_pl[:20])
        if miss:
            log.warning("%d HUC8 missing from the grid: %s", len(miss), miss[:20])
        print(len(miss))
        return
    if args.grid:
        from floodrisk import pipeline
        hucs = args.huc8 or pipeline.huc8_list().huc8.tolist()
        od = GRID_DIR()
        todo = [h for h in hucs if args.overwrite or not (od / f"{h}.parquet").exists()]
        log.info("calibration grid: %d HUC8, %d combos", len(todo),
                 len(GRID["fluvial"]) * len(GRID["td"]) * len(GRID["thr"]))
        with ProcessPoolExecutor(args.workers) as ex:
            futs = [ex.submit(grid_one, h) for h in todo]
            for k, f in enumerate(as_completed(futs), 1):
                r = f.result()
                if r["status"].startswith("error"):
                    log.error("%s %s\n%s", r["huc8"], r["status"], r.get("trace", ""))
                elif k % 100 == 0 or r.get("sec", 0) > 300:
                    log.info("[%d/%d] %s", k, len(todo), r)
    if args.fit:
        fit(log, args.min_pen, "" if args.min_pen <= 0 else f"_pen{int(args.min_pen*100)}")
    if args.apply and not args.regional:
        apply_best(log)
    if args.regional:
        regional(log, apply=args.apply)


if __name__ == "__main__":
    main()
