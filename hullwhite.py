import argparse
import json
import math
from pathlib import Path

import QuantLib as ql

BASE_DIR = Path(__file__).resolve().parent
CURVE_FILE = BASE_DIR / 'eur_swap_curve.json'
BOND_FILE = BASE_DIR / 'XS1693822634.json'


def load_json(path: Path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        content = f.read().strip()

    if not content:
        raise ValueError(f'JSON file is empty: {path}')

    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f'Invalid JSON in {path}: {exc}') from exc


def parse_date(date_str: str):
    day, month, year = map(int, date_str.split('-'))
    return ql.Date(day, month, year)


def get_calendar(name: str):
    calendars = {
        'TARGET': ql.TARGET,
    }
    if name not in calendars:
        raise ValueError(f'Unsupported calendar: {name}')
    return calendars[name]()


def get_day_count(name: str):
    day_counts = {
        'Actual365Fixed': ql.Actual365Fixed,
        'Actual360': ql.Actual360,
    }
    if name not in day_counts:
        raise ValueError(f'Unsupported day count: {name}')
    return day_counts[name]()


def get_business_day_convention(name: str):
    conventions = {
        'Unadjusted': ql.Unadjusted,
        'ModifiedFollowing': ql.ModifiedFollowing,
    }
    if name not in conventions:
        raise ValueError(f'Unsupported business day convention: {name}')
    return conventions[name]


def get_frequency(name: str):
    frequencies = {
        'Annual': ql.Annual,
        'Semiannual': ql.Semiannual,
        'Quarterly': ql.Quarterly,
        'Monthly': ql.Monthly,
    }
    if name not in frequencies:
        raise ValueError(f'Unsupported coupon frequency: {name}')
    return frequencies[name]


def get_date_generation(name: str):
    rules = {
        'Forward': ql.DateGeneration.Forward,
        'Backward': ql.DateGeneration.Backward,
    }
    if name not in rules:
        raise ValueError(f'Unsupported date generation rule: {name}')
    return rules[name]


def get_reference_day_count(name: str):
    ref_day_counts = {
        'Actual360': ql.Actual360,
        'Actual365Fixed': ql.Actual365Fixed,
    }
    if name not in ref_day_counts:
        raise ValueError(f'Unsupported reference rate day count: {name}')
    return ref_day_counts[name]()


def get_coupon_rate(curve, d0, d1, bond_data, eval_date):
    structure = bond_data.get('coupon_structure', 'fixed')

    if structure == 'fixed':
        return bond_data['fixed_coupon_rate']

    if structure == 'cms_resettable':
        calendar = get_calendar(bond_data['calendar'])
        business_day_convention = get_business_day_convention(bond_data['business_day_convention'])
        cms_tenor_years = int(bond_data.get('cms_tenor_years', 10))
        cms_day_count = get_reference_day_count(
            bond_data.get('cms_day_count', bond_data.get('accrual_day_count', 'Actual360'))
        )
        cms_fixed_leg_frequency = get_frequency(
            bond_data.get('cms_fixed_leg_frequency', 'Annual')
        )

        reset_date = d0 if d0 > eval_date else eval_date
        swap_end = calendar.advance(reset_date, ql.Period(cms_tenor_years, ql.Years), business_day_convention)
        fixed_schedule = ql.Schedule(
            reset_date,
            swap_end,
            ql.Period(cms_fixed_leg_frequency),
            calendar,
            business_day_convention,
            business_day_convention,
            ql.DateGeneration.Forward,
            False,
        )

        annuity = 0.0
        for i in range(1, len(fixed_schedule)):
            f0 = fixed_schedule[i - 1]
            f1 = fixed_schedule[i]
            alpha = cms_day_count.yearFraction(f0, f1)
            annuity += alpha * curve.discount(f1)

        if annuity <= 0.0:
            cms_rate = 0.0
        else:
            cms_rate = (curve.discount(reset_date) - curve.discount(swap_end)) / annuity

        rate = bond_data.get('cms_multiplier', 1.0) * cms_rate + bond_data.get('cms_spread', 0.0)
        if 'cms_floor' in bond_data:
            rate = max(rate, bond_data['cms_floor'])
        if 'cms_cap' in bond_data:
            rate = min(rate, bond_data['cms_cap'])
        return rate

    if structure != 'fixed_to_float':
        raise ValueError(f'Unsupported coupon_structure: {structure}')

    fixed_end_date = parse_date(bond_data['fixed_rate_end_date'])
    if d1 <= fixed_end_date:
        return bond_data['fixed_coupon_rate']

    ref_day_count = get_reference_day_count(
        bond_data.get('float_reference_day_count', 'Actual360')
    )
    spread = bond_data.get('float_spread', 0.0)
    floor_rate = bond_data.get('float_floor')

    d_start = d0 if d0 > eval_date else eval_date
    if d_start >= d1:
        fwd_rate = 0.0
    else:
        yf = ref_day_count.yearFraction(d_start, d1)
        if yf <= 0.0:
            fwd_rate = 0.0
        else:
            df0 = curve.discount(d_start)
            df1 = curve.discount(d1)
            fwd_rate = (df0 / df1 - 1.0) / yf

    rate = fwd_rate + spread
    if floor_rate is not None:
        rate = max(rate, floor_rate)
    return rate


