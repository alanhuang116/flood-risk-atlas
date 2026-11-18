"""Keep Windows awake while a long pipeline runs (no power-plan changes).

Holds a SetThreadExecutionState request (system + display required) so idle timeouts never
put the machine into (Modern) Standby, and releases it automatically when the watched
process exits.  Usage:
    python scripts/keep_awake.py --pid <windows pid>     # until that process ends
    python scripts/keep_awake.py                         # until killed
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0, help="Windows PID to watch (0 = run until killed)")
    ap.add_argument("--no-display", action="store_true", help="allow the display to turn off")
    args = ap.parse_args()
    if sys.platform != "win32":
        return 0
    k32 = ctypes.windll.kernel32
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | (0 if args.no_display else ES_DISPLAY_REQUIRED)
    handle = k32.OpenProcess(SYNCHRONIZE, False, args.pid) if args.pid else None
    if args.pid and not handle:
        print(f"keep_awake: process {args.pid} not found", flush=True)
        return 1
    print(f"keep_awake: holding wake request (pid={args.pid or 'none'})", flush=True)
    try:
        while True:
            if not k32.SetThreadExecutionState(flags):
                print("keep_awake: SetThreadExecutionState failed", flush=True)
            if handle:
                if k32.WaitForSingleObject(handle, 60_000) != WAIT_TIMEOUT:
                    break                       # watched process ended
            else:
                time.sleep(60)
    finally:
        k32.SetThreadExecutionState(ES_CONTINUOUS)
        if handle:
            k32.CloseHandle(handle)
    print("keep_awake: released", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
