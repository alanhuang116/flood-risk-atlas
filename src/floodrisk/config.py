"""Project configuration and path helpers."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def settings() -> dict:
    """Return the parsed ``config/settings.yaml``."""
    with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def path(key: str, *parts: str | Path, mkdir: bool = False) -> Path:
    """Resolve a path from the ``paths`` section (relative to the project root).

    ``path("raw", "nsi")`` -> <root>/data/raw/nsi
    """
    base = Path(settings()["paths"][key])
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    p = base.joinpath(*parts)
    if mkdir:
        p.mkdir(parents=True, exist_ok=True)
    return p


def return_periods() -> list[int]:
    return list(settings()["hazard"]["return_periods"])


def scenarios() -> dict[str, dict]:
    return dict(settings()["scenarios"])


def paused(step: str) -> bool:
    """True while data/interim/PAUSE_RISK exists (risk / aggregation / platform steps are skipped so that
    the automated national run does not publish uncalibrated results). Override: FLOODRISK_FORCE=1."""
    import os
    flag = path("interim", "PAUSE_RISK")
    if flag.exists() and os.environ.get("FLOODRISK_FORCE") != "1":
        import logging
        logging.getLogger(step).warning("%s skipped: %s exists (set FLOODRISK_FORCE=1 to run)", step, flag)
        return True
    return False