def build_discount_curve(curve_json, evaluation_date):
    calendar = ql.TARGET()
    ql.Settings.instance().evaluationDate = evaluation_date
    day_count = ql.Actual365Fixed()
    settlement_days = 2
    fixed_leg_frequency = ql.Annual
    fixed_leg_convention = ql.Unadjusted
    fixed_leg_daycount = ql.Thirty360(ql.Thirty360.BondBasis)
    float_index = ql.Euribor6M()

    helpers = []
    for p in curve_json['pillars']:
        tenor = p['tenor']
        rate = p['rate']
        years = int(tenor.replace('Y', ''))
        helpers.append(
            ql.SwapRateHelper(
                ql.QuoteHandle(ql.SimpleQuote(rate)),
                ql.Period(years, ql.Years),
                calendar,
                fixed_leg_frequency,
                fixed_leg_convention,
                fixed_leg_daycount,
                float_index,
            )
        )

    curve = ql.PiecewiseLogCubicDiscount(ql.Settings.instance().evaluationDate, helpers, day_count)
    curve.enableExtrapolation()
    return curve


def build_coupon_schedule(bond_data):
    issue_date = parse_date(bond_data['issue_date'])
    if 'maturity_date' in bond_data:
        maturity_date = parse_date(bond_data['maturity_date'])
    elif 'end_date' in bond_data:
        maturity_date = parse_date(bond_data['end_date'])
    else:
        raise ValueError('Bond JSON must include either maturity_date or end_date')
    calendar = get_calendar(bond_data['calendar'])
    business_day_convention = get_business_day_convention(bond_data['business_day_convention'])
    date_generation_rule = get_date_generation(bond_data['date_generation'])

    structure = bond_data.get('coupon_structure', 'fixed')
    if structure == 'fixed_to_float':
        frequency_name = bond_data.get('float_coupon_frequency', bond_data['coupon_frequency'])
    elif structure == 'cms_resettable':
        frequency_name = bond_data.get('cms_coupon_frequency', bond_data['coupon_frequency'])
    else:
        frequency_name = bond_data['coupon_frequency']

    frequency = get_frequency(frequency_name)
    schedule = ql.Schedule(
        issue_date,
        maturity_date,
        ql.Period(frequency),
        calendar,
        business_day_convention,
        business_day_convention,
        date_generation_rule,
        False,
    )
    return schedule, maturity_date


def price_to_call_date(curve, bond_data, call_date, schedule):
    eval_date = ql.Settings.instance().evaluationDate
    day_count = get_day_count(bond_data['accrual_day_count'])
    par = bond_data['par']
    spread_bp = bond_data['credit_spread_bp']
    spread = spread_bp / 10000.0

    pv = 0.0
    cashflows = []
    for i in range(1, len(schedule)):
        d0 = schedule[i - 1]
        d1 = schedule[i]
        if d1 > call_date:
            break

        accrual = day_count.yearFraction(d0, d1)
        coupon_rate = get_coupon_rate(curve, d0, d1, bond_data, eval_date)
        cf = par * coupon_rate * accrual
        t = day_count.yearFraction(eval_date, d1)
        if t < 0:
            continue

        df = curve.discount(d1) * math.exp(-spread * t)
        pv_cf = cf * df
        pv += pv_cf
        cashflows.append((d1.ISO(), coupon_rate, cf, df, pv_cf))

    t_call = day_count.yearFraction(eval_date, call_date)
    df_call = curve.discount(call_date) * math.exp(-spread * t_call)
    redemption = par * df_call
    pv += redemption

    return pv, redemption, cashflows


