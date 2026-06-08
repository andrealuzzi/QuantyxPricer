# QuantyxPricer

A small Python pricing prototype using QuantLib, including a Hull-White model script and market curve input data.

## Project Files

- `hullwhite.py`: main script
- `eur_swap_curve.json`: input market curve data

## Requirements

- Python 3.14+
- QuantLib Python package

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install QuantLib
```

## Run

```bash
python hullwhite.py
```

## Notes

If you run the script outside the virtual environment, make sure `QuantLib` is installed for that interpreter.
