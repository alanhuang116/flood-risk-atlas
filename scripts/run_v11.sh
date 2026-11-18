#!/usr/bin/env bash
# Model v1.1 rebuild after the national hazard run (hazard layers unchanged):
#   1. post-FIRM shares + elevation classes from NFIP policies (04b; policies from 12b)
#   2. calibration grid v4 (post-FIRM elevation explicit), national fit (+ robustness fit), apply
#   3. state calibration factors (empirical Bayes, year-split validation), apply
#   4. national risk (fixed + raised defence crests), ZIP aggregation, platform bundle
# Usage:  bash scripts/run_v11.sh            # all
#         STAGE=fit bash scripts/run_v11.sh  # from the fit (grid finished)
#         STAGE=risk bash scripts/run_v11.sh # risk / aggregation / platform only
set -u
cd "$(dirname "$0")/.."
PY="scripts/py.sh"
LOG=logs/run_v11.log
say() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
WINPID=$(cat /proc/$$/winpid 2>/dev/null || echo 0)
$PY scripts/keep_awake.py --pid "$WINPID" >> logs/keep_awake.out 2>&1 &
stage=${STAGE:-postfirm}
say "== v1.1 start (stage=$stage, winpid=$WINPID)"
if [ -n "${WAIT_FLAG:-}" ]; then            # optional gate: wait until the file exists
  say "   waiting for $WAIT_FLAG"
  until [ -f "$WAIT_FLAG" ]; do sleep 20; done
  sleep 30
  say "   $WAIT_FLAG present"
fi

if [ "$stage" = "postfirm" ]; then
  say "== post-FIRM inputs (04b)"
  $PY scripts/12b_download_nfip_policies.py --workers 3 >> "$LOG" 2>&1
  $PY scripts/04b_postfirm.py >> "$LOG" 2>&1 || { say "!! 04b failed"; exit 2; }
  stage=grid
fi

if [ "$stage" = "grid" ]; then
  say "== calibration grid v4"
  for pass in 1 2; do
    $PY scripts/14_calibrate_nfip.py --grid --workers 6 >> "$LOG" 2>&1
  done
  missing=$($PY scripts/14_calibrate_nfip.py --grid-missing 2>> "$LOG" | tail -n 1)
  say "   HUC-8s with pluvial output but no grid file: $missing"
  if [ "${missing:-x}" != "0" ]; then
    say "!! calibration grid incomplete - stopping (rerun with STAGE=grid)"
    exit 3
  fi
  stage=fit
fi

if [ "$stage" = "fit" ]; then
  say "== national fit (robustness: penetration >= 2 %), then all counties + apply"
  $PY scripts/14_calibrate_nfip.py --fit --min-pen 0.02 >> "$LOG" 2>&1
  $PY scripts/14_calibrate_nfip.py --fit --apply >> "$LOG" 2>&1
  say "== state calibration factors"
  $PY scripts/14_calibrate_nfip.py --regional --apply >> "$LOG" 2>&1 || { say "!! regional failed"; exit 4; }
  stage=risk
fi

if [ "$stage" = "risk" ]; then
  export FLOODRISK_FORCE=1
  say "== national risk v1.1"
  $PY scripts/09_compute_risk.py --all --overwrite --workers 8 >> "$LOG" 2>&1
  say "== ZIP aggregation"
  $PY scripts/10_aggregate_zip.py >> "$LOG" 2>&1
  say "== platform bundle"
  $PY scripts/11_build_platform.py --coverage "United States" >> "$LOG" 2>&1
fi
say "== v1.1 finished"
