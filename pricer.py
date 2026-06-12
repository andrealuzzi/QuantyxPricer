import argparse
from pathlib import Path

from models import hullwhite, index_linked, montecarlo, spire, trinomialtree,cln
try:
    from reporting import pdf_report, json_report
except ModuleNotFoundError:
    import reporting.pdf_report as pdf_report
    import reporting.json_report as json_report


PROJECT_ROOT = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_ROOT / 'assets'
CURVES_DIR = PROJECT_ROOT / 'curves'
DEFAULT_BOND_FILE = ASSETS_DIR / 'XS1693822634.json'
DEFAULT_CURVE_FILE = CURVES_DIR / 'swap_curves.json'


def parse_args():
    parser = argparse.ArgumentParser(description='Root pricer dispatcher based on bond JSON model field.')
    parser.add_argument('--bond', default=None, help='Bond filename/path, or use `all` to price every bond in assets/')
    parser.add_argument('--bond-file', default=str(DEFAULT_BOND_FILE), help='Bond JSON file path or filename in assets/')
    parser.add_argument('--curve-file', default=str(DEFAULT_CURVE_FILE), help='Curve JSON file path or filename in curves/')
    parser.add_argument('--all-bonds', action='store_true', help='Price all bond JSON files in assets/')
    parser.add_argument('--issuer-spread-bp', type=float, default=None, help='Optional override for tree/montecarlo issuer spread')
    parser.add_argument('--tree-steps', type=int, default=None, help='Optional override for trinomial tree steps')
    parser.add_argument('--time-steps', type=int, default=None, help='Optional override for montecarlo time steps')
    parser.add_argument('--num-paths', type=int, default=None, help='Optional override for montecarlo number of paths')
    parser.add_argument('--seed', type=int, default=None, help='Optional override for montecarlo random seed')
    return parser.parse_args()


