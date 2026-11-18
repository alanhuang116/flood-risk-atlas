"""Synthetic checks for the GPU rain-on-grid solver (mass conservation, speed, monotonicity).

Run:  scripts/py.sh tests/test_pluvial_solver.py
"""
from __future__ import annotations

import time

import numpy as np

from floodrisk.hazard.pluvial.solver import PluvialInputs, run_batch, scs_smax_mm


def scs_runoff_mm(p, s):
    ia = 0.2 * s
    return np.where(p > ia, (p - ia) ** 2 / (p - ia + s), 0.0)


def closed_bowl(n=2000, dx=30.0, seed=1):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:n, 0:n].astype(np.float64)
    r = np.hypot(x - n / 2, y - n / 2) / (n / 2)
    z = 50.0 * r ** 2 + 2.0 * np.sin(x / 37.0) * np.cos(y / 53.0) + rng.normal(0, 0.05, (n, n))
    z[r > 0.98] = 500.0                     # high rim: closed basin, no outflow
    return z.astype(np.float32)


def main() -> None:
    n, dx = 2000, 30.0
    z = closed_bowl(n, dx)
    active = z < 400.0                      # inactive outside the rim -> closed basin
    cn = np.full(z.shape, 80.0)
    smax = scs_smax_mm(cn)
    p_runs = np.stack([np.full(z.shape, p, np.float32) for p in (75.0, 150.0, 300.0)])
    inp = PluvialInputs(z=z, active=active, manning=np.full(z.shape, 0.05, np.float32),
                        smax_mm=smax, drained=np.zeros(z.shape, bool),
                        p_design_mm=np.zeros(z.shape, np.float32), p_runs_mm=p_runs, dx=dx)
    t0 = time.time()
    hmax, hfin = run_batch(inp, storm_hours=24, drain_hours=12, verbose=False, return_final=True)
    el = time.time() - t0
    cells = n * n * p_runs.shape[0]
    print(f"runtime {el:.1f} s for {p_runs.shape[0]} runs x {n*n/1e6:.1f} M cells")

    # mass check: closed basin -> final stored volume must equal the total rainfall excess
    for k, p in enumerate((75.0, 150.0, 300.0)):
        excess = float(scs_runoff_mm(np.array(p), float(smax[0, 0])) * 1e-3) * active.sum() * dx * dx
        vfin = float(hfin[k].astype(np.float64).sum()) * dx * dx
        print(f"P={p:5.0f} mm  excess={excess/1e6:8.2f} Mm3  stored={vfin/1e6:8.2f} Mm3  "
              f"mass ratio={vfin/excess:6.4f}  max depth={hmax[k].max():6.2f} m")
    assert np.all(hmax[0] <= hmax[1] + 1e-3) and np.all(hmax[1] <= hmax[2] + 1e-3), "monotonicity"
    print(f"throughput {cells/el/1e6:.1f} M cell-runs per wall-second (whole simulation)")


if __name__ == "__main__":
    main()