def price_bond(curve, bond_data):
    schedule, maturity_date = build_coupon_schedule(bond_data)
    eval_date = ql.Settings.instance().evaluationDate
    spread_bp = bond_data['credit_spread_bp']
    raw_call_dates = bond_data.get('call_dates', [])

    if raw_call_dates:
        call_dates = [parse_date(d) for d in raw_call_dates]
    else:
        if 'end_date' in bond_data:
            call_dates = [parse_date(bond_data['end_date'])]
        else:
            call_dates = [maturity_date]

    eligible_call_dates = sorted(
        d for d in call_dates if d >= eval_date and d <= maturity_date
    )
    if not eligible_call_dates:
        eligible_call_dates = [maturity_date]

    scenarios = []
    for call_date in eligible_call_dates:
        npv, redemption, cashflows = price_to_call_date(curve, bond_data, call_date, schedule)
        scenarios.append(
            {
                'call_date': call_date.ISO(),
                'npv': npv,
                'redemption_pv': redemption,
                'cashflows': cashflows,
            }
        )

    maturity_npv, maturity_redemption, maturity_cashflows = price_to_call_date(
        curve, bond_data, maturity_date, schedule
    )
    maturity_scenario = {
        'call_date': maturity_date.ISO(),
        'npv': maturity_npv,
        'redemption_pv': maturity_redemption,
        'cashflows': maturity_cashflows,
    }

    worst = min(scenarios, key=lambda x: x['npv'])
    first = min(scenarios, key=lambda x: x['call_date'])

    # Select which NPV concept to report for callable structures.
    if 'valuation_mode' in bond_data:
        valuation_mode = bond_data['valuation_mode']
    else:
        valuation_mode = 'to_maturity'

    if valuation_mode == 'worst_call':
        selected = worst
    elif valuation_mode == 'first_call':
        selected = first
    elif valuation_mode == 'to_maturity':
        selected = maturity_scenario
    else:
        raise ValueError(f'Unsupported valuation_mode: {valuation_mode}')

    return {
        'selected_npv': selected['npv'],
        'valuation_mode': valuation_mode,
        'selected_call_date': selected['call_date'],
        'redemption_pv': selected['redemption_pv'],
        'spread_bp': spread_bp,
        'cashflows': selected['cashflows'],
        'npv_to_worst_call': worst['npv'],
        'npv_to_first_call': first['npv'],
        'npv_to_maturity': maturity_scenario['npv'],
        'scenarios': scenarios,
    }


def get_model_price(result):
    return result['selected_npv']


def implied_spread_bp(curve, bond_data, market_price, low_bp=-500.0, high_bp=3000.0, tol=1e-6, max_iter=120):
    def price_at(spread_bp):
        trial = dict(bond_data)
        trial['credit_spread_bp'] = spread_bp
        trial_result = price_bond(curve, trial)
        return get_model_price(trial_result)

    low_price = price_at(low_bp)
    high_price = price_at(high_bp)

    # Expand bracket if needed.
    expand_count = 0
    while not (low_price >= market_price >= high_price) and expand_count < 20:
        low_bp -= 500.0
        high_bp += 500.0
        low_price = price_at(low_bp)
        high_price = price_at(high_bp)
        expand_count += 1

    if not (low_price >= market_price >= high_price):
        raise ValueError('Could not bracket implied spread for the provided market_price.')

    for _ in range(max_iter):
        mid_bp = 0.5 * (low_bp + high_bp)
        mid_price = price_at(mid_bp)
        if abs(mid_price - market_price) < tol:
            return mid_bp

        if mid_price > market_price:
            low_bp = mid_bp
        else:
            high_bp = mid_bp

    return 0.5 * (low_bp + high_bp)


def price_with_spread_bp(curve, bond_data, spread_bp):
    trial = dict(bond_data)
    trial['credit_spread_bp'] = spread_bp
    trial_result = price_bond(curve, trial)
    return get_model_price(trial_result)


