import argparse
from datetime import date
import json
import math
from pathlib import Path

import QuantLib as ql
import random
from . import hullwhite

try:
    from reporting import pdf_report
except ModuleNotFoundError:
    import reporting.pdf_report as pdf_report


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
ASSETS_DIR = PROJECT_ROOT / 'assets'
CURVES_DIR = PROJECT_ROOT / 'curves'
CURVE_FILE = CURVES_DIR / 'swap_curves.json'
BOND_FILE = ASSETS_DIR / 'XS2725067362.json'


def today_date_string():
    return date.today().strftime('%d-%m-%Y')


def apply_runtime_pricing_defaults(data):
    if isinstance(data, dict) and data.get('instrument_id'):
        data = dict(data)
        data['evaluation_date'] = today_date_string()
    return data


def resolve_json_path(path: Path):
    if path.is_absolute():
        return path

    candidates = [
        path,
        PROJECT_ROOT / path,
        ASSETS_DIR / path,
        CURVES_DIR / path,
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    if path.parent == Path('.'):
        asset_candidate = ASSETS_DIR / path.name
        if asset_candidate.exists():
            return asset_candidate
        curve_candidate = CURVES_DIR / path.name
        if curve_candidate.exists():
            return curve_candidate

    return path


def load_json(path: Path):
    path = resolve_json_path(path)
    with open(path, 'r', encoding='utf-8-sig') as f:
        content = f.read().strip()
    if not content:
        raise ValueError(f'JSON file is empty: {path}')
    return apply_runtime_pricing_defaults(json.loads(content))


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
        'ACT/ACT': lambda: ql.ActualActual(ql.ActualActual.ISDA),
        'ACT/ACT (PERIODIC BASIS)': lambda: ql.ActualActual(ql.ActualActual.ISDA),
        'ACT/ACT (ICMA)': lambda: ql.ActualActual(ql.ActualActual.ISDA),
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

    freq = note_data.get('coupon_frequency', 'Annual')
    if freq is None or str(freq).strip().lower() in {'none', 'null', ''}:
        return [issue_date, maturity_date]

    schedule = build_regular_schedule(
        issue_date,
        maturity_date,
        freq,
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


def price_note(note_data, curve, curve_day_count, collateral_curve=None, collateral_curve_day_count=None):
    eval_date = ql.Settings.instance().evaluationDate
    note_day_count = get_day_count(note_data.get('accrual_day_count', '30/360'))
    coupon_structure = note_data.get('coupon_structure', 'fixed')

    if coupon_structure not in {'fixed', 'zero_coupon'}:
        raise ValueError(
            'Spire supports coupon_structure="fixed" or "zero_coupon" only. '
            f'Received coupon_structure="{coupon_structure}" for {note_data.get("instrument_id", "unknown")}. '
            'Use models/hullwhite.py for CMS/floating structures.'
        )

    notional = float(note_data.get('note_notional', 100000000.0))
    coupon_rate = float(note_data['fixed_coupon_rate'])
    issuer_spread_bp = float(note_data.get('credit_spread_bp', 0.0))

    # Build note dates; for zero_coupon the builder will return only issue and maturity
    dates = build_note_dates(note_data)
    maturity_date = dates[-1]

    def pv_to_horizon(horizon_date, redemption_pct):
        pv_coupons = 0.0
        pv_redemption = 0.0
        cashflows = []

        for i in range(1, len(dates)):
            d0 = dates[i - 1]
            d1 = dates[i]
            if d1 > horizon_date:
                break
            accrual = note_day_count.yearFraction(d0, d1)
            if d1 <= eval_date:
                continue
            coupon_cf = notional * coupon_rate * accrual
            df = discount_factor_with_issuer_spread(curve, curve_day_count, eval_date, d1, issuer_spread_bp)
            pv = coupon_cf * df
            pv_coupons += pv
            cashflows.append({'date': d1.ISO(), 'type': 'coupon', 'amount': coupon_cf, 'df': df, 'pv': pv})

        if horizon_date > eval_date:
            redemption_cf = notional * redemption_pct / 100.0
            df_horizon = discount_factor_with_issuer_spread(
                curve,
                curve_day_count,
                eval_date,
                horizon_date,
                issuer_spread_bp,
            )
            pv_redemption = redemption_cf * df_horizon
            cashflows.append(
                {
                    'date': horizon_date.ISO(),
                    'type': 'redemption',
                    'amount': redemption_cf,
                    'df': df_horizon,
                    'pv': pv_redemption,
                }
            )

        return {
            'horizon_date': horizon_date,
            'pv_note': pv_coupons + pv_redemption,
            'pv_note_coupons': pv_coupons,
            'pv_note_redemption': pv_redemption,
            'cashflows': cashflows,
        }

    raw_call_dates = note_data.get('call_dates', [])
    issuer_call_applicable = str(note_data.get('issuer_call', '')).strip().lower() == 'applicable'
    eligible_call_dates = []
    if issuer_call_applicable and raw_call_dates:
        eligible_call_dates = sorted(
            d for d in [parse_date(x) for x in raw_call_dates]
            if eval_date <= d < maturity_date
        )

    callable_type = note_data.get('callable_type', '').strip().lower()
    autocall_trigger = note_data.get('autocall_trigger', {}) or {}

    def estimate_forward_dirty_price(collateral_data, collateral_curve, collateral_day_count, call_date, eval_date):
        # Estimate forward dirty price (%) of collateral at call_date using model cashflows
        issue = parse_date(collateral_data['issue_date'])
        mat = parse_date(collateral_data['maturity_date'])
        principal = float(collateral_data.get('principal_amount', collateral_data.get('principal', 0.0)))

        # Build schedule for collateral same as model_collateral_pv
        schedule = build_regular_schedule(
            issue,
            mat,
            collateral_data.get('coupon_frequency', 'Semiannual'),
            collateral_data.get('calendar', 'TARGET'),
            collateral_data.get('business_day_convention', 'Following'),
        )

        day_count = get_day_count(collateral_data.get('day_count', 'ActualActual'))
        inflation_assumption = collateral_data.get('inflation_assumption', {})

        df_call = collateral_curve.discount(call_date)
        sum_fwd = 0.0
        for i in range(1, len(schedule)):
            d1 = schedule[i]
            if d1 < call_date:
                continue
            # coupon or redemption amount at d1
            accrual = day_count.yearFraction(schedule[i - 1], d1)
            index_ratio = inflation_factor(eval_date, d1, inflation_assumption)
            coupon_rate = float(collateral_data.get('coupon_rate', 0.0))
            coupon_cf = principal * coupon_rate * accrual * index_ratio
            redemption_cf = 0.0
            if d1 == mat:
                redemption_cf = principal * index_ratio

            cf = coupon_cf + redemption_cf
            df_t = collateral_curve.discount(d1)
            # forward price contribution discounted to call_date
            if df_call > 0:
                sum_fwd += cf * (df_t / df_call)

        # forward dirty price as percent of principal
        if principal <= 0:
            return 0.0
        return 100.0 * (sum_fwd / principal)


    def monte_carlo_call_probability(mean_pct, vol, eval_date, call_date, trigger_level_pct, paths=1000):
        # mean_pct and trigger_level_pct are percentages (e.g., 71.3)
        if mean_pct is None:
            return 0.0
        T = ql.Actual365Fixed().yearFraction(eval_date, call_date)
        if T <= 0:
            return 1.0 if mean_pct >= (trigger_level_pct or 0.0) else 0.0
        mean_dec = max(mean_pct / 100.0, 1e-9)
        sigma = float(vol) * math.sqrt(T)
        mu = math.log(mean_dec) - 0.5 * sigma * sigma
        count = 0
        for _ in range(int(paths)):
            sample = math.exp(random.gauss(mu, sigma))
            sample_pct = sample * 100.0
            if trigger_level_pct is not None and sample_pct >= float(trigger_level_pct):
                count += 1
        return float(count) / float(paths)

    call_redemption_pct = float(note_data.get('issuer_call_redemption_amount_pct', 100.0))
    maturity_redemption_amount = float(note_data.get('redemption', note_data.get('par', 100.0)))
    par_amount = float(note_data.get('par', 100.0))
    maturity_redemption_pct = 100.0 * maturity_redemption_amount / par_amount if par_amount else 100.0

    # Build call scenarios, evaluating autocall triggers when specified
    call_scenarios = []
    for d in eligible_call_dates:
        sc = pv_to_horizon(d, call_redemption_pct)
        sc['horizon_date'] = d
        sc['triggered'] = None
        # If autocall trigger defined and collateral curve available, evaluate forward dirty price
        if callable_type == 'autocall_forward_dirty' and autocall_trigger and collateral_curve is not None:
            trigger_level = autocall_trigger.get('trigger_level_pct') or autocall_trigger.get('trigger_level')
            if trigger_level is not None:
                try:
                    fd = estimate_forward_dirty_price(note_data.get('collateral', {}), collateral_curve, collateral_curve_day_count, d, eval_date)
                except Exception:
                    fd = None
                sc['forward_dirty_pct'] = fd
                sc['triggered'] = (fd is not None and fd >= float(trigger_level))
            else:
                # No explicit trigger level: compute forward_dirty_pct for informational use
                try:
                    fd = estimate_forward_dirty_price(note_data.get('collateral', {}), collateral_curve, collateral_curve_day_count, d, eval_date)
                except Exception:
                    fd = None
                sc['forward_dirty_pct'] = fd
                sc['triggered'] = None
        call_scenarios.append(sc)

    maturity_scenario = pv_to_horizon(maturity_date, maturity_redemption_pct)

    valuation_mode = note_data.get('valuation_mode', 'to_maturity')
    # If trigger level absent but Monte Carlo params provided, compute probabilistic call
    monte_info = None
    if callable_type == 'autocall_forward_dirty' and call_scenarios and autocall_trigger:
        trigger_level = autocall_trigger.get('trigger_level_pct') or autocall_trigger.get('trigger_level')
        monte_paths = autocall_trigger.get('monte_carlo_paths')
        monte_vol = autocall_trigger.get('monte_carlo_vol')
        if trigger_level is None and monte_paths and monte_vol is not None:
            # For simplicity handle single-call-date case: compute probability of call at earliest call date
            sc = min(call_scenarios, key=lambda s: int(s['horizon_date'].serialNumber()))
            fd_mean = sc.get('forward_dirty_pct')
            # default trigger candidate: use collateral market dirty price if available
            trigger_candidate = note_data.get('collateral', {}).get('market_dirty_price')
            p_call = monte_carlo_call_probability(fd_mean, monte_vol, eval_date, sc['horizon_date'], trigger_candidate, paths=monte_paths)
            monte_info = {
                'monte_paths': int(monte_paths),
                'monte_vol': float(monte_vol),
                'trigger_level_used_pct': trigger_candidate,
                'p_call': p_call,
                'call_date': sc['horizon_date'].ISO(),
                'fd_mean_pct': fd_mean,
            }
            # expected PV under simple two-outcome: call vs maturity
            expected_pv = p_call * sc['pv_note'] + (1.0 - p_call) * maturity_scenario['pv_note']
            # set selected to expected result
            selected = {
                'pv_note': expected_pv,
                'pv_note_coupons': sc.get('pv_note_coupons', 0.0) * p_call + maturity_scenario.get('pv_note_coupons', 0.0) * (1 - p_call),
                'pv_note_redemption': sc.get('pv_note_redemption', 0.0) * p_call + maturity_scenario.get('pv_note_redemption', 0.0) * (1 - p_call),
                'cashflows': sc.get('cashflows', []) if p_call >= 0.5 else maturity_scenario.get('cashflows', []),
                'horizon_date': sc['horizon_date'],
            }

    if 'selected' in locals() and selected is not None:
        # monte-selected already computed above
        pass
    elif valuation_mode == 'first_call' and call_scenarios:
        selected = min(call_scenarios, key=lambda s: int(s['horizon_date'].serialNumber()))
    elif valuation_mode == 'worst_call' and call_scenarios:
        selected = min(call_scenarios, key=lambda s: s['pv_note'])
    elif valuation_mode == 'call_and_maturity' and call_scenarios:
        # If any call scenario was deterministically triggered, select the earliest triggered call
        triggered_calls = [s for s in call_scenarios if s.get('triggered')]
        if triggered_calls:
            selected = min(triggered_calls, key=lambda s: int(s['horizon_date'].serialNumber()))
        else:
            # No deterministic trigger: fall back to maturity
            selected = maturity_scenario
    elif valuation_mode in {'to_maturity'}:
        selected = maturity_scenario
    else:
        raise ValueError(f'Unsupported valuation_mode: {valuation_mode}')

    return {
        'pv_note': selected['pv_note'],
        'pv_note_coupons': selected['pv_note_coupons'],
        'pv_note_redemption': selected['pv_note_redemption'],
        'cashflows': selected['cashflows'],
        'valuation_mode': valuation_mode,
        'selected_call_date': selected['horizon_date'].ISO(),
        'npv_to_first_call': (
            min(call_scenarios, key=lambda s: int(s['horizon_date'].serialNumber()))['pv_note']
            if call_scenarios else maturity_scenario['pv_note']
        ),
        'npv_to_worst_call': (
            min(call_scenarios, key=lambda s: s['pv_note'])['pv_note']
            if call_scenarios else maturity_scenario['pv_note']
        ),
        'npv_to_maturity': maturity_scenario['pv_note'],
        'monte_info': monte_info,
        'call_scenarios': [
            {
                'horizon_date': s['horizon_date'].ISO(),
                'pv_note': s['pv_note'],
                'pv_note_coupons': s.get('pv_note_coupons', 0.0),
                'pv_note_redemption': s.get('pv_note_redemption', 0.0),
                'triggered': s.get('triggered'),
                'forward_dirty_pct': s.get('forward_dirty_pct'),
            }
            for s in call_scenarios
        ],
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
    principal = float(collateral_data['principal_amount'])
    collateral_spread_bp = float(
        collateral_data.get('collateral_spread_bp', collateral_data.get('collateral_spread', 0.0))
    )

    # Handle multi-tranche facilities (new structure) vs. simple collateral (legacy)
    # Priority: explicit coupon_rate field > tranches array > fallback to 0.0
    explicit_coupon_rate = collateral_data.get('coupon_rate')
    tranches = collateral_data.get('tranches', None)
    
    if explicit_coupon_rate is not None:
        # Use explicit coupon_rate if provided (takes precedence)
        coupon_rate = float(explicit_coupon_rate)
    elif tranches:
        # Multi-tranche facility: calculate weighted average coupon
        coupon_rate = 0.0
        total_principal = 0.0
        for tranche in tranches:
            tranche_principal = float(tranche.get('principal', 0.0))
            tranche_coupon_type = tranche.get('coupon_type', 'fixed')
            
            if tranche_coupon_type == 'inflation_linked':
                coupon = float(tranche.get('coupon_rate', 0.0))
            elif tranche_coupon_type == 'fixed':
                coupon = float(tranche.get('coupon_rate', 0.0))
            elif tranche_coupon_type == 'floating':
                # For floating: use spread + assumed base rate
                spread_bp = float(tranche.get('coupon_spread_bp', 0.0)) / 10000.0
                assumed_base = 0.03  # 3% assumed base rate for LIBOR/EURIBOR
                coupon = assumed_base + spread_bp
            else:
                coupon = 0.0
            
            coupon_rate += tranche_principal * coupon
            total_principal += tranche_principal
        
        if total_principal > 0:
            coupon_rate /= total_principal
    else:
        # Fallback: no tranches, no explicit coupon_rate
        coupon_rate = 0.0

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
        df = discount_factor_with_issuer_spread(
            curve,
            curve_day_count,
            eval_date,
            d1,
            collateral_spread_bp,
        )
        pv_cf = coupon_cf * df
        pv_model += pv_cf
        cashflows.append({'date': d1.ISO(), 'type': 'coupon', 'amount': coupon_cf, 'df': df, 'pv': pv_cf})

    if maturity_date > eval_date:
        index_ratio_mat = inflation_factor(eval_date, maturity_date, inflation_assumption)
        redemption_cf = principal * index_ratio_mat
        df_mat = discount_factor_with_issuer_spread(
            curve,
            curve_day_count,
            eval_date,
            maturity_date,
            collateral_spread_bp,
        )
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

    note_leg = price_note(note_data, note_curve, note_curve_day_count, collateral_curve=collateral_curve, collateral_curve_day_count=collateral_curve_day_count)
    collateral_leg = model_collateral_pv(note_data['collateral'], collateral_curve, collateral_curve_day_count)
    adjustments = compute_valuation_adjustments(note_data, note_curve, note_curve_day_count)

    swap_cfg = note_data.get('swap', {})
    swap_mode = swap_cfg.get('mode', 'calibration_residual')

    # If collateral is repo-financed, prefer repo purchase price for collateral PV
    collateral_repo = note_data.get('collateral_repo', {}) or {}
    if collateral_repo.get('is_repo_financed') and collateral_repo.get('repo_purchase_price_pct') is not None:
        principal = float(note_data.get('collateral', {}).get('principal_amount', note_notional))
        collateral_leg['pv_collateral'] = principal * float(collateral_repo.get('repo_purchase_price_pct')) / 100.0

    if swap_mode in {'calibration_residual', 'explicit_cashflows'}:
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
    pv_note_to_call_pct = note_leg.get('npv_to_first_call', lhs) * scale_to_pct
    pv_note_to_worst_pct = note_leg.get('npv_to_worst_call', lhs) * scale_to_pct
    pv_note_to_maturity_pct = note_leg.get('npv_to_maturity', lhs) * scale_to_pct
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

    result = {
        'evaluation_date': evaluation_date.ISO(),
        'note_discount_curve_name': note_curve_name,
        'collateral_discount_curve_name': collateral_curve_name,
        'valuation_mode': note_leg.get('valuation_mode', note_data.get('valuation_mode', 'to_maturity')),
        'selected_call_date': note_leg.get('selected_call_date', parse_date(note_data['maturity_date']).ISO()),
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
        'npv_to_first_call': note_leg.get('npv_to_first_call', note_leg['pv_note']),
        'npv_to_worst_call': note_leg.get('npv_to_worst_call', note_leg['pv_note']),
        'npv_to_maturity': note_leg.get('npv_to_maturity', note_leg['pv_note']),
        'price_pct': {
            'pv_note': pv_note_pct,
            'pv_note_to_call': pv_note_to_call_pct,
            'pv_note_to_worst': pv_note_to_worst_pct,
            'pv_note_to_maturity': pv_note_to_maturity_pct,
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
    # Compute YTM (promised and expected). SPIRE uses deterministic issuer spread
    try:
        day_count = note_curve_day_count
        freq = hullwhite.get_compounding_frequency_per_year(note_data)
        eval_d = evaluation_date
        amounts = []
        times = []
        for cf in note_leg.get('cashflows', []):
            pd = ql.DateParser.parseISO(cf['date'])
            t = day_count.yearFraction(eval_d, pd)
            if t <= 0.0:
                continue
            amounts.append(float(cf.get('amount', 0.0)))
            times.append(float(t))
        # add redemption if present in note_leg but not in cashflows
        t_red = day_count.yearFraction(eval_d, parse_date(note_data['maturity_date']))
        if t_red > 0.0 and (not any(abs(x - t_red) < 1e-9 for x in times)):
            redemption = float(note_data.get('redemption', note_data.get('par', 100.0)))
            amounts.append(float(redemption))
            times.append(float(t_red))

        ytm_promised = hullwhite.solve_ytm_from_cashflows(float(note_leg.get('pv_note', 0.0)), amounts, times, freq)
        # SPIRE does not model default separately; expected == promised
        ytm_expected = ytm_promised
    except Exception:
        ytm_promised = None
        ytm_expected = None

    result['ytm_promised'] = ytm_promised
    result['ytm_expected'] = ytm_expected
    result['ytm'] = ytm_expected
    return result





def print_report(note_data, result):
    pct = result['price_pct']
    print(f"{note_data['description']} ({note_data['instrument_id']})")
    print(f"Evaluation date: {result['evaluation_date']}")
    print(f"Note discount curve: {result['note_discount_curve_name']}")
    print(f"Collateral discount curve: {result['collateral_discount_curve_name']}")
    print(f"Valuation mode: {result.get('valuation_mode', 'to_maturity')}")
    print(f"Selected call date: {result.get('selected_call_date', 'N/A')}")
    print(f"Issue price (%): {result['issue_price']:.4f}")
    print(f"PV(Note) %: {pct['pv_note']:.6f}")
    print(f"PV(Note) to_call %: {pct['pv_note_to_call']:.6f}")
    print(f"PV(Note) to_worst %: {pct['pv_note_to_worst']:.6f}")
    print(f"PV(Note) to_maturity %: {pct['pv_note_to_maturity']:.6f}")
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
    note_leg = result.get('note_leg', {})
    monte = note_leg.get('monte_info') or result.get('monte_info')
    if monte:
        print('Monte Carlo trigger info:')
        for k, v in monte.items():
            print(f"  {k}: {v}")


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
    pdf_path = pdf_report.create_pdf_report(
        model_name='spire',
        instrument_id=note_data.get('instrument_id', 'unknown'),
        input_payload=note_data,
        output_payload=result,
    )
    print(f'PDF report: {pdf_path}')
