# QuantyxPricer

QuantyxPricer is a multi-model bond pricing workspace built on QuantLib.

It supports:
- Hull-White callable/fixed/CMS pricing
- SPIRE decomposition pricing
- Index-linked channel note pricing
- Trinomial tree callable pricing
- Monte Carlo Hull-White pricing
- Unified dispatch from a single root launcher

## Repository Structure

- `assets/`: bond JSON inputs (one file per ISIN)
- `curves/swap_curves.json`: named market curves and vol surfaces
- `models/hullwhite.py`: Hull-White pricer
- `models/spire.py`: SPIRE decomposition pricer
- `models/index_linked.py`: index-linked pricer
- `models/trinomialtree.py`: callable tree pricer
- `models/montecarlo.py`: Monte Carlo pricer
- `models/pdf_report.py`: shared PDF report generator
- `pricer.py`: root dispatcher routing by `model` field in each bond JSON
- `output/`: generated PDF reports (`ISIN.pdf`)

## Requirements

- Python 3.10+
- QuantLib Python package

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install QuantLib numpy
```

## Quick Start

Run one bond by ISIN through the root dispatcher:

```bash
source .venv/bin/activate
python pricer.py --bond XS3328006716
```

Run all bonds in `assets/`:

```bash
python pricer.py --bond all
```

## Root Dispatcher (`pricer.py`)

Routes each bond based on `"model"` in JSON (`hullwhite`, `spire`, `index_linked`, `trinomialtree`, `montecarlo`).

### Parameters

- `--bond`: bond filename/path, ISIN, or `all`
- `--bond-file`: explicit bond file path (legacy selector)
- `--curve-file`: curve file path (default `curves/swap_curves.json`)
- `--all-bonds`: run every JSON in `assets/`
- `--issuer-spread-bp`: optional override for tree/montecarlo
- `--tree-steps`: optional trinomial tree override
- `--time-steps`: optional Monte Carlo steps override
- `--num-paths`: optional Monte Carlo paths override
- `--seed`: optional Monte Carlo random seed override

### Examples

```bash
python pricer.py --bond FR0013398757
python pricer.py --bond XS3328006716.json
python pricer.py --bond all --curve-file curves/swap_curves.json
```

## Model Scripts

Each model can also run standalone from `models/`.

### Hull-White (`models/hullwhite.py`)

```bash
cd models
source ../.venv/bin/activate
python hullwhite.py --bond-file XS3016984372.json --curve-file ../curves/swap_curves.json
```

Parameters:
- `--all-bonds`
- `--bond-file`
- `--curve-file`

### SPIRE (`models/spire.py`)

```bash
cd models
source ../.venv/bin/activate
python spire.py --bond-file XS3328006716.json --curve-file ../curves/swap_curves.json
```

Parameters:
- `--bond-file`
- `--curve-file`

### Index-linked (`models/index_linked.py`)

```bash
cd models
source ../.venv/bin/activate
python index_linked.py --bond-file XS0316010023.json --curve-file ../curves/swap_curves.json
```

Parameters:
- `--bond-file`
- `--curve-file`

### Trinomial Tree (`models/trinomialtree.py`)

```bash
cd models
source ../.venv/bin/activate
python trinomialtree.py --bond-file XS2148370211.json --curve-file ../curves/swap_curves.json --tree-steps 160
```

Parameters:
- `--all-bonds`
- `--bond-file`
- `--curve-file`
- `--issuer-spread-bp`
- `--tree-steps`

### Monte Carlo (`models/montecarlo.py`)

```bash
cd models
source ../.venv/bin/activate
python montecarlo.py --bond-file XS2148370211.json --curve-file ../curves/swap_curves.json --num-paths 10000 --time-steps 480
```

Parameters:
- `--all-bonds`
- `--bond-file`
- `--curve-file`
- `--issuer-spread-bp`
- `--time-steps`
- `--num-paths`
- `--seed`

## Input Conventions

- Bond filenames in `assets/` must match ISIN/instrument ID (`ISIN.json`).
- Include explicit `"currency"` in each bond JSON.
- Use `"model"` in each bond JSON to control dispatcher routing.
- Curves are selected from `curves/swap_curves.json` by currency and optional curve-name fields.

## Reports

- A PDF report is generated for each run in `output/`.
- File naming is `ISIN.pdf`.
- PDF layout uses summary sections and tables with page margins.

## Notes

- Evaluation date is set to today at runtime by model loaders.
- If a run fails for one bond in batch mode, the batch continues and prints the skip reason.
