"""GPU rain-on-grid solver (local-inertial shallow-water equations) for pluvial flooding.

Numerics: Bates et al. (2010, J. Hydrol.) / de Almeida et al. (2012, WRR) local-inertial
formulation on a regular grid with semi-implicit Manning friction, Froude-limited fluxes
and an adaptive CFL time step.  Several rainfall scenarios (return periods / climate
multipliers) are integrated simultaneously as a batch sharing the same terrain.

Rainfall -> runoff:
  * 24-h design storm distributed with the NRCS Type II dimensionless hyetograph;
  * SCS curve-number losses evaluated cell-by-cell on cumulative rainfall;
  * urban storm-drain capacity: in drained cells the runoff of the design storm
    (``P_design``, e.g. 5-yr 24-h) is removed at every time step (sewers cope with
    storms up to the design return period).

Boundary: cells outside ``mask`` are inactive; faces between active and inactive/ocean
cells are free-outfall (critical-flow) outflow boundaries.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

try:
    import cupy as cp
except Exception:  # pragma: no cover - CPU-only environments
    cp = None

G = 9.81

# NRCS Type II cumulative 24-h rainfall fraction (TR-55, Appendix B)
TYPE_II_T = np.array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 11.5, 11.75, 12, 12.5, 13, 14, 15,
                      16, 17, 18, 19, 20, 21, 22, 23, 24], dtype=np.float64)
TYPE_II_F = np.array([0, .011, .022, .034, .048, .063, .080, .098, .120, .147, .181, .235, .283,
                      .357, .663, .735, .772, .820, .854, .880, .902, .921, .938, .952, .965,
                      .977, .989, 1.0], dtype=np.float64)

_KERNELS = r"""
extern "C" {

__device__ __forceinline__ float froude_cap(float q, float hf) {
    float qmax = hf * sqrtf(9.81f * hf);
    return fminf(fmaxf(q, -qmax), qmax);
}

// x-faces: (nr, ny, nx+1); face j sits between cells j-1 and j
__global__ void flux_x(const float* __restrict__ h, const float* __restrict__ z,
                       const float* __restrict__ n2, const unsigned char* __restrict__ act,
                       float* __restrict__ qx, const float dt, const float dx,
                       const float hmin, const int nx, const int ny, const int nr)
{
    long tid = (long)blockIdx.x * blockDim.x + threadIdx.x;
    long nf = (long)ny * (nx + 1);
    if (tid >= nf * nr) return;
    int r = tid / nf; long f = tid % nf;
    int i = f / (nx + 1); int j = f % (nx + 1);
    long base = (long)r * ny * nx;
    int jl = j - 1, jr = j;
    bool al = (jl >= 0) && act[(long)i * nx + jl];
    bool ar = (jr < nx) && act[(long)i * nx + jr];
    float q = qx[tid];
    if (!al && !ar) { qx[tid] = 0.f; return; }
    if (al && ar) {
        long cl = (long)i * nx + jl, cr = (long)i * nx + jr;
        float hl = h[base + cl], hr = h[base + cr];
        float etl = hl + z[cl], etr = hr + z[cr];
        float hf = fmaxf(etl, etr) - fmaxf(z[cl], z[cr]);
        if (hf <= hmin) { qx[tid] = 0.f; return; }
        float s = (etr - etl) / dx;
        float nn = 0.5f * (n2[cl] + n2[cr]);
        float qn = (q - 9.81f * hf * dt * s) / (1.f + 9.81f * dt * nn * fabsf(q) / powf(hf, 2.333333f));
        qx[tid] = froude_cap(qn, hf);
        return;
    }
    // boundary face: free outfall from the active cell
    long c = al ? ((long)i * nx + jl) : ((long)i * nx + jr);
    float hc = h[base + c];
    float qo = (hc > hmin) ? 0.5f * sqrtf(9.81f) * powf(hc, 1.5f) : 0.f;
    qx[tid] = al ? qo : -qo;    // +x direction is out of a left (active) cell
}

// y-faces: (nr, ny+1, nx); face i sits between cells i-1 and i
__global__ void flux_y(const float* __restrict__ h, const float* __restrict__ z,
                       const float* __restrict__ n2, const unsigned char* __restrict__ act,
                       float* __restrict__ qy, const float dt, const float dx,
                       const float hmin, const int nx, const int ny, const int nr)
{
    long tid = (long)blockIdx.x * blockDim.x + threadIdx.x;
    long nf = (long)(ny + 1) * nx;
    if (tid >= nf * nr) return;
    int r = tid / nf; long f = tid % nf;
    int i = f / nx; int j = f % nx;
    long base = (long)r * ny * nx;
    int iu = i - 1, id = i;
    bool au = (iu >= 0) && act[(long)iu * nx + j];
    bool ad = (id < ny) && act[(long)id * nx + j];
    float q = qy[tid];
    if (!au && !ad) { qy[tid] = 0.f; return; }
    if (au && ad) {
        long cu = (long)iu * nx + j, cd = (long)id * nx + j;
        float hu = h[base + cu], hd = h[base + cd];
        float etu = hu + z[cu], etd = hd + z[cd];
        float hf = fmaxf(etu, etd) - fmaxf(z[cu], z[cd]);
        if (hf <= hmin) { qy[tid] = 0.f; return; }
        float s = (etd - etu) / dx;
        float nn = 0.5f * (n2[cu] + n2[cd]);
        float qn = (q - 9.81f * hf * dt * s) / (1.f + 9.81f * dt * nn * fabsf(q) / powf(hf, 2.333333f));
        qy[tid] = froude_cap(qn, hf);
        return;
    }
    long c = au ? ((long)iu * nx + j) : ((long)id * nx + j);
    float hc = h[base + c];
    float qo = (hc > hmin) ? 0.5f * sqrtf(9.81f) * powf(hc, 1.5f) : 0.f;
    qy[tid] = au ? qo : -qo;
}

__device__ __forceinline__ float scs_q(float p, float s) {   // cumulative runoff (mm)
    float ia = 0.2f * s;
    if (p <= ia) return 0.f;
    float d = p - ia;
    return d * d / (d + s);
}

// depth update + rainfall excess + running maximum
__global__ void update_h(float* __restrict__ h, float* __restrict__ hmax,
                         const float* __restrict__ qx, const float* __restrict__ qy,
                         const unsigned char* __restrict__ act,
                         const float* __restrict__ ptot, const float* __restrict__ pdes,
                         const float* __restrict__ smax, const unsigned char* __restrict__ drained,
                         const float f0, const float f1, const float dt, const float dx,
                         const int nx, const int ny, const int nr, float* __restrict__ vout)
{
    long tid = (long)blockIdx.x * blockDim.x + threadIdx.x;
    long nc = (long)ny * nx;
    if (tid >= nc * nr) return;
    int r = tid / nc; long c = tid % nc;
    if (!act[c]) return;
    int i = c / nx; int j = c % nx;
    long fx = (long)r * ny * (nx + 1) + (long)i * (nx + 1) + j;
    long fy = (long)r * (ny + 1) * nx + (long)i * nx + j;
    float net = qx[fx] - qx[fx + 1] + qy[fy] - qy[fy + nx];   // m2/s into the cell
    float s = smax[c];
    float p = ptot[tid];
    float inc = scs_q(p * f1, s) - scs_q(p * f0, s);           // mm
    if (drained[c]) {
        float pd = pdes[c];
        inc -= (scs_q(pd * f1, s) - scs_q(pd * f0, s));
        inc = fmaxf(inc, 0.f);
    }
    float hn = h[tid] + dt * net / dx + inc * 1e-3f;
    if (hn < 0.f) hn = 0.f;
    h[tid] = hn;
    if (hn > hmax[tid]) hmax[tid] = hn;
}

}
"""


@dataclass
class PluvialInputs:
    z: np.ndarray            # (ny, nx) bed elevation [m]
    active: np.ndarray       # (ny, nx) bool, cells inside the domain (False = outside / ocean)
    manning: np.ndarray      # (ny, nx) Manning n
    smax_mm: np.ndarray      # (ny, nx) SCS potential retention S [mm] = 25400/CN - 254
    drained: np.ndarray      # (ny, nx) bool, urban cells with storm-drain capacity
    p_design_mm: np.ndarray  # (ny, nx) 24-h design-storm depth for drained cells [mm]
    p_runs_mm: np.ndarray    # (nr, ny, nx) 24-h rainfall depth per run [mm]
    dx: float


def run_batch(inp: PluvialInputs, storm_hours: float = 24.0, drain_hours: float = 6.0,
              cfl: float = 0.7, hmin: float = 1e-3, dt_max: float = 30.0,
              block: int = 256, verbose: bool = False, return_final: bool = False):
    """Integrate all runs; returns max depth (nr, ny, nx) float32 [m] (and final depth)."""
    if cp is None:
        raise RuntimeError("CuPy is required for the pluvial solver")
    mod = cp.RawModule(code=_KERNELS, options=("-use_fast_math",))
    k_fx, k_fy, k_up = (mod.get_function(n) for n in ("flux_x", "flux_y", "update_h"))

    nr, ny, nx = inp.p_runs_mm.shape
    f32 = cp.float32
    z = cp.asarray(np.where(inp.active, inp.z, 0.0), dtype=f32)
    act = cp.asarray(inp.active.astype(np.uint8))
    n2 = cp.asarray(inp.manning.astype(np.float32) ** 2)
    smax = cp.asarray(inp.smax_mm.astype(np.float32))
    drained = cp.asarray(inp.drained.astype(np.uint8))
    pdes = cp.asarray(inp.p_design_mm.astype(np.float32))
    ptot = cp.asarray(inp.p_runs_mm.astype(np.float32))
    h = cp.zeros((nr, ny, nx), dtype=f32)
    hmax = cp.zeros_like(h)
    qx = cp.zeros((nr, ny, nx + 1), dtype=f32)
    qy = cp.zeros((nr, ny + 1, nx), dtype=f32)
    vout = cp.zeros(1, dtype=f32)

    n_fx, n_fy, n_c = qx.size, qy.size, h.size
    grid = lambda n: ((n + block - 1) // block,)  # noqa: E731
    t, t_end = 0.0, (storm_hours + drain_hours) * 3600.0
    t_storm = storm_hours * 3600.0
    dx = float(inp.dx)
    step = 0
    hpeak = 0.05
    while t < t_end:
        if step % 10 == 0:
            hpeak = max(float(h.max()), 0.05)
        dt = min(dt_max, cfl * dx / math.sqrt(G * hpeak), t_end - t)
        f0 = float(np.interp(min(t, t_storm) / 3600.0, TYPE_II_T * storm_hours / 24.0, TYPE_II_F))
        f1 = float(np.interp(min(t + dt, t_storm) / 3600.0, TYPE_II_T * storm_hours / 24.0, TYPE_II_F))
        k_fx(grid(n_fx), (block,), (h, z, n2, act, qx, f32(dt), f32(dx), f32(hmin),
                                     np.int32(nx), np.int32(ny), np.int32(nr)))
        k_fy(grid(n_fy), (block,), (h, z, n2, act, qy, f32(dt), f32(dx), f32(hmin),
                                     np.int32(nx), np.int32(ny), np.int32(nr)))
        k_up(grid(n_c), (block,), (h, hmax, qx, qy, act, ptot, pdes, smax, drained,
                                    f32(f0), f32(f1), f32(dt), f32(dx),
                                    np.int32(nx), np.int32(ny), np.int32(nr), vout))
        t += dt
        step += 1
        if verbose and step % 2000 == 0:
            print(f"  t={t/3600:6.2f} h  dt={dt:5.2f}s  hpeak={hpeak:6.2f} m  steps={step}")
    cp.cuda.Stream.null.synchronize()
    if return_final:
        return cp.asnumpy(hmax), cp.asnumpy(h)
    return cp.asnumpy(hmax)


def scs_smax_mm(cn: np.ndarray) -> np.ndarray:
    cn = np.clip(cn.astype(np.float64), 30, 99.5)
    return (25400.0 / cn - 254.0).astype(np.float32)
