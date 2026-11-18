"""Step 04 — national residential exposure (NSI 2026 -> residential, ZCTA, HUC-8)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from floodrisk.config import path  # noqa: E402
from floodrisk.exposure.build import finalize_by_state, stage_residential  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402


def main() -> None:
    log = setup("04_build_exposure")
    stage = path("interim", "exposure", "residential_stage.parquet")
    if not stage.exists():
        stage = stage_residential()
    out = finalize_by_state(stage)
    log.info("exposure written to %s", out)


if __name__ == "__main__":
    main()
