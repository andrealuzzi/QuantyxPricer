#!/usr/bin/env python3
"""
Fetch latest ECB SDW observations and update swap_curves.json.

For each curve with an ecb_name or ebc_name field, this script:
- Calls the ECB SDW REST API to fetch the latest observation
- Updates the as_of date and relevant rate(s) in swap_curves.json
- Handles multiple data structures (pillars, spot, etc.)
"""

import json
import requests
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET


def fetch_latest_ecb_observation(series_id):
    """
    Fetch the latest observation for a given ECB SDW series ID.
    Returns (date_str, value) or (None, None) on failure.
    """
    url = f"https://data-api.ecb.europa.eu/service/data/{series_id}"
    headers = {
        "Accept": "application/vnd.sdmx.structurespecificdata+xml;version=2.1"
    }
    
    try:
        response = requests.get(url, headers=headers, verify=False, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"  Error fetching {series_id}: {e}")
        return None, None
    
    # Parse XML response
    try:
        root = ET.fromstring(response.content)
        # ECB SDW XML: observations are in non-namespaced Obs elements inside Series
        # Find Series elements (also non-namespaced)
        observations = []
        for series_elem in root.iter("Series"):
            for obs_elem in series_elem.iter("Obs"):
                observations.append(obs_elem)
        
        if not observations:
            print(f"  No observations found for {series_id}")
            return None, None
        
        # Get the last (latest) observation
        last_obs = observations[-1]
        
        # Extract time period and observation value (both are attributes)
        time_period = last_obs.get("TIME_PERIOD")
        obs_value = last_obs.get("OBS_VALUE")
        
        if not time_period or not obs_value:
            print(f"  Could not extract time_period or obs_value from {series_id}")
            return None, None
        
        # Convert time period to date format (e.g., "2026-06" -> "2026-06-01", "2026-06-13" stays)
        if len(time_period) == 7:  # Monthly format YYYY-MM
            date_str = time_period + "-01"
        else:
            date_str = time_period  # Assume YYYY-MM-DD
        
        return date_str, float(obs_value)
    
    except Exception as e:
        print(f"  Error parsing XML for {series_id}: {e}")
        return None, None


def update_swap_curves(swap_curves_path=None, verbose=True):
    """
    Read swap_curves.json, update rates for curves with ecb_name fields,
    and write back.
    
    Args:
        swap_curves_path: Path to swap_curves.json. If None, uses default location.
        verbose: If True, print progress messages.
    """
    if swap_curves_path is None:
        script_dir = Path(__file__).parent
        project_root = script_dir.parent
        swap_curves_path = project_root / "curves" / "swap_curves.json"
    else:
        swap_curves_path = Path(swap_curves_path)
    
    if verbose:
        print(f"\n[ECB] Reading {swap_curves_path}...")
    
    with open(swap_curves_path, "r") as f:
        curves = json.load(f)
    
    updated_count = 0
    
    for curve in curves:
        # Check for ecb_name or ebc_name (typo in original)
        ecb_name = curve.get("ecb_name") or curve.get("ebc_name")
        
        if not ecb_name:
            continue
        
        if verbose:
            print(f"[ECB] Fetching {curve['curve_name']} ({ecb_name})...")
        date_str, value = fetch_latest_ecb_observation(ecb_name)
        
        if date_str is None or value is None:
            if verbose:
                print(f"[ECB]   Skipped (no data)")
            continue
        
        # Update as_of
        curve["as_of"] = date_str
        
        # Update rate depending on curve structure
        if "pillars" in curve:
            # For curves with pillars (OIS, forward curves), update the first pillar
            # (Assumption: first pillar represents the base rate for the curve)
            if curve["pillars"]:
                curve["pillars"][0]["rate"] = value
                curve["pillars"][0]["source"] = f"ECB SDW {ecb_name} ({date_str})"
                if verbose:
                    print(f"[ECB]   ✓ Updated first pillar to {value:.4f} (as of {date_str})")
                updated_count += 1
        
        elif "spot" in curve:
            # For FX curves, update the spot rate
            curve["spot"]["rate"] = value
            curve["spot"]["source"] = f"ECB SDW {ecb_name} ({date_str})"
            if verbose:
                print(f"[ECB]   ✓ Updated spot to {value:.4f} (as of {date_str})")
            updated_count += 1
        
        else:
            if verbose:
                print(f"[ECB]   Skipped (unknown structure)")
    
    # Write back to file
    with open(swap_curves_path, "w") as f:
        json.dump(curves, f, indent=2)
    
    if verbose:
        print(f"[ECB] ✓ Updated {updated_count} curve(s). Written to {swap_curves_path}")


if __name__ == "__main__":
    # Get the project root and path to swap_curves.json
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    swap_curves_path = project_root / "curves" / "swap_curves.json"
    
    update_swap_curves(swap_curves_path, verbose=True)
