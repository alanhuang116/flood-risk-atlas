#!/usr/bin/env bash
# Finalisation after the national hazard run (v1.0), fully automatic:
#   1. rerun pluvial for the HUC-8s that lacked Atlas 14 rainfall (now gap-filled with NOAA Atlas 2),
#      then any HUC-8 still without pluvial output
#   2. calibration grid for all HUC-8s (county x SFHA, exact defended AAL), fit against NFIP claims,
#      write the cross-validated best parameters into config/settings.yaml
#   3. national risk with --overwrite, ZIP aggregation, platform bundle
# Usage:  bash scripts/run_finalize.sh                    # 1-3
#         STAGE=leftovers bash scripts/run_finalize.sh    # pluvial leftovers, then 2-3
#         STAGE=calibrate bash scripts/run_finalize.sh    # 2-3
#         STAGE=risk bash scripts/run_finalize.sh         # 3 only
# Gates (optional, checked in this order before the first stage):
#   WAIT_NATIONAL=1      wait for "== done" in logs/run_national.log
#   WAIT_WINPID=<pid>    wait until that Windows process has exited
#   WAIT_FLAG=<file>     wait until the file exists
set -u
cd "$(dirname "$0")/.."
PY="scripts/py.sh"
LOG=logs/run_finalize.log
say() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
WINPID=$(cat /proc/$$/winpid 2>/dev/null || echo 0)
$PY scripts/keep_awake.py --pid "$WINPID" >> logs/keep_awake.out 2>&1 &
stage=${STAGE:-hazard}
say "== finalize start (stage=$stage, winpid=$WINPID)"

if [ "${WAIT_NATIONAL:-0}" = "1" ]; then
  say "   waiting for scripts/run_national.sh to finish"
  until grep -q "== done" logs/run_national.log; do sleep 120; done
  say "   national run finished"
fi
if [ -n "${WAIT_WINPID:-}" ]; then
  say "   waiting for process $WAIT_WINPID to exit"
  while tasklist //FI "PID eq $WAIT_WINPID" 2>/dev/null | grep -q " $WAIT_WINPID "; do sleep 60; done
  say "   process $WAIT_WINPID has exited"
fi
if [ -n "${WAIT_FLAG:-}" ]; then
  say "   waiting for $WAIT_FLAG"
  until [ -f "$WAIT_FLAG" ]; do sleep 30; done
  say "   $WAIT_FLAG present"
fi

if [ "$stage" = "hazard" ]; then
  say "== pluvial rerun for Atlas-2 gap-filled HUC-8s"
  for h in $(cat data/interim/pnw_gap_huc8.txt); do
    rm -f "data/interim/hazard/pluvial/buildings/$h.parquet"
  done
  $PY scripts/08_run_hazard.py --peril pluvial --huc8 $(cat data/interim/pnw_gap_huc8.txt) --workers 2 >> logs/run_nat_pluvial.out 2>&1
  stage=leftovers
fi

if [ "$stage" = "leftovers" ]; then
  say "== pluvial leftovers"
  $PY scripts/08_run_hazard.py --peril pluvial --all --workers 1 >> logs/run_nat_pluvial.out 2>&1
  stage=calibrate
fi

if [ "$stage" = "calibrate" ]; then
  say "== calibration grid (all HUC-8s; existing grid files are kept)"
  for pass in 1 2; do                       # second pass retries HUC-8s that failed (e.g. memory)
    $PY scripts/14_calibrate_nfip.py --grid --workers 6 >> "$LOG" 2>&1
  done
  missing=$($PY scripts/14_calibrate_nfip.py --grid-missing 2>> "$LOG" | tail -n 1)
  say "   HUC-8s with pluvial output but no grid file: $missing"
  if [ "${missing:-x}" != "0" ]; then
    say "!! calibration grid incomplete - stopping before fit/apply (rerun with STAGE=calibrate)"
    exit 3
  fi
  say "== calibration fit + apply"
  $PY scripts/14_calibrate_nfip.py --fit --min-pen 0.02 >> "$LOG" 2>&1
  $PY scripts/14_calibrate_nfip.py --fit --apply >> "$LOG" 2>&1
  stage=risk
fi

if [ "$stage" = "risk" ]; then
  export FLOODRISK_FORCE=1
  say "== national risk (calibrated)"
  $PY scripts/09_compute_risk.py --all --overwrite --workers 8 >> "$LOG" 2>&1
  say "== ZIP aggregation"
  $PY scripts/10_aggregate_zip.py >> "$LOG" 2>&1
  say "== platform bundle"
  $PY scripts/11_build_platform.py --coverage "United States" >> "$LOG" 2>&1
fi
say "== finalize finished"
