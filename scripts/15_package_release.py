"""Package and publish a data release on GitHub: platform data, map tiles and the ZIP product tables.

Writes data/release/v<version>/ (version from config/settings.yaml):

  platform-data.zip             platform/data without the map tiles and the full CSV: meta.json, zips.json,
                                downloads.json, map bins (m/), return-period curves (rp/), data dictionary
  zcta.pmtiles                  ZIP polygons (vector tiles)
  zip_flood_risk_long.csv.zip   product table: ZIP x defence assumption x scenario-year x flood type (CSV, zipped)
  zip_flood_risk_long.parquet   the same table as Parquet
  zip_flood_risk_wide.parquet   one row per ZIP, metric__flood type__scenario columns
  summary_state.csv, summary_national.csv, data_dictionary.csv, SHA256SUMS.txt

and platform/data/downloads.json, which points the data page at the release files.

With --upload the files are published as release v<version> of the GitHub repository (created as a draft and
published once every file is up; an existing release has its files replaced), then the Pages workflow
(.github/workflows/pages.yml) is started: it deploys platform/ together with the latest release's data.
Release notes for a new release are read from docs/releases/v<version>.md when that file exists.

Usage:  scripts/py.sh scripts/15_package_release.py [--upload] [--repo OWNER/NAME] [--notes FILE]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from floodrisk.config import PROJECT_ROOT, path, settings

PLATFORM_DATA = PROJECT_ROOT / "platform" / "data"

# release file, source (paths key, file name), title, description, format
PRODUCTS = [
    ("zip_flood_risk_long.csv.zip", ("outputs", "zip_flood_risk_long.csv"), "ZIP flood risk — full table",
     "All ZIPs × 2 defence assumptions × 5 scenario-years × 4 flood types", "CSV, zipped"),
    ("zip_flood_risk_long.parquet", ("outputs", "zip_flood_risk_long.parquet"), "Full table (Parquet)",
     "The same table with typed columns, for Python, R or DuckDB", "Parquet"),
    ("zip_flood_risk_wide.parquet", ("outputs", "zip_flood_risk_wide.parquet"), "Wide table (Parquet)",
     "One row per ZIP; columns named metric__flood type__scenario", "Parquet"),
    ("summary_state.csv", ("outputs", "summary_state.csv"), "State summary",
     "Buildings, value, AAL, 1-in-100 and 1-in-1,000 loss by state, scenario-year and flood type", "CSV"),
    ("summary_national.csv", ("outputs", "summary_national.csv"), "National summary",
     "US totals by defence assumption, scenario-year and flood type", "CSV"),
]


def github_repo(arg: str | None) -> str:
    if arg:
        return arg
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=PROJECT_ROOT, capture_output=True,
                         text=True).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        sys.exit(f"no GitHub remote 'origin' ({url or 'not set'}); pass --repo OWNER/NAME")
    return m.group(1)


def link_or_copy(src: Path, dst: Path) -> None:
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)          # same volume: no second copy of the large files
    except OSError:
        shutil.copy2(src, dst)


def zip_file(src: Path, dst: Path) -> None:
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return
    tmp = dst.with_name(dst.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(src, src.name)
    tmp.replace(dst)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def publish(repo: str, tag: str, assets: list[Path], notes: Path | None) -> None:
    new = subprocess.run(["gh", "release", "view", tag, "-R", repo], capture_output=True).returncode != 0
    if new:
        cmd = ["gh", "release", "create", tag, "-R", repo, "--draft", "--target", "main",
               "--title", f"Flood Risk Atlas {tag}"]
        cmd += ["--notes-file", str(notes)] if notes else ["--notes", f"Data release {tag}."]
        subprocess.run(cmd, check=True)
    subprocess.run(["gh", "release", "upload", tag, "-R", repo, "--clobber", *map(str, assets)], check=True)
    if new:
        subprocess.run(["gh", "release", "edit", tag, "-R", repo, "--draft=false", "--latest"], check=True)
    r = subprocess.run(["gh", "workflow", "run", "pages.yml", "-R", repo, "--ref", "main"],
                       capture_output=True, text=True)
    print("Pages deployment started" if r.returncode == 0 else f"Pages workflow not started: {r.stderr.strip()}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Package (and publish) a GitHub data release.")
    ap.add_argument("--upload", action="store_true", help="publish the files as a GitHub release, redeploy the site")
    ap.add_argument("--repo", help="GitHub repository OWNER/NAME (default: the 'origin' remote)")
    ap.add_argument("--notes", type=Path, help="release notes (Markdown) for a new release")
    args = ap.parse_args()

    version = settings()["project"]["version"]
    tag = f"v{version}"
    meta = json.loads((PLATFORM_DATA / "meta.json").read_text(encoding="utf-8"))
    if meta["version"] != version:
        sys.exit(f"platform/data is version {meta['version']} but settings say {version}: run step 11 first")
    repo = github_repo(args.repo)
    rel = path("data", "release", tag, mkdir=True)

    files = []
    for name, (key, src_name), title, desc, fmt in PRODUCTS:
        src, dst = path(key, src_name), rel / name
        if name.endswith(".zip"):
            zip_file(src, dst)
        else:
            link_or_copy(src, dst)
        files.append({"file": name, "title": title, "desc": desc, "format": fmt, "bytes": dst.stat().st_size})
    link_or_copy(PROJECT_ROOT / "docs" / "data_dictionary.csv", rel / "data_dictionary.csv")
    link_or_copy(PLATFORM_DATA / "zcta.pmtiles", rel / "zcta.pmtiles")

    base = f"https://github.com/{repo}"
    downloads = {"release": tag, "repo_url": base, "release_url": f"{base}/releases/tag/{tag}",
                 "base_url": f"{base}/releases/download/{tag}/", "files": files}
    (PLATFORM_DATA / "downloads.json").write_text(json.dumps(downloads, indent=1, ensure_ascii=False),
                                                  encoding="utf-8")

    members = ["meta.json", "zips.json", "downloads.json", "downloads/data_dictionary.csv"]
    members += sorted(p.relative_to(PLATFORM_DATA).as_posix() for p in (PLATFORM_DATA / "m").glob("*.bin"))
    members += sorted(p.relative_to(PLATFORM_DATA).as_posix() for p in (PLATFORM_DATA / "rp").glob("*.json"))
    tmp = rel / "platform-data.zip.part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for m in members:
            z.write(PLATFORM_DATA / m, m)
    tmp.replace(rel / "platform-data.zip")

    names = ["platform-data.zip", "zcta.pmtiles", *(f["file"] for f in files), "data_dictionary.csv"]
    (rel / "SHA256SUMS.txt").write_text("".join(f"{sha256(rel / n)}  {n}\n" for n in names), encoding="utf-8")
    for n in names:
        print(f"{(rel / n).stat().st_size / 1e6:9.1f} MB  {n}")
    print(f"release {tag} for {repo}: {rel}")

    if args.upload:
        notes = args.notes or PROJECT_ROOT / "docs" / "releases" / f"{tag}.md"
        publish(repo, tag, [rel / n for n in names] + [rel / "SHA256SUMS.txt"], notes if notes.exists() else None)


if __name__ == "__main__":
    main()
