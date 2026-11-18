"""Channel capacity missing from the 30 m DEM, and the calibrated effective capacity.

HAND rating curves see the channel only above the DEM water surface, so the in-channel
conveyance must be added back. We assume the channel conveys the flow of return period Tc
before water leaves the banks:
    natural channels (developed fraction of the local catchment <= 10 %): Tc = Tn
    urban / engineered channels (>= 60 % developed):                      Tc = Tu
    log-linear in between.
Q_cap is interpolated from the reach flood-frequency curve in log(T).

Two capacities are used (fluvial depth = SRC((Q_T - Q_cap(Tn, Tu)) / k) - HAND):
  * the rating anchor (ANCHOR_NATURAL = 2 yr = bankfull, ANCHOR_URBAN = 10 yr) fixes the reach
    multiplier k so that the 100-year flood reproduces the FEMA SFHA (calibrate.recompute_k). k
    therefore never depends on the NFIP-calibrated values below;
  * the effective capacity (Tn, Tu; config risk.calibration, NFIP-calibrated) is subtracted from
    the flow. Above the anchor it lowers all flood stages, smoothly and without changing k.
Anchoring k at the calibrated capacity instead degenerates as Tc approaches 100 years
(Q100 - Q_cap -> 0 makes k -> 0, or k = K_MAX when Tc >= 100), which is why the two are separate.
"""
from __future__ import annotations

import numpy as np

ANCHOR_NATURAL, ANCHOR_URBAN = 2.0, 10.0   # rating anchor (physical defaults)
T_NATURAL, T_URBAN = ANCHOR_NATURAL, ANCHOR_URBAN   # defaults when no calibrated value is given
DEV_LO, DEV_HI = 0.10, 0.60


def capacity_rp(dev_frac: float, t_natural: float | None = None, t_urban: float | None = None) -> float:
    tn = float(t_natural or T_NATURAL)
    tu = float(t_urban or T_URBAN)
    w = float(np.clip((dev_frac - DEV_LO) / (DEV_HI - DEV_LO), 0.0, 1.0))
    return float(np.exp(np.log(tn) + w * (np.log(tu) - np.log(tn))))


def capacity_flow(q_by_rp: dict[int, float], dev_frac: float, t_natural: float | None = None,
                  t_urban: float | None = None) -> float:
    """Interpolate the reach flood-frequency curve at Tc (log-T)."""
    tc = capacity_rp(dev_frac if np.isfinite(dev_frac) else 0.0, t_natural, t_urban)
    T = np.array(sorted(q_by_rp), dtype=float)
    q = np.array([q_by_rp[t] for t in sorted(q_by_rp)], dtype=float)
    return float(np.interp(np.log(tc), np.log(T), q))
