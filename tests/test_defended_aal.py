import numpy as np

from floodrisk.risk.engine import aal_defended, defence_failure_p
from floodrisk.risk.metrics import aal_trapezoid

RPS = [2, 5, 10, 25, 50, 100, 250, 500, 1000]


def _curves(k=50, ns=3, seed=0):
    rng = np.random.default_rng(seed)
    D = np.sort(rng.uniform(0, 3, (k, ns, len(RPS))), axis=-1)
    L = D * 1e5
    return D, L


def test_undefended_equals_trapezoid():
    D, L = _curves()
    a = aal_defended(RPS, 2.0, {"fluvial": (D, L)}, {})
    assert np.allclose(a["fluvial"], aal_trapezoid(L, RPS, 2.0))
    assert np.allclose(a["combined"], aal_trapezoid(L, RPS, 2.0))


def test_no_protection_flag_equals_trapezoid():
    D, L = _curves()
    pf = defence_failure_p(D, np.zeros(len(D)), RPS)
    a = aal_defended(RPS, 2.0, {"coastal": (D, L)}, {"coastal": pf})
    assert np.allclose(a["coastal"], aal_trapezoid(L, RPS, 2.0), rtol=1e-6)


def test_failure_at_design_level():
    # depth rises linearly in RP index; 100-yr defence -> fails exactly at p = 0.01 in the baseline
    D = np.tile(np.arange(len(RPS), dtype=float)[None, None, :], (1, 1, 1))
    L = D * 1000.0
    pf = defence_failure_p(D, np.array([100.0]), RPS)
    assert np.isclose(pf[0, 0], 0.01)
    a = aal_defended(RPS, 2.0, {"fluvial": (D, L)}, {"fluvial": pf})
    p = 1.0 / np.array(RPS[5:], float)                          # 100 .. 1000
    l = L[0, 0, 5:]
    expect = np.sum(0.5 * (l[:-1] + l[1:]) * (p[:-1] - p[1:])) + l[-1] * p[-1]
    assert np.isclose(a["fluvial"][0, 0], expect)


def test_continuous_in_climate_shift():
    base = np.linspace(0, 4, len(RPS))
    vals = []
    for shift in (0.0, 0.01, 0.02, 0.05):
        D = np.stack([base, base + shift])[None, :, :]          # scenario 0 = baseline, 1 = shifted
        L = D * 1e5
        pf = defence_failure_p(D, np.array([100.0]), RPS, base_idx=0)
        vals.append(aal_defended(RPS, 2.0, {"coastal": (D, L)}, {"coastal": pf})["coastal"][0, 1])
    d = np.diff(vals)
    assert np.all(d > 0) and d[0] < 0.2 * vals[0]                # small shift -> small, positive change
