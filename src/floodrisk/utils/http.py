"""Robust HTTP helpers: pooled session with retries, resumable atomic downloads."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

USER_AGENT = "floodrisk-research-pipeline/0.1 (python-requests)"


def make_session(pool: int = 16, retries: int = 6, backoff: float = 1.5) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": USER_AGENT})
    return s


_SESSION: requests.Session | None = None


def session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = make_session()
    return _SESSION


def atomic_parquet(df, dest, **kw) -> Path:
    """Write a DataFrame to Parquet atomically (temp file + rename) so killed jobs never leave
    half-written outputs that the resume logic would mistake for finished work."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    df.to_parquet(tmp, index=False, **kw)
    os.replace(tmp, dest)
    return dest


def head_size(url: str, timeout: int = 30) -> int | None:
    r = session().head(url, allow_redirects=True, timeout=timeout)
    if r.status_code != 200:
        return None
    n = r.headers.get("Content-Length")
    return int(n) if n is not None else None


def download(url: str, dest: Path, *, chunk: int = 8 << 20, timeout: int = 120,
             attempts: int = 5, overwrite: bool = False) -> Path:
    """Download ``url`` to ``dest`` atomically, resuming partial ``.part`` files."""
    dest = Path(dest)
    if dest.exists() and not overwrite:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, attempts + 1):
        try:
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            with session().get(url, stream=True, timeout=timeout, headers=headers) as r:
                if r.status_code == 416:  # already complete
                    break
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code} for {url}")
                mode = "ab" if (have and r.status_code == 206) else "wb"
                with open(part, mode) as fh:
                    for block in r.iter_content(chunk_size=chunk):
                        if block:
                            fh.write(block)
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("download attempt %d/%d failed for %s: %s", attempt, attempts, url, exc)
            if attempt == attempts:
                raise
            time.sleep(min(60, 2 ** attempt))
    os.replace(part, dest)
    return dest
