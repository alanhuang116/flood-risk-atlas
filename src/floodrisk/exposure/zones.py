"""FEMA flood-zone classes of the NSI ``firmzone`` field.

The NSI uses the FIRM zone codes plus labels for unmapped land ("AREA NOT INCLUDED", "D", "OPEN WATER",
null). Zone tests must use exact codes: a prefix test such as ``firmzone LIKE 'A%'`` wrongly counts
"AREA NOT INCLUDED" (0.78 M homes, e.g. most of Portland, OR) as an A zone.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_NUMBERED = {f"A{i}" for i in range(1, 31)} | {f"V{i}" for i in range(1, 31)}
A_ZONES = {"A", "AE", "AH", "AO"} | {f"A{i}" for i in range(1, 31)}
V_ZONES = {"V", "VE"} | {f"V{i}" for i in range(1, 31)}
SFHA = A_ZONES | V_ZONES | {"A99", "AR"}          # Special Flood Hazard Area (mandatory purchase applies)
ELEVATION_RULE = A_ZONES | V_ZONES                # NFIP elevation rule for new construction (44 CFR 60.3)


def _norm(firmzone: pd.Series) -> pd.Series:
    return firmzone.fillna("").astype(str).str.upper().str.strip()


def is_sfha(firmzone: pd.Series) -> np.ndarray:
    return _norm(firmzone).isin(SFHA).to_numpy()


def elevation_rule(firmzone: pd.Series) -> np.ndarray:
    """Homes in zones where post-FIRM construction must be elevated to the BFE (A, AE, AH, AO, V, VE)."""
    return _norm(firmzone).isin(ELEVATION_RULE).to_numpy()


def is_v(firmzone: pd.Series) -> np.ndarray:
    return _norm(firmzone).isin(V_ZONES).to_numpy()


def sql_in(zones: set) -> str:
    """SQL list for ``upper(trim(firmzone)) IN (...)``."""
    return "(" + ", ".join(f"'{z}'" for z in sorted(zones)) + ")"
