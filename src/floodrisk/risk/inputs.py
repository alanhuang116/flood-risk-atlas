"""Assemble the per-HUC-8 inputs of the loss model (exposure, hazard tables, calibration)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from floodrisk.config import path, return_periods


def read(p: Path) -> pd.DataFrame | None:
    return pd.read_parquet(p) if p.exists() else None


def load_components(huc8: str):
    """-> (exposure, Components) with all hazard tables of the HUC-8 (None where absent)."""
    from floodrisk import pipeline
    from floodrisk.risk.engine import Components
    hz = path("interim", "hazard")
    ex = pipeline.load_buildings(huc8)
    fb = read(hz / "fluvial" / "buildings" / f"{huc8}.parquet")
    src = read(hz / "fluvial" / "src" / f"{huc8}.parquet")
    comps = Components(
        exposure=ex, fluvial_bld=fb, src=src,
        flows=pipeline.flood_quantiles() if fb is not None else None,
        qmean=pipeline.qmean_by_comid() if fb is not None else None,
        pluvial_bld=read(hz / "pluvial" / "buildings" / f"{huc8}.parquet"),
        coastal_bld=read(hz / "coastal" / "buildings" / f"{huc8}.parquet"))
    return ex, comps


def fluvial_k(huc8: str, comps, prm=None, save: bool = True):
    """Reach conveyance multipliers: FEMA-informed reaches (step 09a) + regional drainage-area fallback.

    k is anchored at the physical channel capacity (capacity.ANCHOR_*), independent of the
    NFIP-calibrated effective capacity in ``prm`` (which enters only fluvial_depths); ``prm`` is
    accepted for call compatibility. Returns (reach_dev, k by comid) or (None, None) without fluvial data."""
    from floodrisk.hazard.fluvial.calibrate import (calibrate_reaches, complete_calibration, fallback_table,
                                                    recompute_k)
    from floodrisk.hazard.fluvial.capacity import ANCHOR_NATURAL, ANCHOR_URBAN
    fb, src = comps.fluvial_bld, comps.src
    if fb is None or src is None or len(src) == 0:
        return None, None
    hz = path("interim", "hazard")
    ra = read(hz / "fluvial" / "reaches" / f"{huc8}.parquet")
    dev = ra.set_index("comid").dev_frac if ra is not None else None
    raw = read(hz / "fluvial" / "calib_raw" / f"{huc8}.parquet")
    if raw is None:
        raw = calibrate_reaches(fb, comps.exposure, src, comps.flows, None, comps.coastal_bld, reach_dev=dev,
                                rps=return_periods(), informed_only=True)
    cal = recompute_k(raw.drop(columns=["huc8"], errors="ignore"), comps.flows, dev, return_periods(),
                      ANCHOR_NATURAL, ANCHOR_URBAN)
    cal = complete_calibration(cal, src.comid.unique(), huc8[:2], fallback_table(ANCHOR_NATURAL, ANCHOR_URBAN))
    if save:
        from floodrisk.utils.http import atomic_parquet
        atomic_parquet(cal.assign(huc8=huc8), path("interim", "hazard", "fluvial", "calib", mkdir=True) / f"{huc8}.parquet")
    return dev, cal.set_index("comid").k