def print_bond_result(bond_data, result, curve=None):
    print(f"{bond_data['description']} ({bond_data['instrument_id']})")
    print(f"Valuation mode: {result['valuation_mode']}")
    print(f"Selected call date: {result['selected_call_date']}")
    print(f"Selected NPV: {result['selected_npv']:.4f}")
    print(f"NPV (worst call): {result['npv_to_worst_call']:.4f}")
    print(f"NPV (first call): {result['npv_to_first_call']:.4f}")
    print(f"NPV (to maturity): {result['npv_to_maturity']:.4f}")
    print(f"Redemption PV: {result['redemption_pv']:.4f}")
    print(f"Spread: {result['spread_bp']:.1f} bp")

    if 'market_price' in bond_data and curve is not None:
        market_price = bond_data['market_price']
        model_price = get_model_price(result)
        diff = model_price - market_price
        imp_spread = implied_spread_bp(curve, bond_data, market_price)
        fitted_price = price_with_spread_bp(curve, bond_data, imp_spread)
        print(f"Market price: {market_price:.4f}")
        print(f"Model - Market: {diff:.4f}")
        print(f"Implied spread from market price: {imp_spread:.2f} bp")
        print(f"Model NPV at implied spread: {fitted_price:.4f}")
        print(f"Residual at implied spread: {fitted_price - market_price:.6f}")

    if result.get('scenarios'):
        print('Scenarios:')
        for scenario in result['scenarios']:
            print(f"  {scenario['call_date']}: {scenario['npv']:.4f}")
    print()


def get_bond_files(base_dir: Path):
    bond_files = []
    for path in sorted(base_dir.glob('*.json')):
        if path.name == CURVE_FILE.name:
            continue
        if path.name.startswith('.'):
            continue
        bond_files.append(path)
    return bond_files


def run_all_bonds(curve_json, bond_files=None):
    if bond_files is None:
        bond_files = get_bond_files(BASE_DIR)

    results = []
    for bond_file in bond_files:
        bond_data = load_json(bond_file)
        evaluation_date = parse_date(bond_data['evaluation_date'])
        curve = build_discount_curve(curve_json, evaluation_date)
        result = price_bond(curve, bond_data)

        model_price = get_model_price(result)
        market_price = bond_data.get('market_price')
        if market_price is not None:
            diff = model_price - market_price
        else:
            diff = None

        results.append(
            {
                'bond_file': bond_file.name,
                'instrument_id': bond_data['instrument_id'],
                'description': bond_data.get('description', bond_data['instrument_id']),
                'valuation_mode': result['valuation_mode'],
                'selected_call_date': result['selected_call_date'],
                'model_price': model_price,
                'market_price': market_price,
                'model_minus_market': diff,
                'implied_spread_bp': (
                    implied_spread_bp(curve, bond_data, market_price)
                    if market_price is not None
                    else None
                ),
            }
        )

    return results


def parse_args():
    parser = argparse.ArgumentParser(description='Price a bond from JSON terms using a simplified QuantLib setup.')
    parser.add_argument(
        '--all-bonds',
        action='store_true',
        help='Price all known bond JSON files in the project folder',
    )
    parser.add_argument(
        '--bond-file',
        default=str(BOND_FILE),
        help='Path to bond JSON input file',
    )
    parser.add_argument(
        '--curve-file',
        default=str(CURVE_FILE),
        help='Path to EUR swap curve JSON input file',
    )
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    curve_json = load_json(Path(args.curve_file))

    if args.all_bonds:
        batch_results = run_all_bonds(curve_json)
        for item in batch_results:
            bond_data = load_json(BASE_DIR / item['bond_file'])
            evaluation_date = parse_date(bond_data['evaluation_date'])
            curve = build_discount_curve(curve_json, evaluation_date)
            result = price_bond(curve, bond_data)
            print_bond_result(bond_data, result, curve)
        raise SystemExit(0)

    bond_data = load_json(Path(args.bond_file))
    evaluation_date = parse_date(bond_data['evaluation_date'])
    curve = build_discount_curve(curve_json, evaluation_date)
    result = price_bond(curve, bond_data)
    print_bond_result(bond_data, result, curve)
