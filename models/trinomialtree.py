import argparse
from pathlib import Path

import QuantLib as ql

try:
    from models.hullwhite import (
        ASSETS_DIR,
        BOND_FILE,
        CURVE_FILE,
        build_discount_curve,
        get_bond_files,
        get_business_day_convention,
        get_calendar,
        get_coupon_rate,
        get_day_count,
        get_frequency,
        load_json,
        parse_date,
        select_discount_curve_config,
    )
except ModuleNotFoundError:
    from hullwhite import (
        ASSETS_DIR,
        BOND_FILE,
        CURVE_FILE,
        build_discount_curve,
        get_bond_files,
        get_business_day_convention,
        get_calendar,
        get_coupon_rate,
        get_day_count,
        get_frequency,
        load_json,
        parse_date,
        select_discount_curve_config,
    )

BASE_DIR = Path(__file__).resolve().parent


def build_spreaded_curve(base_curve, issuer_spread_bp):
    spread_decimal = issuer_spread_bp / 10000.0
    spread_handle = ql.QuoteHandle(ql.SimpleQuote(spread_decimal))
    base_handle = ql.YieldTermStructureHandle(base_curve)
    spreaded_curve = ql.ZeroSpreadedTermStructure(base_handle, spread_handle)
    return ql.YieldTermStructureHandle(spreaded_curve)


def parse_callability_type(name):
    types = {
        'american': ql.Callability.Call,
        'bermudan': ql.Callability.Call,
        'european': ql.Callability.Call,
    }
    if name not in types:
        raise ValueError(f'Unsupported callable_type: {name}')
    return types[name]


def build_callability_schedule(bond_data):
    call_dates = bond_data.get('call_dates', [])
    if not call_dates:
        raise ValueError('Callable bond JSON must include call_dates')

    call_price = float(bond_data.get('call_price', bond_data.get('par', 100.0)))
    call_type = parse_callability_type(bond_data.get('callable_type', 'bermudan'))
    callability_schedule = ql.CallabilitySchedule()

    for raw_date in call_dates:
        call_date = parse_date(raw_date)
        callability_schedule.append(
            ql.Callability(
                ql.BondPrice(call_price, ql.BondPrice.Clean),
                call_type,
                call_date,
            )
        )

    return callability_schedule


def build_callable_bond(bond_data, projection_curve, eval_date):
    issue_date = parse_date(bond_data['issue_date'])
    if 'maturity_date' in bond_data:
        maturity_date = parse_date(bond_data['maturity_date'])
    elif 'end_date' in bond_data:
        maturity_date = parse_date(bond_data['end_date'])
    else:
        raise ValueError('Callable bond JSON must include maturity_date or end_date')
    calendar = get_calendar(bond_data['calendar'])
    business_day_convention = get_business_day_convention(bond_data['business_day_convention'])
    date_generation_rule = getattr(ql.DateGeneration, bond_data.get('date_generation', 'Forward'))
    structure = bond_data.get('coupon_structure', 'fixed')
    if structure == 'fixed_to_float':
        coupon_frequency_name = bond_data.get('float_coupon_frequency', bond_data.get('coupon_frequency', 'Semiannual'))
    elif structure == 'cms_resettable':
        coupon_frequency_name = bond_data.get('cms_coupon_frequency', bond_data.get('coupon_frequency', 'Semiannual'))
    else:
        coupon_frequency_name = bond_data.get('coupon_frequency', 'Semiannual')
    coupon_frequency = get_frequency(coupon_frequency_name)

    schedule = ql.Schedule(
        issue_date,
        maturity_date,
        ql.Period(coupon_frequency),
        calendar,
        business_day_convention,
        business_day_convention,
        date_generation_rule,
        False,
    )

    settlement_days = int(bond_data.get('settlement_days', 2))
    face_amount = float(bond_data.get('par', 100.0))
    redemption = float(bond_data.get('redemption', face_amount))

    coupons = []
    for i in range(1, len(schedule)):
        d0 = schedule[i - 1]
        d1 = schedule[i]
        coupons.append(float(get_coupon_rate(projection_curve, d0, d1, bond_data, eval_date)))

    if not coupons:
        raise ValueError('Coupon schedule is empty')

    callability_schedule = build_callability_schedule(bond_data)

    bond = ql.CallableFixedRateBond(
        settlement_days,
        face_amount,
        schedule,
        coupons,
        get_day_count(bond_data.get('accrual_day_count', 'Actual365Fixed')),
        business_day_convention,
        redemption,
        issue_date,
        callability_schedule,
    )
    return bond


