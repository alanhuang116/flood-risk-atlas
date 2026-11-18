"""USACE National Structure Inventory (NSI 2026 Base) — streaming county downloader.

The public NSI API (``/nsiapi/structures?fips=<county>&fmt=fs``) returns one GeoJSON
feature per line, which we parse in a streaming fashion and write to Parquet in
row-group batches so that even the largest counties use bounded memory.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from floodrisk.config import path, settings
from floodrisk.utils.http import make_session

log = logging.getLogger(__name__)

# Columns retained from NSI (name -> arrow type). Population is collapsed to day/night totals.
SCHEMA = pa.schema([
    ("fd_id", pa.int64()),
    ("x", pa.float64()),
    ("y", pa.float64()),
    ("cbfips", pa.string()),
    ("st_damcat", pa.string()),
    ("occtype", pa.string()),
    ("bldgtype", pa.string()),
    ("found_type", pa.string()),
    ("found_ht", pa.float32()),
    ("num_story", pa.int16()),
    ("sqft", pa.float32()),
    ("ftprntsqft", pa.float32()),
    ("bldheight", pa.float32()),
    ("resunits", pa.int32()),
    ("val_struct", pa.float64()),
    ("val_cont", pa.float64()),
    ("val_vehic", pa.float64()),
    ("fullrep", pa.float64()),
    ("depindex", pa.float32()),
    ("ground_elv", pa.float32()),
    ("grnd_elv_m", pa.float32()),
    ("firmzone", pa.string()),
    ("zone_sub", pa.string()),
    ("static_bfe", pa.float32()),
    ("med_yr_blt", pa.int16()),
    ("usastrucid", pa.string()),
    ("ftprntsrc", pa.string()),
    ("source", pa.string()),
    ("pop_night", pa.float32()),
    ("pop_day", pa.float32()),
])
_DIRECT = [f.name for f in SCHEMA if f.name not in ("pop_night", "pop_day")]
_INT_COLS = {f.name for f in SCHEMA if pa.types.is_integer(f.type)}
_FLOAT_COLS = {f.name for f in SCHEMA if pa.types.is_floating(f.type)}

# Connecticut replaced its 8 counties with 9 planning regions in 2022; query both sets.
CT_PLANNING_REGIONS = [f"09{c}" for c in ("110", "120", "130", "140", "150", "160", "170", "180", "190")]


def county_list() -> list[str]:
    """All 5-digit county FIPS (Census 2020 list + CT planning regions)."""
    src = path("raw", "census", "national_county2020.txt")
    if not src.exists():
        from floodrisk.utils.http import download
        download("https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt", src)
    fips = []
    with open(src, encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            p = line.rstrip("\n").split("|")
            if len(p) >= 3:
                fips.append(p[1] + p[2])
    fips += CT_PLANNING_REGIONS
    return sorted(set(fips))


def _coerce(name: str, v):
    if v is None or v == "":
        return None
    if name in _INT_COLS:
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return None
    if name in _FLOAT_COLS:   # ints > 2**24 cannot be cast to float32 by arrow -> pass floats
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return v


def _empty_cols() -> dict[str, list]:
    return {f.name: [] for f in SCHEMA}


def _iter_records(resp, chunk: int = 1 << 20):
    """Yield JSON records from a GeoJSON Text Sequence (RFC 8142: RS-prefixed, LF-terminated)."""
    buf = b""
    for block in resp.iter_content(chunk_size=chunk):
        if not block:
            continue
        buf += block
        parts = buf.split(b"\x1e")
        buf = parts.pop()            # last part may be incomplete
        for p in parts:
            p = p.strip()
            if p:
                yield p
    buf = buf.strip()
    if buf:
        yield buf


def download_county(fips: str, out_root: Path | None = None, batch_rows: int = 250_000,
                    attempts: int = 4) -> dict:
    """Stream one county from the NSI API into ``<out_root>/<state>/<fips>.parquet``."""
    out_root = Path(out_root or path("raw", "nsi"))
    dest = out_root / fips[:2] / f"{fips}.parquet"
    if dest.exists():
        return {"fips": fips, "status": "exists"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".parquet.part")
    url = settings()["exposure"]["nsi_api"]
    sess = make_session(pool=2)
    last_err = None
    for attempt in range(1, attempts + 1):
        t0 = time.time()
        n = n_res = 0
        nbytes = 0
        writer = None
        try:
            with sess.get(url, params={"fips": fips, "fmt": "fs"}, stream=True, timeout=(30, 900)) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                cols = _empty_cols()
                for line in _iter_records(r):
                    nbytes += len(line)
                    feat = json.loads(line)
                    props = feat.get("properties")
                    if props is None:  # error payloads look like {"message": ...}
                        if "message" in feat:
                            raise RuntimeError(f"API message: {feat['message']}")
                        continue
                    for k in _DIRECT:
                        cols[k].append(_coerce(k, props.get(k)))
                    cols["pop_night"].append((props.get("pop2amu65") or 0) + (props.get("pop2amo65") or 0))
                    cols["pop_day"].append((props.get("pop2pmu65") or 0) + (props.get("pop2pmo65") or 0))
                    n += 1
                    if props.get("st_damcat") == "RES":
                        n_res += 1
                    if len(cols["fd_id"]) >= batch_rows:
                        tbl = pa.Table.from_pydict(cols, schema=SCHEMA)
                        if writer is None:
                            writer = pq.ParquetWriter(tmp, SCHEMA, compression="zstd")
                        writer.write_table(tbl)
                        cols = _empty_cols()
                tbl = pa.Table.from_pydict(cols, schema=SCHEMA)
                if writer is None:
                    writer = pq.ParquetWriter(tmp, SCHEMA, compression="zstd")
                writer.write_table(tbl)
                writer.close()
                writer = None
            os.replace(tmp, dest)
            return {"fips": fips, "status": "ok", "n": n, "n_res": n_res,
                    "mb": round(nbytes / 1e6, 1), "sec": round(time.time() - t0, 1)}
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if writer is not None:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass
            if tmp.exists():
                tmp.unlink()
            time.sleep(min(120, 5 * 2 ** attempt))
    return {"fips": fips, "status": f"failed: {last_err}"}
