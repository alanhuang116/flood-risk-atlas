"""Risk metrics: average annual loss (AAL) and return-period losses.

AAL is the integral of loss over annual exceedance probability (AEP):
    AAL = integral_0^1 L(p) dp
approximated with the trapezoidal rule over the modelled return periods, with
  * no damage for events more frequent than ``floor_rp`` (default 2-yr), and
  * the loss beyond the largest return period held constant (tail = L_Tmax * p_Tmax).
"""
from __future__ import annotations

import numpy as np


def aal_trapezoid(losses: np.ndarray, return_periods: list[float] | np.ndarray,
                  floor_rp: float = 2.0, tail: str = "hold-constant") -> np.ndarray:
    """Vectorised AAL.

    losses: (..., n_rp) array aligned with ``return_periods`` (ascending T).
    """
    T = np.asarray(return_periods, dtype=np.float64)
    if np.any(np.diff(T) <= 0):
        raise ValueError("return periods must be strictly increasing")
    L = np.asarray(losses, dtype=np.float64)
    keep = T >= floor_rp
    T, L = T[keep], L[..., keep]
    p = 1.0 / T
    dp = p[:-1] - p[1:]                                   # positive
    aal = np.sum(0.5 * (L[..., :-1] + L[..., 1:]) * dp, axis=-1)
    if tail == "hold-constant":
        aal = aal + L[..., -1] * p[-1]
    elif tail != "none":
        raise ValueError(tail)
    return aal


def interp_log_rp(values: np.ndarray, rp_in: list[float], rp_out: list[float]) -> np.ndarray:
    """Interpolate a (..., n_rp) array to new return periods, linear in log(T)."""
    x_in = np.log(np.asarray(rp_in, dtype=np.float64))
    x_out = np.log(np.asarray(rp_out, dtype=np.float64))
    v = np.asarray(values, dtype=np.float64)
    flat = v.reshape(-1, v.shape[-1])
    out = np.empty((flat.shape[0], len(rp_out)))
    for i in range(flat.shape[0]):
        out[i] = np.interp(x_out, x_in, flat[i])
    return out.reshape(*v.shape[:-1], len(rp_out))
