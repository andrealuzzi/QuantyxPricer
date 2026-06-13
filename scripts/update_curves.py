#!/usr/bin/env python3

"""Convenience wrapper to update swap curves from ECB and Fed data."""

import argparse
from pathlib import Path

from update_swap_curves_ecb import update_swap_curves
from update_swap_curves_fed import update_swap_curves_fed


def parse_args():
    parser = argparse.ArgumentParser(
        description="Update curves/swap_curves.json using ECB SDW data."
    )
    parser.add_argument(
        "--curve-file",
        default=None,
        help="Optional path to swap_curves.json (defaults to curves/swap_curves.json).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output.",
    )
    return parser.parse_args()


def main():
    
    args = parse_args()
    curve_file = Path(args.curve_file) if args.curve_file else None
    # Update ECB curves first
    try:
        update_swap_curves(curve_file, verbose=not args.quiet)
    except Exception as e:
        print(f"Error running ECB updater: {e}")

    # Then update USD OIS from Fed (SOFR)
    try:
        update_swap_curves_fed(curve_file, verbose=not args.quiet)
    except Exception as e:
        print(f"Error running Fed updater: {e}")


if __name__ == "__main__":
    main()
