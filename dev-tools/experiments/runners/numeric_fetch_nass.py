"""Fetch county-level crop yields from the USDA NASS Quick Stats API.

The numeric-verifier proof of concept needs a public evaluation whose
shape resembles the model performance report it is modelled on: many
units, four crops of very uneven size, and a yield figure per unit.
USDA county yields are the closest public analogue, with a county
standing in for a field.

The API needs a free key. It is read from ``NASS_API_KEY`` in the
environment, or from an ``.env`` file passed with ``--env-file``. The
key is a credential: keep it outside the repository.

Results are cached per request, so re-running costs nothing and the
staged evaluation is reproducible.

Usage::

    python dev-tools/experiments/runners/numeric_fetch_nass.py
        --env-file  ~/marcus-experiments/numeric-verifier/.env
        --out       ~/marcus-experiments/numeric-verifier/data/nass.parquet
        --year-from 2005 --year-to 2024
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

API_URL = "https://quickstats.nass.usda.gov/api/api_GET/"

# Quick Stats names each series by commodity and a short description.
# Cotton is reported in pounds per acre while the grains are reported
# in bushels per acre, which mirrors the mixed units of the report
# this experiment is modelled on.
SERIES = {
    "maize": {
        "commodity_desc": "CORN",
        "short_desc": "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE",
        "area_desc": "CORN, GRAIN - ACRES HARVESTED",
    },
    "soybeans": {
        "commodity_desc": "SOYBEANS",
        "short_desc": "SOYBEANS - YIELD, MEASURED IN BU / ACRE",
        "area_desc": "SOYBEANS - ACRES HARVESTED",
    },
    "wheat": {
        "commodity_desc": "WHEAT",
        # Plain WHEAT stops being reported by county after 2007;
        # winter wheat is the series that runs to the present.
        "short_desc": "WHEAT, WINTER - YIELD, MEASURED IN BU / ACRE",
        "area_desc": "WHEAT, WINTER - ACRES HARVESTED",
    },
    "cotton": {
        "commodity_desc": "COTTON",
        "short_desc": "COTTON, UPLAND - YIELD, MEASURED IN LB / ACRE",
        "area_desc": "COTTON, UPLAND - ACRES HARVESTED",
    },
}


def load_key(env_file: Optional[str]) -> str:
    """Read the API key from the environment or an env file.

    Parameters
    ----------
    env_file : Optional[str]
        Path to a file holding ``NASS_API_KEY=...``.

    Returns
    -------
    str
        The key.

    Raises
    ------
    SystemExit
        When no key can be found.
    """
    key = os.environ.get("NASS_API_KEY", "").strip()
    if not key and env_file:
        path = Path(env_file).expanduser()
        if path.exists():
            for line in path.read_text().splitlines():
                if line.startswith("NASS_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    if not key:
        raise SystemExit("no NASS_API_KEY in the environment or the given env file")
    return key


def _request(
    params: Dict[str, str], cache_dir: Path, pause: float = 0.4
) -> List[Dict[str, Any]]:
    """Call Quick Stats once, caching the response on disk.

    Parameters
    ----------
    params : Dict[str, str]
        Query parameters, including the key.
    cache_dir : Path
        Directory for cached responses.
    pause : float
        Seconds to wait after a live call, to stay a polite client.

    Returns
    -------
    List[Dict[str, Any]]
        The ``data`` rows, empty when the series has none.
    """
    cache_key = "_".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "key")
    safe = "".join(c if c.isalnum() or c in "=_-" else "." for c in cache_key)
    cached = cache_dir / f"{safe[:180]}.json"
    if cached.exists():
        return list(json.loads(cached.read_text()).get("data", []))

    url = API_URL + "?" + urllib.parse.urlencode(params)
    # The URL is built from a module constant, but open it only after
    # checking the scheme so a future edit cannot turn this into a
    # file:// or custom-scheme read.
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"refusing a non-https request: {url[:60]}")
    with urllib.request.urlopen(url, timeout=120) as resp:  # nosec B310
        payload = json.loads(resp.read().decode())
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(payload))
    time.sleep(pause)
    return list(payload.get("data", []))


def fetch_series(
    key: str,
    crop: str,
    short_desc: str,
    year: int,
    cache_dir: Path,
) -> pd.DataFrame:
    """Fetch one crop's county series for one year.

    Parameters
    ----------
    key : str
        API key.
    crop : str
        Short crop label used downstream.
    short_desc : str
        Quick Stats series description.
    year : int
        Year to fetch.
    cache_dir : Path
        Directory for cached responses.

    Returns
    -------
    pd.DataFrame
        Columns ``area``, ``crop``, ``year``, ``value``, ``unit``.
    """
    params = {
        "key": key,
        "short_desc": short_desc,
        "agg_level_desc": "COUNTY",
        "year": str(year),
        "format": "JSON",
    }
    try:
        rows = _request(params, cache_dir)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        print(f"  {crop} {year}: {type(exc).__name__}", file=sys.stderr)
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame = frame[
        frame["Value"]
        .astype(str)
        .str.replace(",", "")
        .str.replace(".", "", 1)
        .str.isdigit()
    ]
    if frame.empty:
        return pd.DataFrame()
    return pd.DataFrame(
        {
            "area": (
                frame["state_alpha"].astype(str)
                + "-"
                + frame["county_name"].astype(str)
            ),
            "crop": crop,
            "year": int(year),
            "value": frame["Value"].astype(str).str.replace(",", "").astype(float),
            "unit": frame["unit_desc"].astype(str),
        }
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the fetcher's argument parser.

    Returns
    -------
    argparse.ArgumentParser
        Configured parser.
    """
    p = argparse.ArgumentParser(prog="numeric_fetch_nass")
    p.add_argument("--env-file", default=None)
    p.add_argument("--out", required=True)
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--year-from", type=int, default=2005)
    p.add_argument("--year-to", type=int, default=2024)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Fetch every crop and year, then write one parquet extract.

    Parameters
    ----------
    argv : Optional[List[str]]
        Command-line arguments; ``None`` reads ``sys.argv``.

    Returns
    -------
    int
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    key = load_key(args.env_file)
    out = Path(args.out).expanduser()
    cache_dir = Path(args.cache_dir or (out.parent / "nass-cache")).expanduser()

    frames: List[pd.DataFrame] = []
    for crop, spec in SERIES.items():
        for year in range(args.year_from, args.year_to + 1):
            got = fetch_series(key, crop, spec["short_desc"], year, cache_dir)
            if not got.empty:
                got["measure"] = "yield_per_acre"
                frames.append(got)
            area = fetch_series(key, crop, spec["area_desc"], year, cache_dir)
            if not area.empty:
                area["measure"] = "acres_harvested"
                frames.append(area)
        total = sum(len(f) for f in frames)
        print(f"{crop}: running total {total} rows")

    if not frames:
        raise SystemExit("no rows returned; check the key and the series")

    combined = pd.concat(frames, ignore_index=True)
    wide = combined.pivot_table(
        index=["area", "crop", "year"],
        columns="measure",
        values="value",
        aggfunc="first",
    ).reset_index()
    units = (
        combined[combined["measure"] == "yield_per_acre"]
        .groupby("crop")["unit"]
        .first()
        .to_dict()
    )
    wide["unit"] = wide["crop"].map(units)
    # The location column is called ``area``; the measures are named
    # so they cannot collide with it when the long form is pivoted.
    wide = wide.dropna(subset=["yield_per_acre", "acres_harvested"])
    out.parent.mkdir(parents=True, exist_ok=True)
    wide.to_parquet(out)
    print(f"\nwrote {len(wide)} county-crop-year rows -> {out}")
    print(wide.groupby("crop").size().to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
