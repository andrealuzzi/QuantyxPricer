import argparse
import json
import math
from pathlib import Path

import QuantLib as ql


BASE_DIR = Path(__file__).resolve().parent
CURVE_FILE = BASE_DIR / 'swap_curves.json'
BOND_FILE = BASE_DIR / 'XS2725067362.json'


def load_json(path: Path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        content = f.read().strip()
    if not content:
        raise ValueError(f'JSON file is empty: {path}')
    return json.loads(content)


def parse_date(date_str: str):
    day, month, year = map(int, date_str.split('-'))
    return ql.Date(day, month, year)


def get_calendar(name: str):
    calendars = {
        'TARGET': ql.TARGET,
        'UnitedStates': lambda: ql.UnitedStates(ql.UnitedStates.GovernmentBond),
        'TARGET+UnitedStates': ql.TARGET,
    }
    if name not in calendars:
        raise ValueError(f'Unsupported calendar: {name}')
    return calendars[name]()


def get_business_day_convention(name: str):
    conventions = {
        'Following': ql.Following,
        'ModifiedFollowing': ql.ModifiedFollowing,
        'Unadjusted': ql.Unadjusted,
    }
    if name not in conventions:
        raise ValueError(f'Unsupported business day convention: {name}')
    return conventions[name]


def get_day_count(name: str):
    day_counts = {
        'Actual365Fixed': ql.Actual365Fixed,
        'Actual360': ql.Actual360,
        '30/360': lambda: ql.Thirty360(ql.Thirty360.BondBasis),
        'Thirty360': lambda: ql.Thirty360(ql.Thirty360.BondBasis),
        'ActualActual': lambda: ql.ActualActual(ql.ActualActual.ISDA),
    }
    if name not in day_counts:
        raise ValueError(f'Unsupported day count: {name}')
    return day_counts[name]()


def get_frequency(name: str):
    frequencies = {
        'Annual': ql.Annual,
        'Semiannual': ql.Semiannual,
        'Quarterly': ql.Quarterly,
        'Monthly': ql.Monthly,
    }
    if name not in frequencies:
        raise ValueError(f'Unsupported frequency: {name}')
    return frequencies[name]


def tenor_to_period(tenor: str):
    value = tenor.strip().upper()
    if value == 'ON':
        return ql.Period(1, ql.Days)
    if value.endswith('D'):
        return ql.Period(int(value[:-1]), ql.Days)
    if value.endswith('W'):
        return ql.Period(int(value[:-1]), ql.Weeks)
    if value.endswith('M'):
        return ql.Period(int(value[:-1]), ql.Months)
    if value.endswith('Y'):
        return ql.Period(int(value[:-1]), ql.Years)
    raise ValueError(f'Unsupported tenor: {tenor}')


def normalize_curve_catalog(curve_json):
    if isinstance(curve_json, dict):
        return None
    if not isinstance(curve_json, list):
        raise ValueError('Curve file must be a single curve object or a list of named curves.')

    catalog = {}
    for entry in curve_json:
        if not isinstance(entry, dict):
            continue
        name = entry.get('curve_name')
        if name:
            catalog[name] = entry
    if not catalog:
        raise ValueError('No named curves found in curve catalog JSON.')
    return catalog


def infer_currency_from_isin(isin):
    if not isin:
        return None
    prefix = str(isin).strip().upper()[:2]
    if prefix == 'US':
        return 'USD'
    if prefix in {'XS', 'EU'}:
        return 'EUR'
    return None


def select_curve_from_catalog(curve_json, requested_name=None, default_currency='EUR'):
    catalog = normalize_curve_catalog(curve_json)
    if catalog is None:
        return curve_json

    if requested_name:
        if requested_name not in catalog:
            raise ValueError(f'Requested curve_name not found: {requested_name}')
        return catalog[requested_name]

    default_name = f'{default_currency.upper()}_OIS_PROXY'
    if default_name in catalog:
        return catalog[default_name]

    for name, cfg in catalog.items():
        upper_name = name.upper()
        if upper_name.startswith(f'{default_currency.upper()}_') and 'OIS' in upper_name and 'pillars' in cfg:
            return cfg

    raise ValueError(f'No default OIS curve found for currency={default_currency}.')


def select_note_curve(note_data, curve_json):
    requested = note_data.get('discount_curve_name') or note_data.get('note_discount_curve_name')
    currency = str(note_data.get('currency', 'EUR')).upper()
    cfg = select_curve_from_catalog(curve_json, requested_name=requested, default_currency=currency)
    return cfg, cfg.get('curve_name', 'UNNAMED_CURVE')


def select_collateral_curve(note_data, curve_json):
    collateral = note_data.get('collateral', {})
    requested = collateral.get('discount_curve_name')
    collateral_currency = (
        collateral.get('currency')
        or note_data.get('csa', {}).get('base_currency')
        or infer_currency_from_isin(collateral.get('isin'))
        or note_data.get('currency')
        or 'EUR'
    )
    cfg = select_curve_from_catalog(curve_json, requested_name=requested, default_currency=str(collateral_currency).upper())
    return cfg, cfg.get('curve_name', 'UNNAMED_CURVE')


def build_discount_curve(curve_json, evaluation_date):
    ql.Settings.instance().evaluationDate = evaluation_date

    day_count = get_day_count(curve_json.get('day_count', 'Actual365Fixed'))
    calendar = get_calendar(curve_json.get('calendar', 'TARGET'))

    pillars = curve_json.get('pillars', [])
    if not pillars:
        raise ValueError('Selected curve has no pillars.')

    date_rate_pairs = []
    for pillar in pillars:
        period = tenor_to_period(pillar['tenor'])
        pillar_date = calendar.advance(evaluation_date, period, ql.Following)
        date_rate_pairs.append((pillar_date, float(pillar['rate'])))

    date_rate_pairs.sort(key=lambda x: int(x[0].serialNumber()))
    unique_dates = {}
    for d, r in date_rate_pairs:
        unique_dates[int(d.serialNumber())] = (d, r)

    sorted_pairs = [unique_dates[k] for k in sorted(unique_dates.keys())]
    first_rate = sorted_pairs[0][1]
    dates = [evaluation_date]
    rates = [first_rate]
    for d, r in sorted_pairs:
        if d == evaluation_date:
            rates[0] = r
            continue
        dates.append(d)
        rates.append(r)

    if len(dates) < 2:
        raise ValueError('Insufficient curve pillars to build term structure.')

    curve = ql.ZeroCurve(dates, rates, day_count, calendar)
    curve.enableExtrapolation()
    return curve, day_count


def build_regular_schedule(start_date, end_date, frequency_name, calendar_name, business_day_convention_name):
    frequency = get_frequency(frequency_name)
    calendar = get_calendar(calendar_name)
    convention = get_business_day_convention(business_day_convention_name)
    return ql.Schedule(
        start_date,
        end_date,
        ql.Period(frequency),
        calendar,
        convention,
        convention,
        ql.DateGeneration.Forward,
        False,
    )


def build_note_dates(note_data):
    issue_date = parse_date(note_data['issue_date'])
    maturity_date = parse_date(note_data['maturity_date'])
    if 'first_coupon_date' in note_data:
        dates = [issue_date, parse_date(note_data['first_coupon_date'])]
        current = dates[-1]
        while current < maturity_date:
            next_date = ql.Date(current.dayOfMonth(), current.month(), current.year() + 1)
            if next_date > maturity_date:
                next_date = maturity_date
            dates.append(next_date)
            current = next_date
        return dates

    schedule = build_regular_schedule(
        issue_date,
        maturity_date,
        note_data.get('coupon_frequency', 'Annual'),
        note_data.get('calendar', 'TARGET'),
        note_data.get('business_day_convention', 'Following'),
    )
    return [schedule[i] for i in range(len(schedule))]


def discount_factor_with_issuer_spread(curve, day_count, evaluation_date, target_date, issuer_spread_bp):
    t = day_count.yearFraction(evaluation_date, target_date)
    if t < 0.0:
        return 0.0
    base_df = curve.discount(target_date)
    spread_df = math.exp(-(issuer_spread_bp / 10000.0) * t)
    return base_df * spread_df


def price_note(note_data, curve, curve_day_count):
    eval_date = ql.Settings.instance().evaluationDate
    note_day_count = get_day_count(note_data.get('accrual_day_count', '30/360'))

    notional = float(note_data.get('note_notional', 100000000.0))
    coupon_rate = float(note_data['fixed_coupon_rate'])
    issuer_spread_bp = float(note_data.get('credit_spread_bp', 0.0))

    dates = build_note_dates(note_data)

    pv_coupons = 0.0
    pv_redemption = 0.0
    cashflows = []

    for i in range(1, len(dates)):
        d0 = dates[i - 1]
        d1 = dates[i]
        accrual = note_day_count.yearFraction(d0, d1)
        if d1 <= eval_date:
            continue
        coupon_cf = notional * coupon_rate * accrual
        df = discount_factor_with_issuer_spread(curve, curve_day_count, eval_date, d1, issuer_spread_bp)
        pv = coupon_cf * df
        pv_coupons += pv
        cashflows.append({'date': d1.ISO(), 'type': 'coupon', 'amount': coupon_cf, 'df': df, 'pv': pv})

    maturity_date = dates[-1]
    if maturity_date > eval_date:
        redemption_cf = notional
        df_maturity = discount_factor_with_issuer_spread(
            curve,
            curve_day_count,
            eval_date,
            maturity_date,
            issuer_spread_bp,
        )
        pv_redemption = redemption_cf * df_maturity
        cashflows.append(
            {
                'date': maturity_date.ISO(),
                'type': 'redemption',
                'amount': redemption_cf,
                'df': df_maturity,
                'pv': pv_redemption,
            }
        )

    return {
        'pv_note': pv_coupons + pv_redemption,
        'pv_note_coupons': pv_coupons,
        'pv_note_redemption': pv_redemption,
        'cashflows': cashflows,
    }


def inflation_factor(eval_date, pay_date, inflation_assumption):
    if pay_date <= eval_date:
        return float(inflation_assumption.get('index_ratio_at_eval', 1.0))
    base_ratio = float(inflation_assumption.get('index_ratio_at_eval', 1.0))
    annual_infl = float(inflation_assumption.get('annual_inflation_rate', 0.02))
    yf = ql.Actual365Fixed().yearFraction(eval_date, pay_date)
    return base_ratio * ((1.0 + annual_infl) ** yf)


def model_collateral_pv(collateral_data, curve, curve_day_count):
    eval_date = ql.Settings.instance().evaluationDate
    issue_date = parse_date(collateral_data['issue_date'])
    maturity_date = parse_date(collateral_data['maturity_date'])
    coupon_rate = float(collateral_data['coupon_rate'])
    principal = float(collateral_data['principal_amount'])

    schedule = build_regular_schedule(
        issue_date,
        maturity_date,
        collateral_data.get('coupon_frequency', 'Semiannual'),
        collateral_data.get('calendar', 'TARGET'),
        collateral_data.get('business_day_convention', 'Following'),
    )

    day_count = get_day_count(collateral_data.get('day_count', 'ActualActual'))
    inflation_assumption = collateral_data.get('inflation_assumption', {})

    pv_model = 0.0
    cashflows = []

    for i in range(1, len(schedule)):
        d0 = schedule[i - 1]
        d1 = schedule[i]
        if d1 <= eval_date:
            continue

        accrual = day_count.yearFraction(d0, d1)
        index_ratio = inflation_factor(eval_date, d1, inflation_assumption)
        coupon_cf = principal * coupon_rate * accrual * index_ratio
        df = curve.discount(d1)
        pv_cf = coupon_cf * df
        pv_model += pv_cf
        cashflows.append({'date': d1.ISO(), 'type': 'coupon', 'amount': coupon_cf, 'df': df, 'pv': pv_cf})

    if maturity_date > eval_date:
        index_ratio_mat = inflation_factor(eval_date, maturity_date, inflation_assumption)
        redemption_cf = principal * index_ratio_mat
        df_mat = curve.discount(maturity_date)
        pv_red = redemption_cf * df_mat
        pv_model += pv_red
        cashflows.append({'date': maturity_date.ISO(), 'type': 'redemption', 'amount': redemption_cf, 'df': df_mat, 'pv': pv_red})

    market_dirty = collateral_data.get('market_dirty_price')
    if market_dirty is not None:
        pv_market = principal * float(market_dirty) / 100.0
        valuation_method = 'market_dirty_price'
    else:
        pv_market = pv_model
        valuation_method = 'model_curve_plus_inflation'

    return {
        'pv_collateral': pv_market,
        'pv_collateral_model': pv_model,
        'valuation_method': valuation_method,
        'cashflows': cashflows,
    }


def spread_cost_from_schedule(notional, schedule_dates, eval_date, curve, curve_day_count, spread_bp):
    spread = spread_bp / 10000.0
    pv = 0.0
    for i in range(1, len(schedule_dates)):
        d0 = schedule_dates[i - 1]
        d1 = schedule_dates[i]
        if d1 <= eval_date:
            continue
        dt = curve_day_count.yearFraction(max(d0, eval_date), d1)
        df = curve.discount(d1)
        pv += notional * spread * dt * df
    return pv


def compute_valuation_adjustments(note_data, curve, curve_day_count):
    eval_date = ql.Settings.instance().evaluationDate
    note_notional = float(note_data.get('note_notional', 100000000.0))
    schedule_dates = build_note_dates(note_data)

    va = note_data.get('valuation_adjustments', {})
    fees_bp = float(va.get('fees_bp', 0.0))
    funding_bp = float(va.get('funding_bp', 0.0))
    csa_bp = float(va.get('csa_bp', 0.0))
    residual_basis_bp = float(va.get('residual_basis_bp', 0.0))

    fees = spread_cost_from_schedule(note_notional, schedule_dates, eval_date, curve, curve_day_count, fees_bp)
    funding = spread_cost_from_schedule(note_notional, schedule_dates, eval_date, curve, curve_day_count, funding_bp)
    csa = spread_cost_from_schedule(note_notional, schedule_dates, eval_date, curve, curve_day_count, csa_bp)
    residual = spread_cost_from_schedule(
        note_notional,
        schedule_dates,
        eval_date,
        curve,
        curve_day_count,
        residual_basis_bp,
    )

    total = fees + funding + csa + residual
    return {
        'pv_fees': fees,
        'pv_funding': funding,
        'pv_csa': csa,
        'pv_residual_basis': residual,
        'pv_total_adjustments': total,
    }


def price_spire_note(note_data, curve_json):
    evaluation_date = parse_date(note_data['evaluation_date'])
    note_curve_cfg, note_curve_name = select_note_curve(note_data, curve_json)
    collateral_curve_cfg, collateral_curve_name = select_collateral_curve(note_data, curve_json)
    note_curve, note_curve_day_count = build_discount_curve(note_curve_cfg, evaluation_date)
    collateral_curve, collateral_curve_day_count = build_discount_curve(collateral_curve_cfg, evaluation_date)
    note_notional = float(note_data.get('note_notional', 100000000.0))
    issue_price = float(note_data.get('issue_price', 100.0))

    note_leg = price_note(note_data, note_curve, note_curve_day_count)
    collateral_leg = model_collateral_pv(note_data['collateral'], collateral_curve, collateral_curve_day_count)
    adjustments = compute_valuation_adjustments(note_data, note_curve, note_curve_day_count)

    swap_cfg = note_data.get('swap', {})
    swap_mode = swap_cfg.get('mode', 'calibration_residual')

    if swap_mode == 'calibration_residual':
        pv_swap = (
            note_leg['pv_note']
            - collateral_leg['pv_collateral']
            + adjustments['pv_total_adjustments']
        )
    else:
        raise ValueError(f'Unsupported swap mode: {swap_mode}')

    lhs = note_leg['pv_note']
    rhs = collateral_leg['pv_collateral'] + pv_swap - adjustments['pv_total_adjustments']

    # Convert all decomposition legs to percentage terms per 100 note notional.
    scale_to_pct = 100.0 / note_notional
    pv_note_pct = lhs * scale_to_pct
    pv_collateral_pct = collateral_leg['pv_collateral'] * scale_to_pct
    pv_collateral_model_pct = collateral_leg['pv_collateral_model'] * scale_to_pct
    pv_swap_pct = pv_swap * scale_to_pct
    pv_fees_pct = adjustments['pv_fees'] * scale_to_pct
    pv_funding_pct = adjustments['pv_funding'] * scale_to_pct
    pv_csa_pct = adjustments['pv_csa'] * scale_to_pct
    pv_residual_basis_pct = adjustments['pv_residual_basis'] * scale_to_pct
    pv_total_adjustments_pct = adjustments['pv_total_adjustments'] * scale_to_pct
    lhs_pct = lhs * scale_to_pct
    rhs_pct = rhs * scale_to_pct

    return {
        'evaluation_date': evaluation_date.ISO(),
        'note_discount_curve_name': note_curve_name,
        'collateral_discount_curve_name': collateral_curve_name,
        'issue_price': issue_price,
        'note_notional': note_notional,
        'pv_note': note_leg['pv_note'],
        'pv_collateral': collateral_leg['pv_collateral'],
        'pv_collateral_model': collateral_leg['pv_collateral_model'],
        'collateral_valuation_method': collateral_leg['valuation_method'],
        'pv_swap': pv_swap,
        'pv_adjustments': adjustments,
        'identity_lhs_pv_note': lhs,
        'identity_rhs_reconstructed': rhs,
        'identity_error': lhs - rhs,
        'price_pct': {
            'pv_note': pv_note_pct,
            'pv_collateral': pv_collateral_pct,
            'pv_collateral_model': pv_collateral_model_pct,
            'pv_swap': pv_swap_pct,
            'pv_fees': pv_fees_pct,
            'pv_funding': pv_funding_pct,
            'pv_csa': pv_csa_pct,
            'pv_residual_basis': pv_residual_basis_pct,
            'pv_total_adjustments': pv_total_adjustments_pct,
            'identity_lhs_pv_note': lhs_pct,
            'identity_rhs_reconstructed': rhs_pct,
            'identity_error': lhs_pct - rhs_pct,
        },
        'note_leg': note_leg,
        'collateral_leg': collateral_leg,
        'swap_mode': swap_mode,
    }


def print_report(note_data, result):
    pct = result['price_pct']
    print(f"{note_data['description']} ({note_data['instrument_id']})")
    print(f"Evaluation date: {result['evaluation_date']}")
    print(f"Note discount curve: {result['note_discount_curve_name']}")
    print(f"Collateral discount curve: {result['collateral_discount_curve_name']}")
    print(f"Issue price (%): {result['issue_price']:.4f}")
    print(f"PV(Note) %: {pct['pv_note']:.6f}")
    print(f"PV(Collateral) %: {pct['pv_collateral']:.6f}")
    print(f"PV(Collateral model estimate) %: {pct['pv_collateral_model']:.6f}")
    print(f"Collateral valuation method: {result['collateral_valuation_method']}")
    print(f"PV(Swap) %: {pct['pv_swap']:.6f}")
    print(f"PV(Fees) %: {pct['pv_fees']:.6f}")
    print(f"PV(Funding) %: {pct['pv_funding']:.6f}")
    print(f"PV(CSA) %: {pct['pv_csa']:.6f}")
    print(f"PV(Residual Basis) %: {pct['pv_residual_basis']:.6f}")
    print(f"PV(Adjustments Total) %: {pct['pv_total_adjustments']:.6f}")
    print(f"Check LHS PV(Note) %: {pct['identity_lhs_pv_note']:.6f}")
    print(f"Check RHS Collateral+Swap-Adjustments %: {pct['identity_rhs_reconstructed']:.6f}")
    print(f"Identity error %: {pct['identity_error']:.8f}")


def parse_args():
    parser = argparse.ArgumentParser(description='SPIRE collateral-mapped decomposition pricer')
    parser.add_argument('--bond-file', default=str(BOND_FILE), help='Path to SPIRE note JSON')
    parser.add_argument('--curve-file', default=str(CURVE_FILE), help='Path to swap curve JSON (single curve or catalog)')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    note_data = load_json(Path(args.bond_file))
    curve_json = load_json(Path(args.curve_file))
    result = price_spire_note(note_data, curve_json)
    print_report(note_data, result)
