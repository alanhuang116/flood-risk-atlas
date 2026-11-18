"""Logging setup shared by pipeline scripts."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from floodrisk.config import path


def setup(name: str, level: int = logging.INFO) -> logging.Logger:
    log_dir = path("logs", mkdir=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(Path(log_dir) / f"{name}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    return logging.getLogger(name)