def price_callable_bond_tree(curve_json, bond_data, issuer_spread_bp=None):
    evaluation_date = parse_date(bond_data['evaluation_date'])
    ql.Settings.instance().evaluationDate = evaluation_date

    discount_curve_cfg = select_discount_curve_config(curve_json, bond_data)
    base_curve = build_discount_curve(discount_curve_cfg, evaluation_date)
    if issuer_spread_bp is None:
        issuer_spread_bp = float(bond_data.get('issuer_spread_bp', bond_data.get('credit_spread_bp', 0.0)))

    spreaded_curve_handle = build_spreaded_curve(base_curve, issuer_spread_bp)
    model_a = float(bond_data.get('hw_a', 0.03))
    model_sigma = float(bond_data.get('hw_sigma', 0.01))
    tree_steps = int(bond_data.get('tree_time_steps', 120))

    model = ql.HullWhite(spreaded_curve_handle, model_a, model_sigma)
    engine = ql.TreeCallableFixedRateBondEngine(model, tree_steps, spreaded_curve_handle)

    bond = build_callable_bond(bond_data, base_curve, evaluation_date)
    bond.setPricingEngine(engine)

    return {
        'npv': bond.NPV(),
        'clean_price': bond.cleanPrice(),
        'dirty_price': bond.dirtyPrice(),
        'accrued_amount': bond.accruedAmount(),
        'issuer_spread_bp': issuer_spread_bp,
        'model_a': model_a,
        'model_sigma': model_sigma,
        'tree_steps': tree_steps,
        'evaluation_date': evaluation_date.ISO(),
        'bond': bond,
    }


def print_tree_result(bond_data, result):
    issue_price = float(bond_data.get('issue_price', 100.0))
    model_clean_price_pct = result['clean_price']
    model_dirty_price_pct = result['dirty_price']

    print(f"{bond_data['description']} ({bond_data['instrument_id']})")
    print(f"Evaluation date: {result['evaluation_date']}")
    print(f"Issuer spread: {result['issuer_spread_bp']:.2f} bp")
    print(f"Hull-White a: {result['model_a']:.4f}")
    print(f"Hull-White sigma: {result['model_sigma']:.4f}")
    print(f"Tree steps: {result['tree_steps']}")
    print(f"Issue price (%): {issue_price:.4f}")
    print(f"Model NPV: {result['npv']:.4f}")
    print(f"Model clean price (%): {model_clean_price_pct:.6f}")
    print(f"Model dirty price (%): {model_dirty_price_pct:.6f}")
    print(f"Accrued amount: {result['accrued_amount']:.4f}")
    print(f"Model - Issue (%): {model_clean_price_pct - issue_price:.6f}")

    if 'market_price' in bond_data:
        market_price = float(bond_data['market_price'])
        diff = model_clean_price_pct - market_price
        print(f"Market price (%): {market_price:.6f}")
        print(f"Model - Market (%): {diff:.6f}")
    print()


def print_tree_skip(bond_data, error):
    print(f"{bond_data.get('description', bond_data['instrument_id'])} ({bond_data['instrument_id']})")
    print(f"Skipped: {error}")
    print()


def run_all_bonds(curve_json, bond_files=None):
    if bond_files is None:
        bond_files = get_bond_files(ASSETS_DIR)

    for bond_file in bond_files:
        bond_data = load_json(bond_file)
        try:
            result = price_callable_bond_tree(curve_json, bond_data)
        except Exception as exc:
            print_tree_skip(bond_data, exc)
            continue
        print_tree_result(bond_data, result)


def parse_args():
    parser = argparse.ArgumentParser(description='Price callable fixed-rate bonds with a Hull-White trinomial tree.')
    parser.add_argument('--all-bonds', action='store_true', help='Price all known bond JSON files in the project folder')
    parser.add_argument('--bond-file', default=str(BOND_FILE), help='Path to bond JSON input file')
    parser.add_argument('--curve-file', default=str(CURVE_FILE), help='Path to swap curve JSON input file (single curve or catalog)')
    parser.add_argument('--issuer-spread-bp', type=float, default=None, help='Override issuer spread in basis points')
    parser.add_argument('--tree-steps', type=int, default=None, help='Override trinomial tree time steps')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    curve_json = load_json(Path(args.curve_file))

    if args.all_bonds:
        for bond_file in get_bond_files(ASSETS_DIR):
            bond_data = load_json(bond_file)
            if args.tree_steps is not None:
                bond_data = dict(bond_data)
                bond_data['tree_time_steps'] = args.tree_steps
            try:
                result = price_callable_bond_tree(curve_json, bond_data, issuer_spread_bp=args.issuer_spread_bp)
            except Exception as exc:
                print_tree_skip(bond_data, exc)
                continue
            print_tree_result(bond_data, result)
        raise SystemExit(0)

    bond_data = load_json(Path(args.bond_file))
    if args.tree_steps is not None:
        bond_data = dict(bond_data)
        bond_data['tree_time_steps'] = args.tree_steps

    result = price_callable_bond_tree(curve_json, bond_data, issuer_spread_bp=args.issuer_spread_bp)
    print_tree_result(bond_data, result)
