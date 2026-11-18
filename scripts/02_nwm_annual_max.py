"""Step 02 — stream NWM v3.0 retrospective (1979-2023) and compute water-year annual maxima.

Usage:  python scripts/02_nwm_annual_max.py [--threads 24]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from floodrisk.hazard.fluvial.nwm_peaks import AnnualMaxJob  # noqa: E402
from floodrisk.utils.logs import setup  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=24)
    ap.add_argument("--batch", type=int, default=6, help="time chunks per checkpoint")
    args = ap.parse_args()
    log = setup("02_nwm_annual_max")
    job = AnnualMaxJob(threads=args.threads, batch_time_chunks=args.batch)
    log.info("streamflow %d hours x %d reaches; %d x %d chunks; water years %d..%d",
             job.nt, job.nf, job.n_tc, job.n_fc, job.wy0, job.wy0 + job.n_wy - 1)
    out = job.run()
    log.info("done: %s", out)


if __name__ == "__main__":
    main()
