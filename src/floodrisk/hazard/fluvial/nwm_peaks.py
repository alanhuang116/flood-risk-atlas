"""Annual-maximum streamflow for every NWM v3.0 reach from the public retrospective Zarr store.

Source: s3://noaa-nwm-retrospective-3-0-pds/CONUS/zarr/chrtout.zarr  (anonymous HTTPS)
  streamflow: int32, shape (385704 hours, 2776734 reaches), chunks (672, 30000), zstd,
  scale_factor 0.01 m3/s, fill -999900, time = hours since 1979-02-01T01:00.

We stream every chunk once (~100 GB compressed), reduce it to per-water-year maxima on the
fly and checkpoint after each batch of time chunks so the job is resumable.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from numcodecs import Zstd

from floodrisk.config import path
from floodrisk.utils.http import make_session

log = logging.getLogger(__name__)

BASE = "https://noaa-nwm-retrospective-3-0-pds.s3.amazonaws.com/CONUS/zarr/chrtout.zarr"
FILL = -999900
SCALE = 0.01
T0 = np.datetime64("1979-02-01T01:00")
_codec = Zstd()


def _get(sess, key: str) -> bytes:
    for attempt in range(8):
        try:
            r = sess.get(f"{BASE}/{key}", timeout=120)
            if r.status_code == 200:
                return r.content
            if r.status_code == 404:
                raise FileNotFoundError(key)
            raise RuntimeError(f"HTTP {r.status_code}")
        except FileNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt == 7:
                raise
            time.sleep(min(60, 2 ** attempt))
            log.debug("retry %s: %s", key, exc)
    raise RuntimeError("unreachable")


def _meta(sess, var: str) -> dict:
    return json.loads(_get(sess, f"{var}/.zarray"))


def water_year(hours: np.ndarray) -> np.ndarray:
    """Water year (Oct-Sep) for hour offsets since T0."""
    dt = T0 + hours.astype("timedelta64[h]")
    yr = dt.astype("datetime64[Y]").astype(int) + 1970
    mon = dt.astype("datetime64[M]").astype(int) % 12 + 1
    return np.where(mon >= 10, yr + 1, yr)


class AnnualMaxJob:
    def __init__(self, out_dir: Path | None = None, threads: int = 24, batch_time_chunks: int = 6):
        self.out_dir = Path(out_dir or path("interim", "nwm", mkdir=True))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.sess = make_session(pool=threads + 4)
        self.threads = threads
        self.batch = batch_time_chunks
        m = _meta(self.sess, "streamflow")
        self.nt, self.nf = m["shape"]
        self.ct, self.cf = m["chunks"]
        self.n_tc = -(-self.nt // self.ct)
        self.n_fc = -(-self.nf // self.cf)
        hours = np.arange(self.nt, dtype=np.int64)
        self.wy = water_year(hours)
        self.wy0 = int(self.wy.min())
        self.n_wy = int(self.wy.max()) - self.wy0 + 1
        self.ckpt = self.out_dir / "annual_max_checkpoint.npz"

    # -- helpers -------------------------------------------------------------
    def feature_ids(self) -> np.ndarray:
        raw = _codec.decode(_get(self.sess, "feature_id/0"))
        return np.frombuffer(raw, dtype="<i8")[: self.nf].copy()

    def _task(self, i: int, j: int):
        raw = _codec.decode(_get(self.sess, f"streamflow/{i}.{j}"))
        a = np.frombuffer(raw, dtype="<i4").reshape(self.ct, self.cf)
        t_lo = i * self.ct
        t_hi = min(t_lo + self.ct, self.nt)
        a = a[: t_hi - t_lo]
        wy = self.wy[t_lo:t_hi]
        out = []
        for y in np.unique(wy):
            sel = wy == y
            out.append((int(y) - self.wy0, a[sel].max(axis=0)))
        return i, j, out

    # -- main ------------------------------------------------------------------
    def run(self) -> Path:
        if self.ckpt.exists():
            ck = np.load(self.ckpt)
            M = ck["M"]
            next_i = int(ck["next_i"])
            log.info("resuming from time chunk %d / %d", next_i, self.n_tc)
        else:
            M = np.full((self.n_wy, self.nf), FILL, dtype=np.int32)
            next_i = 0
        t_start = time.time()
        nbytes_note = 0
        with ThreadPoolExecutor(max_workers=self.threads) as ex:
            while next_i < self.n_tc:
                i_hi = min(next_i + self.batch, self.n_tc)
                futs = [ex.submit(self._task, i, j) for i in range(next_i, i_hi) for j in range(self.n_fc)]
                for fut in futs:
                    i, j, parts = fut.result()
                    f_lo = j * self.cf
                    f_hi = min(f_lo + self.cf, self.nf)
                    for yi, mx in parts:
                        np.maximum(M[yi, f_lo:f_hi], mx[: f_hi - f_lo], out=M[yi, f_lo:f_hi])
                next_i = i_hi
                np.savez(self.ckpt, M=M, next_i=next_i)
                el = time.time() - t_start
                log.info("time chunks %d/%d done | %.1f min elapsed", next_i, self.n_tc, el / 60)
                nbytes_note += 1
        return self.finalize(M)

    def finalize(self, M: np.ndarray) -> Path:
        fid = self.feature_ids()
        years = np.arange(self.wy0, self.wy0 + self.n_wy)
        q = M.astype(np.float32) * SCALE
        q[M == FILL] = np.nan
        df = pd.DataFrame(q.T, columns=[f"wy{y}" for y in years])
        df.insert(0, "feature_id", fid)
        out = self.out_dir / "nwm_v3_annual_max_wy.parquet"
        df.to_parquet(out, index=False, compression="zstd")
        log.info("wrote %s (%d reaches x %d water years)", out, len(df), len(years))
        return out