def resolve_asset_path(path_like: str):
    path = Path(path_like)
    if path.is_absolute() and path.exists():
        return path

    # Allow selecting by bare ISIN, e.g. --bond XS1693822634
    if path.suffix == '':
        path = Path(f'{path_like}.json')

    candidates = [
        path,
        PROJECT_ROOT / path,
        ASSETS_DIR / path,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return path


def resolve_curve_path(path_like: str):
    path = Path(path_like)
    if path.is_absolute() and path.exists():
        return path
    candidates = [
        path,
        PROJECT_ROOT / path,
        CURVES_DIR / path,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return path


def expected_isin_filename(bond_data):
    instrument_id = str(bond_data.get('instrument_id', '')).strip()
    if instrument_id:
        return f'{instrument_id}.json'

    isin = str(bond_data.get('isin', '')).strip()
    if isin:
        return f'{isin}.json'

    return None


def validate_asset_filenames_by_isin():
    mismatches = []
    for bond_file in sorted(ASSETS_DIR.glob('*.json')):
        if bond_file.name.startswith('.'):
            continue
        try:
            bond_data = hullwhite.load_json(bond_file)
        except Exception as exc:
            print(f"Skipping {bond_file.name}: could not load JSON ({exc})")
            continue
        expected_name = expected_isin_filename(bond_data)
        if expected_name and bond_file.name != expected_name:
            mismatches.append((bond_file.name, expected_name))

    if mismatches:
        details = ', '.join([f'{actual} -> {expected}' for actual, expected in mismatches])
        raise ValueError(
            'Bond files in assets/ must be named with the ISIN (or instrument_id) as filename. '
            f'Mismatches: {details}'
        )


def apply_mc_overrides(bond_data, args):
    data = dict(bond_data)
    if args.time_steps is not None:
        data['mc_time_steps'] = args.time_steps
    if args.num_paths is not None:
        data['mc_num_paths'] = args.num_paths
    if args.seed is not None:
        data['mc_seed'] = args.seed
    return data


def dispatch_one(bond_file: Path, curve_json, args):
    bond_data = hullwhite.load_json(bond_file)

    expected_name = expected_isin_filename(bond_data)
    if expected_name and bond_file.name != expected_name:
        raise ValueError(
            f'Bond file name must match ISIN/instrument_id. Got {bond_file.name}, expected {expected_name}.'
        )

    model_name = str(bond_data.get('model', '')).strip().lower()
    if not model_name:
        raise ValueError(f'Missing model field in {bond_file.name}. Add model in bond JSON.')

    # allow alias 'bond' for the hullwhite pricer
    effective_model = model_name
    if model_name == 'bond':
        effective_model = 'hullwhite'

    if effective_model == 'hullwhite':
        evaluation_date = hullwhite.parse_date(bond_data['evaluation_date'])
        discount_curve_cfg = hullwhite.select_discount_curve_config(curve_json, bond_data)
        curve = hullwhite.build_discount_curve(discount_curve_cfg, evaluation_date)
        result = hullwhite.price_bond(
            curve,
            bond_data,
            curve_json=curve_json,
            discount_curve_name=discount_curve_cfg.get('curve_name'),
        )
        hullwhite.print_bond_result(bond_data, result, curve, curve_json=curve_json)
        pdf_path = pdf_report.create_pdf_report(
            model_name='hullwhite',
            instrument_id=bond_data.get('instrument_id', 'unknown'),
            input_payload=bond_data,
            output_payload=result,
        )
        print(f'PDF report: {pdf_path}')
        print()
        return {
            'bond_file': bond_file.name,
            'instrument_id': bond_data.get('instrument_id'),
            'model': model_name,
            'currency': bond_data.get('currency'),
            'pdf': str(pdf_path),
            'result': result,
        }

    if model_name == 'cln':
        # Reduced-form credit-linked note pricer
        evaluation_date = hullwhite.parse_date(bond_data['evaluation_date'])
        discount_curve_cfg = hullwhite.select_discount_curve_config(curve_json, bond_data)
        curve = hullwhite.build_discount_curve(discount_curve_cfg, evaluation_date)
        result = cln.price_cln(curve, bond_data, curve_json=curve_json)
        cln.print_cln_result(bond_data, result)
        pdf_path = pdf_report.create_pdf_report(
            model_name='cln',
            instrument_id=bond_data.get('instrument_id', 'unknown'),
            input_payload=bond_data,
            output_payload=result,
        )
        print(f'PDF report: {pdf_path}')
        print()
        return {
            'bond_file': bond_file.name,
            'instrument_id': bond_data.get('instrument_id'),
            'model': model_name,
            'currency': bond_data.get('currency'),
            'pdf': str(pdf_path),
            'result': result,
        }

    if model_name == 'spire':
        result = spire.price_spire_note(bond_data, curve_json)
        spire.print_report(bond_data, result)
        pdf_path = pdf_report.create_pdf_report(
            model_name='spire',
            instrument_id=bond_data.get('instrument_id', 'unknown'),
            input_payload=bond_data,
            output_payload=result,
        )
        print(f'PDF report: {pdf_path}')
        print()
        return {
            'bond_file': bond_file.name,
            'instrument_id': bond_data.get('instrument_id'),
            'model': model_name,
            'currency': bond_data.get('currency'),
            'pdf': str(pdf_path),
            'result': result,
        }

    if model_name == 'index_linked':
        result = index_linked.price_index_linked_note(bond_data, curve_json)
        index_linked.print_report(bond_data, result)
        pdf_path = pdf_report.create_pdf_report(
            model_name='index_linked',
            instrument_id=bond_data.get('instrument_id', 'unknown'),
            input_payload=bond_data,
            output_payload=result,
        )
        print(f'PDF report: {pdf_path}')
        print()
        return {
            'bond_file': bond_file.name,
            'instrument_id': bond_data.get('instrument_id'),
            'model': model_name,
            'currency': bond_data.get('currency'),
            'pdf': str(pdf_path),
            'result': result,
        }

    if model_name == 'trinomialtree':
        data = dict(bond_data)
        if args.tree_steps is not None:
            data['tree_time_steps'] = args.tree_steps
        result = trinomialtree.price_callable_bond_tree(curve_json, data, issuer_spread_bp=args.issuer_spread_bp)
        trinomialtree.print_tree_result(data, result)
        pdf_path = pdf_report.create_pdf_report(
            model_name='trinomialtree',
            instrument_id=data.get('instrument_id', 'unknown'),
            input_payload=data,
            output_payload=result,
        )
        print(f'PDF report: {pdf_path}')
        print()
        return {
            'bond_file': bond_file.name,
            'instrument_id': data.get('instrument_id'),
            'model': model_name,
            'currency': data.get('currency'),
            'pdf': str(pdf_path),
            'result': result,
        }

    if model_name == 'montecarlo':
        data = apply_mc_overrides(bond_data, args)
        result = montecarlo.price_bond_monte_carlo(curve_json, data, issuer_spread_bp=args.issuer_spread_bp)
        montecarlo.print_mc_result(data, result)
        pdf_path = pdf_report.create_pdf_report(
            model_name='montecarlo',
            instrument_id=data.get('instrument_id', 'unknown'),
            input_payload=data,
            output_payload=result,
        )
        print(f'PDF report: {pdf_path}')
        print()
        return {
            'bond_file': bond_file.name,
            'instrument_id': data.get('instrument_id'),
            'model': model_name,
            'currency': data.get('currency'),
            'pdf': str(pdf_path),
            'result': result,
        }

    raise ValueError(
        f'Unsupported model="{model_name}" in {bond_file.name}. '
        'Supported values: hullwhite, spire, index_linked, trinomialtree, montecarlo.'
    )


def run_all_bonds(curve_json, args):
    collected = []
    for bond_file in sorted(ASSETS_DIR.glob('*.json')):
        if bond_file.name.startswith('.'):
            continue
        try:
            result_entry = dispatch_one(bond_file, curve_json, args)
            if result_entry is not None:
                collected.append(result_entry)
        except Exception as exc:
            print(f'{bond_file.name}')
            print(f'Skipped: {exc}')
            print()

    if collected:
        out_path = json_report.create_json_report(collected)
        print(f'JSON summary: {out_path}')


def main():
    args = parse_args()
    validate_asset_filenames_by_isin()
    curve_file = resolve_curve_path(args.curve_file)
    curve_json = hullwhite.load_json(curve_file)

    bond_selector = args.bond if args.bond is not None else args.bond_file
    run_all_requested = args.all_bonds or str(bond_selector).strip().lower() == 'all'

    if run_all_requested:
        run_all_bonds(curve_json, args)
        return

    bond_file = resolve_asset_path(str(bond_selector))
    dispatch_one(bond_file, curve_json, args)


if __name__ == '__main__':
    main()
