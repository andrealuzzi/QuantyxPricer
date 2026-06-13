#!/usr/bin/env python3
"""
Fetch latest USD OIS (SOFR) observations from FRED and update swap_curves.json.

This script looks for a curve named `USD_OIS` (or `USD_OIS_PROXY`) in
`curves/swap_curves.json` and updates its first pillar rate (if present)
using the latest SOFR observation from the FRED API. The FRED API key is
read from the environment variable `FRED_API_KEY` or from a project `.env`
file.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import requests


def _read_fred_api_key(project_root: Path):
    key = os.getenv("FRED_API_KEY")
    if key:
        return key

    # Fallback: try to read .env in project root
    env_path = project_root / ".env"
    if not env_path.exists():
        return None

    try:
        with open(env_path, "r") as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                # allow lines like: FRED_API_KEY=xxx or export FRED_API_KEY=xxx
                if "FRED_API_KEY" not in raw:
                    continue
                # remove export prefix if present
                if raw.startswith("export "):
                    raw = raw[len("export "):]
                parts = raw.split("=", 1)
                if len(parts) != 2:
                    continue
                key_name = parts[0].strip()
                val = parts[1].strip()
                # strip quotes
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                if key_name == "FRED_API_KEY":
                    return val
    except Exception:
        return None

    return None


def _load_env(project_root: Path):
    """Load key=value pairs from a .env file into os.environ if not already set."""
    env_path = project_root / ".env"
    if not env_path.exists():
        return

    try:
        with open(env_path, "r") as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                if raw.startswith("export "):
                    raw = raw[len("export "):]
                if "=" not in raw:
                    continue
                k, v = raw.split("=", 1)
                k = k.strip()
                v = v.strip()
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                # Only set if not already in environment
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        return


def fetch_latest_fred_observation(series_id: str, api_key: str):
    """Return (date_str, value) for latest observation or (None, None)."""
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "limit": 1,
        "sort": "desc",
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Error fetching FRED series {series_id}: {e}")
        return None, None

    try:
        data = resp.json()
        observations = data.get("observations", [])
        if not observations:
            print(f"  No observations returned for {series_id}")
            return None, None

        obs = observations[0]
        date_str = obs.get("date")
        value_str = obs.get("value")
        if not date_str or value_str is None or value_str == ".":
            return None, None

        return date_str, float(value_str)
    except Exception as e:
        print(f"  Error parsing FRED response for {series_id}: {e}")
        return None, None


def update_swap_curves_fed(swap_curves_path=None, verbose=True):
    if swap_curves_path is None:
        script_dir = Path(__file__).parent
        project_root = script_dir.parent
        swap_curves_path = project_root / "curves" / "swap_curves.json"
    else:
        swap_curves_path = Path(swap_curves_path)
        project_root = swap_curves_path.parent.parent

    if verbose:
        print(f"\n[FED] Reading {swap_curves_path}...")

    with open(swap_curves_path, "r") as f:
        curves = json.load(f)

    # Load environment variables from .env into os.environ, then read key
    _load_env(project_root)
    api_key = _read_fred_api_key(project_root)
    if not api_key:
        print("FRED API key not found. Set FRED_API_KEY in environment or .env")
        return

    updated_count = 0

    # Iterate curves and fetch any that have a 'fed_name' entry
    for curve in curves:
        series_id = curve.get("fed_name")
        if not series_id:
            continue

        if verbose:
            print(f"[FED] Fetching FRED series {series_id} for {curve.get('curve_name')}...")

        date_str, value = fetch_latest_fred_observation(series_id, api_key)
        if date_str is None or value is None:
            if verbose:
                print(f"[FED]   No valid observation for {series_id}; skipping {curve.get('curve_name')}")
            continue

        if verbose:
            print(f"[FED] Updating {curve.get('curve_name')} with {series_id} ({date_str})...")

        curve["as_of"] = date_str

        if "pillars" in curve and curve["pillars"]:
            curve["pillars"][0]["rate"] = value
            curve["pillars"][0]["source"] = f"FRED {series_id} ({date_str})"
            updated_count += 1
        elif "spot" in curve:
            curve["spot"]["rate"] = value
            curve["spot"]["source"] = f"FRED {series_id} ({date_str})"
            updated_count += 1
        else:
            # fallback: set a top-level 'rate' if present
            if "rate" in curve:
                curve["rate"] = value
                curve["source"] = f"FRED {series_id} ({date_str})"
                updated_count += 1
            else:
                if verbose:
                    print(f"[FED]   Skipped {curve.get('curve_name')} (unknown structure)")

    with open(swap_curves_path, "w") as f:
        json.dump(curves, f, indent=2)

    if verbose:
        print(f"[FED] ✓ Updated {updated_count} curve(s). Written to {swap_curves_path}")


if __name__ == "__main__":
    update_swap_curves_fed(verbose=True)
