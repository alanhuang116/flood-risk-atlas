#!/usr/bin/env bash
# National production run (CONUS). Every step is resumable; rerun the script to continue.
#   bash scripts/run_national.sh            # full chain
#   STAGE=hazard bash scripts/run_national.sh   # start from hazards
set -u
cd "$(dirname "$0")/.."
PY="scripts/py.sh"
LOG=logs/run_national.log
unset FLOODRISK_EXPOSURE_ROOT
stage=${STAGE:-exposure}
say() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

# keep Windows out of (Modern) Standby while this script runs; released when it exits
WINPID=$(cat /proc/$$/winpid 2>/dev/null || echo 0)
$PY scripts/keep_awake.py --pid "$WINPID" >> logs/keep_awake.out 2>&1 &
say "== start (stage=$stage, winpid=$WINPID)"

if [ "$stage" = "exposure" ]; then
  say "== NSI: retry failed / missing counties"
  $PY scripts/01_download_nsi.py --workers 3 >> "$LOG" 2>&1
  say "== exposure build (national residential, ZCTA, HUC-8)"
  $PY scripts/04_build_exposure.py >> "$LOG" 2>&1
  stage=hazard
fi

if [ "$stage" = "hazard" ]; then
  say "== hazards: coastal+fluvial (CPU) || pluvial (GPU)"
  ( $PY scripts/08_run_hazard.py --peril coastal --all --workers 2 >> logs/run_nat_coastal.out 2>&1
    $PY scripts/08_run_hazard.py --peril fluvial --all --workers 5 >> logs/run_nat_fluvial.out 2>&1 ) &
  CPU_PID=$!
  $PY scripts/08_run_hazard.py --peril pluvial --all --workers 2 >> logs/run_nat_pluvial.out 2>&1 &
  GPU_PID=$!
  wait $CPU_PID; say "   coastal + fluvial finished"
  # while the GPU works, compute losses on the CPU for HUC-8s whose hazards are all finished
  while kill -0 $GPU_PID 2>/dev/null; do
    $PY scripts/09_compute_risk.py --all --ready --workers 3 >> "$LOG" 2>&1
    for _ in $(seq 60); do kill -0 $GPU_PID 2>/dev/null || break; sleep 30; done
  done
  wait $GPU_PID; say "   pluvial finished"
  say "   retry pass for any failed HUC-8s"
  $PY scripts/08_run_hazard.py --peril coastal --all --workers 1 >> logs/run_nat_coastal.out 2>&1
  $PY scripts/08_run_hazard.py --peril fluvial --all --workers 2 >> logs/run_nat_fluvial.out 2>&1
  $PY scripts/08_run_hazard.py --peril pluvial --all --workers 1 >> logs/run_nat_pluvial.out 2>&1
  stage=risk
fi

if [ "$stage" = "risk" ]; then
  say "== risk (building losses, AAL, RP losses)"
  $PY scripts/09_compute_risk.py --all --workers 8 >> "$LOG" 2>&1
  say "== ZIP aggregation"
  $PY scripts/10_aggregate_zip.py >> "$LOG" 2>&1
  say "== platform bundle"
  $PY scripts/11_build_platform.py --coverage "United States" >> "$LOG" 2>&1
fi
say "== done"
