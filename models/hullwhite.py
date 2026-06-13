import argparse
from datetime import date
import json
import math
from pathlib import Path

import QuantLib as ql

try:
    from reporting import pdf_report
except ModuleNotFoundError:
    import reporting.pdf_report as pdf_report

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
ASSETS_DIR = PROJECT_ROOT / 'assets'
CURVES_DIR = PROJECT_ROOT / 'curves'
CURVE_FILE = CURVES_DIR / 'swap_curves.json'
BOND_FILE = ASSETS_DIR / 'XS1693822634.json'


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

    # If a bare filename is provided, prefer assets for bond JSON and curves for curve JSON.
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

    try:
        return apply_runtime_pricing_defaults(json.loads(content))
    except json.JSONDecodeError as exc:
        raise ValueError(f'Invalid JSON in {path}: {exc}') from exc


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


def get_day_count(name: str):
    day_counts = {
        'Actual365Fixed': ql.Actual365Fixed,
        'Actual360': ql.Actual360,
        'Thirty360': lambda: ql.Thirty360(ql.Thirty360.BondBasis),
        '30/360': lambda: ql.Thirty360(ql.Thirty360.BondBasis),
        'ActualActual': lambda: ql.ActualActual(ql.ActualActual.ISDA),
        'ACT/ACT': lambda: ql.ActualActual(ql.ActualActual.ISDA),
        'ACT/ACT (PERIODIC BASIS)': lambda: ql.ActualActual(ql.ActualActual.ISDA),
        'ACT/ACT (ICMA)': lambda: ql.ActualActual(ql.ActualActual.ISDA),
    }
    if name not in day_counts:
        raise ValueError(f'Unsupported day count: {name}')
    return day_counts[name]()


def get_business_day_convention(name: str):
    conventions = {
        'Unadjusted': ql.Unadjusted,
        'ModifiedFollowing': ql.ModifiedFollowing,
        'Following': ql.Following,
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
        'ActualActual': lambda: ql.ActualActual(ql.ActualActual.ISDA),
        'ACT/ACT': lambda: ql.ActualActual(ql.ActualActual.ISDA),
    }
    if name not in ref_day_counts:
        raise ValueError(f'Unsupported reference rate day count: {name}')
    return ref_day_counts[name]()


def parse_spread_from_denomination(bond_data):
    import re
    denom = str(bond_data.get('denomination', '') or '')
    # Look for patterns like +0.8% or +80bp or +0.80%
    m_pct = re.search(r"([+-]?\d+(?:\.\d+)?)%", denom)
    if m_pct:
        try:
            val = float(m_pct.group(1))
            # percent to decimal
            return val / 100.0
        except Exception:
            pass
    m_bp = re.search(r"([+-]?\d+(?:\.\d+)?)\s*bps?|bp\b", denom, flags=re.IGNORECASE)
    if m_bp:
        try:
            val = float(m_bp.group(1))
            return val / 10000.0
        except Exception:
            pass
    return None


def normalize_curve_catalog(curve_json):
    if isinstance(curve_json, dict):
        return None
    if not isinstance(curve_json, list):
        raise ValueError('Curve file must be a single curve object or a list of curve objects.')

    catalog = {}
    for entry in curve_json:
        if not isinstance(entry, dict):
            continue
        curve_name = entry.get('curve_name')
        if not curve_name:
            continue
        catalog[curve_name] = entry
    if not catalog:
        raise ValueError('No named curves found in curve catalog JSON.')
    return catalog


def infer_currency_from_isin(isin):
    if not isin:
        return None
    prefix = str(isin).strip().upper()[:2]
    if prefix in {'US', 'XS', 'EU'}:
        if prefix == 'US':
            return 'USD'
        if prefix in {'XS', 'EU'}:
            return 'EUR'
    return None


def select_discount_curve_config(curve_json, bond_data):
    catalog = normalize_curve_catalog(curve_json)
    if catalog is None:
        return curve_json

    # Allow the curve catalog to explicitly map curves to instruments.
    # If a curve entry contains an `instruments` or `applies_to` list, and the
    # bond's `instrument_id` or `isin` appears there, prefer that curve.
    instr = str(bond_data.get('instrument_id') or bond_data.get('isin') or '').strip()
    if instr:
        for name, cfg in catalog.items():
            applies = cfg.get('instruments') or cfg.get('applies_to') or cfg.get('instrument_ids')
            if isinstance(applies, (list, tuple)) and instr in [str(x).strip() for x in applies]:
                return cfg

    requested_name = bond_data.get('discount_curve_name') or bond_data.get('curve_name')
    if requested_name:
        if requested_name not in catalog:
            raise ValueError(f"Requested discount_curve_name not found: {requested_name}")
        return catalog[requested_name]

    currency = str(
        bond_data.get('currency')
        or infer_currency_from_isin(bond_data.get('instrument_id'))
        or 'EUR'
    ).upper()
    default_by_currency = {
        'EUR': 'EUR_OIS_PROXY',
        'USD': 'USD_OIS_PROXY',
    }
    default_name = default_by_currency.get(currency, 'EUR_OIS_PROXY')
    if default_name in catalog:
        return catalog[default_name]

    for name, cfg in catalog.items():
        if name.upper().startswith(f'{currency}_') and 'OIS' in name.upper() and 'pillars' in cfg:
            return cfg

    raise ValueError(f'No discount curve available for currency={currency}. Add discount_curve_name in bond JSON.')


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
    raise ValueError(f'Unsupported tenor format: {tenor}')


def tenor_to_years(tenor: str):
    value = tenor.strip().upper()
    if value == 'ON':
        return 1.0 / 365.0
    if value.endswith('D'):
        return float(value[:-1]) / 365.0
    if value.endswith('W'):
        return float(value[:-1]) * 7.0 / 365.0
    if value.endswith('M'):
        return float(value[:-1]) / 12.0
    if value.endswith('Y'):
        return float(value[:-1])
    raise ValueError(f'Unsupported tenor format: {tenor}')


def select_named_curve_config(curve_json, curve_name):
    if not curve_name:
        return None
    catalog = normalize_curve_catalog(curve_json)
    if catalog is None:
        raise ValueError('Named curve selection requires a curve catalog JSON list.')
    if curve_name not in catalog:
        raise ValueError(f'Requested curve not found in catalog: {curve_name}')
    return catalog[curve_name]


def interpolate_surface_vol(surface_cfg, target_expiry_years, target_tenor_years):
    quotes = surface_cfg.get('quotes', [])
    if not quotes:
        return 0.0

    parsed = []
    for q in quotes:
        expiry = tenor_to_years(q['expiry'])
        tenor = tenor_to_years(q['tenor'])
        vol = float(q['vol'])
        parsed.append((expiry, tenor, vol))

    if not parsed:
        return 0.0

    tenors = sorted(set(t for _, t, _ in parsed))
    nearest_tenor = min(tenors, key=lambda x: abs(x - target_tenor_years))
    slice_points = sorted((e, v) for e, t, v in parsed if t == nearest_tenor)
    if not slice_points:
        return 0.0

    if target_expiry_years <= slice_points[0][0]:
        return slice_points[0][1]
    if target_expiry_years >= slice_points[-1][0]:
        return slice_points[-1][1]

    for i in range(1, len(slice_points)):
        e0, v0 = slice_points[i - 1]
        e1, v1 = slice_points[i]
        if e0 <= target_expiry_years <= e1:
            if e1 == e0:
                return v1
            w = (target_expiry_years - e0) / (e1 - e0)
            return v0 + w * (v1 - v0)
    return slice_points[-1][1]


def select_hw_vol_surface_config(curve_json, bond_data):
    catalog = normalize_curve_catalog(curve_json)
    if catalog is None:
        return None, None

    requested_name = (
        bond_data.get('hw_vol_surface_name')
        or bond_data.get('calibration_vol_surface_name')
        or bond_data.get('cms_vol_adjustment', {}).get('vol_surface_name')
    )
    if requested_name:
        cfg = catalog.get(requested_name)
        if cfg is None:
            raise ValueError(f'Requested HW vol surface not found in catalog: {requested_name}')
        if 'quotes' not in cfg:
            raise ValueError(f'HW vol surface {requested_name} is missing quotes.')
        return cfg, requested_name

    currency = str(
        bond_data.get('currency')
        or infer_currency_from_isin(bond_data.get('instrument_id'))
        or 'EUR'
    ).upper()

    default_by_currency = {
        'EUR': 'EUR_SWPTN_VOL_SURFACE_PROXY',
    }
    default_name = default_by_currency.get(currency)
    if default_name and default_name in catalog and 'quotes' in catalog[default_name]:
        return catalog[default_name], default_name

    for name, cfg in catalog.items():
        upper_name = name.upper()
        if 'SWPTN' in upper_name and 'VOL' in upper_name and 'quotes' in cfg:
            return cfg, name

    return None, None


def build_swap_index_for_calibration(currency, curve_handle):
    ccy = str(currency or 'EUR').upper()
    if ccy == 'USD':
        return ql.USDLibor(ql.Period(3, ql.Months), curve_handle)
    return ql.Euribor6M(curve_handle)


def calibrate_hw_parameters(discount_curve, bond_data, vol_surface_cfg):
    quotes = vol_surface_cfg.get('quotes', [])
    if not quotes:
        raise ValueError('Vol surface has no quotes for HW calibration.')

    curve_handle = ql.YieldTermStructureHandle(discount_curve)
    currency = bond_data.get('currency') or infer_currency_from_isin(bond_data.get('instrument_id')) or 'EUR'
    index = build_swap_index_for_calibration(currency, curve_handle)

    fixed_leg_tenor = ql.Period(1, ql.Years)
    fixed_leg_day_count = ql.Thirty360(ql.Thirty360.BondBasis)
    floating_leg_day_count = index.dayCounter()

    helpers = []
    for quote in quotes:
        expiry = quote.get('expiry')
        tenor = quote.get('tenor')
        vol = float(quote.get('vol', 0.0))
        if not expiry or not tenor or vol <= 0.0:
            continue

        maturity = tenor_to_period(expiry)
        length = tenor_to_period(tenor)
        vol_handle = ql.QuoteHandle(ql.SimpleQuote(vol))
        helper = ql.SwaptionHelper(
            maturity,
            length,
            vol_handle,
            index,
            fixed_leg_tenor,
            fixed_leg_day_count,
            floating_leg_day_count,
            curve_handle,
        )
        helpers.append(helper)

    if not helpers:
        raise ValueError('No valid swaption helpers could be created from vol surface quotes.')

    init_a = float(bond_data.get('hw_a', 0.03))
    init_sigma = float(bond_data.get('hw_sigma', 0.01))

    model = ql.HullWhite(curve_handle, init_a, init_sigma)
    engine = ql.JamshidianSwaptionEngine(model)
    for helper in helpers:
        helper.setPricingEngine(engine)

    method = ql.LevenbergMarquardt()
    end_criteria = ql.EndCriteria(500, 250, 1e-8, 1e-8, 1e-8)
    model.calibrate(helpers, method, end_criteria)

    params = model.params()
    calibrated_a = float(params[0])
    calibrated_sigma = float(params[1])

    sq_errors = []
    for helper in helpers:
        market = helper.marketValue()
        model_value = helper.modelValue()
        if market != 0.0:
            err = (model_value - market) / market
        else:
            err = model_value - market
        sq_errors.append(err * err)

    rmse = math.sqrt(sum(sq_errors) / len(sq_errors)) if sq_errors else 0.0

    return {
        'a': calibrated_a,
        'sigma': calibrated_sigma,
        'rmse': rmse,
        'num_helpers': len(helpers),
    }


def resolve_hw_parameters(curve, bond_data, curve_json):
    params = {
        'a': float(bond_data.get('hw_a', 0.03)),
        'sigma': float(bond_data.get('hw_sigma', 0.01)),
        'source': 'input',
        'vol_surface_name': None,
        'rmse': None,
        'num_helpers': 0,
        'error': None,
    }

    calibration_cfg = bond_data.get('hw_calibration', {})
    calibration_enabled = bool(calibration_cfg.get('enabled', True))
    if not calibration_enabled or curve_json is None:
        return params

    try:
        vol_surface_cfg, vol_surface_name = select_hw_vol_surface_config(curve_json, bond_data)
        if vol_surface_cfg is None:
            return params

        calibrated = calibrate_hw_parameters(curve, bond_data, vol_surface_cfg)
        params['a'] = calibrated['a']
        params['sigma'] = calibrated['sigma']
        params['source'] = 'calibrated'
        params['vol_surface_name'] = vol_surface_name
        params['rmse'] = calibrated['rmse']
        params['num_helpers'] = calibrated['num_helpers']
        return params
    except Exception as exc:
        params['error'] = str(exc)
        return params


def build_cms_context(curve_json, bond_data, eval_date, default_curve):
    if bond_data.get('coupon_structure', 'fixed') != 'cms_resettable':
        return None

    cms_curve = default_curve
    cms_curve_name = bond_data.get('cms_swap_curve_name')
    if cms_curve_name:
        cms_curve_cfg = select_named_curve_config(curve_json, cms_curve_name)
        if 'pillars' not in cms_curve_cfg:
            raise ValueError(f'CMS curve {cms_curve_name} has no pillars.')
        cms_curve = build_discount_curve(cms_curve_cfg, eval_date)

    cms_vol_cfg = bond_data.get('cms_vol_adjustment', {})
    vol_surface_cfg = None
    if cms_vol_cfg.get('enabled', False):
        vol_curve_name = cms_vol_cfg.get('vol_surface_name')
        if not vol_curve_name:
            raise ValueError('cms_vol_adjustment.enabled=true requires vol_surface_name.')
        vol_surface_cfg = select_named_curve_config(curve_json, vol_curve_name)
        if 'quotes' not in vol_surface_cfg:
            raise ValueError(f'Vol surface {vol_curve_name} is missing quotes.')

    return {
        'cms_curve': cms_curve,
        'vol_surface_cfg': vol_surface_cfg,
    }


def get_coupon_rate(curve, d0, d1, bond_data, eval_date, cms_context=None):
    structure = bond_data.get('coupon_structure', 'fixed')

    # Pure floating-rate note: compute forward rate + spread
    if structure == 'floating':
        ref_day_count = get_reference_day_count(
            bond_data.get('float_reference_day_count', 'Actual360')
        )
        spread = bond_data.get('float_spread')
        if spread is None:
            parsed = parse_spread_from_denomination(bond_data)
            spread = parsed if parsed is not None else 0.0
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

    if structure == 'fixed':
        return bond_data['fixed_coupon_rate']

    if structure == 'cms_resettable':
        cms_curve = curve
        vol_surface_cfg = None
        if cms_context is not None:
            cms_curve = cms_context.get('cms_curve', curve)
            vol_surface_cfg = cms_context.get('vol_surface_cfg')

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
            annuity += alpha * cms_curve.discount(f1)

        if annuity <= 0.0:
            cms_rate = 0.0
        else:
            cms_rate = (cms_curve.discount(reset_date) - cms_curve.discount(swap_end)) / annuity

        cms_vol_cfg = bond_data.get('cms_vol_adjustment', {})
        if cms_vol_cfg.get('enabled', False) and vol_surface_cfg is not None:
            expiry_years = max(1.0 / 12.0, ql.Actual365Fixed().yearFraction(eval_date, reset_date))
            swap_tenor_years = float(cms_tenor_years)
            atm_vol = interpolate_surface_vol(vol_surface_cfg, expiry_years, swap_tenor_years)
            convexity_lambda = float(cms_vol_cfg.get('convexity_lambda', 1.0))
            callability_lambda = float(cms_vol_cfg.get('callability_lambda', 0.0))

            convexity_adj = 0.5 * convexity_lambda * (atm_vol ** 2) * expiry_years
            callability_adj = 0.0

            call_dates = [parse_date(x) for x in bond_data.get('call_dates', [])]
            future_calls = [x for x in call_dates if x >= reset_date]
            if future_calls and callability_lambda > 0.0:
                next_call = min(future_calls)
                tau_call = max(0.0, ql.Actual365Fixed().yearFraction(reset_date, next_call))
                callability_adj = callability_lambda * atm_vol * math.sqrt(tau_call)

            cms_rate = cms_rate + convexity_adj - callability_adj

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
    spread = bond_data.get('float_spread')
    if spread is None:
        parsed = parse_spread_from_denomination(bond_data)
        spread = parsed if parsed is not None else 0.0
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
    calendar = get_calendar(curve_json.get('calendar', 'TARGET'))
    ql.Settings.instance().evaluationDate = evaluation_date
    day_count = get_day_count(curve_json.get('day_count', 'Actual365Fixed'))

    pillars = curve_json.get('pillars', [])
    if not pillars:
        raise ValueError('Selected curve has no pillars.')

    date_rate_pairs = []
    for p in pillars:
        period = tenor_to_period(p['tenor'])
        pillar_date = calendar.advance(evaluation_date, period, ql.Following)
        date_rate_pairs.append((pillar_date, float(p['rate'])))

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


def price_to_call_date(curve, bond_data, call_date, schedule, cms_context=None):
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
        coupon_rate = get_coupon_rate(curve, d0, d1, bond_data, eval_date, cms_context=cms_context)
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


def get_compounding_frequency_per_year(bond_data):
    structure = bond_data.get('coupon_structure', 'fixed')
    if structure == 'fixed_to_float':
        frequency_name = bond_data.get('float_coupon_frequency', bond_data.get('coupon_frequency', 'Semiannual'))
    elif structure == 'cms_resettable':
        frequency_name = bond_data.get('cms_coupon_frequency', bond_data.get('coupon_frequency', 'Semiannual'))
    else:
        frequency_name = bond_data.get('coupon_frequency', 'Semiannual')

    mapping = {
        'Annual': 1,
        'Semiannual': 2,
        'Quarterly': 4,
        'Monthly': 12,
    }
    return mapping.get(frequency_name, 2)


def solve_ytm_from_cashflows(price_amount, cashflow_amounts, cashflow_times, freq_per_year):
    if price_amount <= 0.0:
        return None
    if not cashflow_amounts or not cashflow_times:
        return None

    def pv_for_rate(rate):
        base = 1.0 + rate / freq_per_year
        if base <= 0.0:
            return float('inf')
        total = 0.0
        for amt, t in zip(cashflow_amounts, cashflow_times):
            total += amt / (base ** (freq_per_year * t))
        return total

    def objective(rate):
        return pv_for_rate(rate) - price_amount

    low = -0.95
    high = 1.0
    f_low = objective(low)
    f_high = objective(high)

    expand_count = 0
    while f_low * f_high > 0.0 and expand_count < 30:
        high += 0.5
        f_high = objective(high)
        expand_count += 1

    if f_low * f_high > 0.0:
        return None

    for _ in range(120):
        mid = 0.5 * (low + high)
        f_mid = objective(mid)
        if abs(f_mid) < 1e-10:
            return mid
        if f_low * f_mid <= 0.0:
            high = mid
            f_high = f_mid
        else:
            low = mid
            f_low = f_mid
    return 0.5 * (low + high)


def compute_model_ytm_to_maturity(bond_data, maturity_scenario, maturity_date, eval_date):
    day_count = get_day_count(bond_data['accrual_day_count'])
    freq_per_year = get_compounding_frequency_per_year(bond_data)

    amounts = []
    times = []
    for cf_date_iso, _, cf_amount, _, _ in maturity_scenario['cashflows']:
        cf_date = ql.DateParser.parseISO(cf_date_iso)
        t = day_count.yearFraction(eval_date, cf_date)
        if t <= 0.0:
            continue
        amounts.append(float(cf_amount))
        times.append(float(t))

    redemption = float(bond_data.get('redemption', bond_data.get('par', 100.0)))
    t_redemption = day_count.yearFraction(eval_date, maturity_date)
    if t_redemption > 0.0:
        amounts.append(redemption)
        times.append(float(t_redemption))

    return solve_ytm_from_cashflows(
        price_amount=float(maturity_scenario['npv']),
        cashflow_amounts=amounts,
        cashflow_times=times,
        freq_per_year=freq_per_year,
    )


def price_bond(curve, bond_data, curve_json=None, discount_curve_name=None):
    schedule, maturity_date = build_coupon_schedule(bond_data)
    eval_date = ql.Settings.instance().evaluationDate
    spread_bp = bond_data['credit_spread_bp']
    cms_context = None
    if curve_json is not None:
        cms_context = build_cms_context(curve_json, bond_data, eval_date, curve)
    hw_params = resolve_hw_parameters(curve, bond_data, curve_json)
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
        npv, redemption, cashflows = price_to_call_date(curve, bond_data, call_date, schedule, cms_context=cms_context)
        scenarios.append(
            {
                'call_date': call_date.ISO(),
                'npv': npv,
                'redemption_pv': redemption,
                'cashflows': cashflows,
            }
        )

    maturity_npv, maturity_redemption, maturity_cashflows = price_to_call_date(
        curve, bond_data, maturity_date, schedule, cms_context=cms_context
    )
    maturity_scenario = {
        'call_date': maturity_date.ISO(),
        'npv': maturity_npv,
        'redemption_pv': maturity_redemption,
        'cashflows': maturity_cashflows,
    }

    model_ytm_to_maturity = compute_model_ytm_to_maturity(
        bond_data,
        maturity_scenario,
        maturity_date,
        eval_date,
    )

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
        'discount_curve_name': discount_curve_name,
        'redemption_pv': selected['redemption_pv'],
        'spread_bp': spread_bp,
        'cashflows': selected['cashflows'],
        'npv_to_worst_call': worst['npv'],
        'npv_to_first_call': first['npv'],
        'npv_to_maturity': maturity_scenario['npv'],
        'model_ytm_to_maturity': model_ytm_to_maturity,
        'scenarios': scenarios,
        'hw_parameters': hw_params,
    }


def get_model_price(result):
    return result['selected_npv']


def get_par_amount(bond_data):
    return float(bond_data.get('par', 100.0))


def get_issue_price_pct(bond_data):
    return float(bond_data.get('issue_price', 100.0))


def amount_to_pct(value, bond_data):
    par_amount = get_par_amount(bond_data)
    return value * 100.0 / par_amount


def pct_to_amount(value_pct, bond_data):
    par_amount = get_par_amount(bond_data)
    return value_pct * par_amount / 100.0


def implied_spread_bp(curve, bond_data, market_price, curve_json=None, low_bp=-500.0, high_bp=3000.0, tol=1e-6, max_iter=120):
    def price_at(spread_bp):
        trial = dict(bond_data)
        trial['credit_spread_bp'] = spread_bp
        trial_result = price_bond(curve, trial, curve_json=curve_json)
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


def price_with_spread_bp(curve, bond_data, spread_bp, curve_json=None):
    trial = dict(bond_data)
    trial['credit_spread_bp'] = spread_bp
    trial_result = price_bond(curve, trial, curve_json=curve_json)
    return get_model_price(trial_result)


def print_bond_result(bond_data, result, curve=None, curve_json=None):
    issue_price_pct = get_issue_price_pct(bond_data)
    selected_npv_pct = amount_to_pct(result['selected_npv'], bond_data)
    worst_npv_pct = amount_to_pct(result['npv_to_worst_call'], bond_data)
    first_npv_pct = amount_to_pct(result['npv_to_first_call'], bond_data)
    maturity_npv_pct = amount_to_pct(result['npv_to_maturity'], bond_data)
    redemption_pct = amount_to_pct(result['redemption_pv'], bond_data)

    print(f"{bond_data['description']} ({bond_data['instrument_id']})")
    print(f"Valuation mode: {result['valuation_mode']}")
    print(f"Selected call date: {result['selected_call_date']}")
    if result.get('discount_curve_name'):
        print(f"Discount curve used: {result['discount_curve_name']}")
    print(f"Issue price (%): {issue_price_pct:.4f}")
    print(f"Selected price (%): {selected_npv_pct:.6f}")
    print(f"Price to_worst (%): {worst_npv_pct:.6f}")
    print(f"Price to_call (%): {first_npv_pct:.6f}")
    print(f"Price to_maturity (%): {maturity_npv_pct:.6f}")
    model_ytm_to_maturity = result.get('model_ytm_to_maturity')
    if model_ytm_to_maturity is not None:
        print(f"Model YTM (to maturity): {model_ytm_to_maturity * 100.0:.6f}%")
    print(f"Redemption PV (%): {redemption_pct:.6f}")
    print(f"Spread: {result['spread_bp']:.1f} bp")
    hw_params = result.get('hw_parameters', {})
    if hw_params:
        print(
            f"Hull-White params (a, sigma): {hw_params.get('a', 0.0):.6f}, "
            f"{hw_params.get('sigma', 0.0):.6f} [{hw_params.get('source', 'input')}]"
        )
        if hw_params.get('vol_surface_name'):
            print(f"HW calibration vol surface: {hw_params['vol_surface_name']}")
        if hw_params.get('rmse') is not None:
            print(f"HW calibration relative RMSE: {hw_params['rmse']:.8f}")
        if hw_params.get('error'):
            print(f"HW calibration fallback reason: {hw_params['error']}")
    print(f"Model - Issue (%): {selected_npv_pct - issue_price_pct:.6f}")

    selected_cashflows = result.get('cashflows', [])
    if selected_cashflows:
        print('Coupons (selected path):')
        for cashflow in selected_cashflows:
            pay_date, coupon_rate, coupon_amount, _, _ = cashflow
            print(f"  {pay_date}: coupon rate={coupon_rate * 100.0:.6f}% amount={coupon_amount:.6f}")

    if bond_data.get('coupon_structure') == 'cms_resettable':
        cms_curve_name = bond_data.get('cms_swap_curve_name', 'discount_curve_default')
        cms_vol_cfg = bond_data.get('cms_vol_adjustment', {})
        cms_vol_enabled = bool(cms_vol_cfg.get('enabled', False))
        cms_vol_name = cms_vol_cfg.get('vol_surface_name', 'none')
        print(f"CMS swap curve: {cms_curve_name}")
        print(f"CMS vol adjustment enabled: {cms_vol_enabled}")
        if cms_vol_enabled:
            print(f"CMS vol surface: {cms_vol_name}")

    if 'market_price' in bond_data and curve is not None:
        market_price = float(bond_data['market_price'])
        market_price_amount = pct_to_amount(market_price, bond_data)
        model_price = get_model_price(result)
        model_price_pct = amount_to_pct(model_price, bond_data)
        diff_pct = model_price_pct - market_price
        imp_spread = implied_spread_bp(curve, bond_data, market_price_amount, curve_json=curve_json)
        fitted_price = price_with_spread_bp(curve, bond_data, imp_spread, curve_json=curve_json)
        fitted_price_pct = amount_to_pct(fitted_price, bond_data)
        print(f"Market price (%): {market_price:.6f}")
        print(f"Model - Market (%): {diff_pct:.6f}")
        print(f"Implied spread from market price: {imp_spread:.2f} bp")
        print(f"Model price at implied spread (%): {fitted_price_pct:.6f}")
        print(f"Residual at implied spread (%): {fitted_price_pct - market_price:.8f}")

    if result.get('scenarios'):
        print('Scenarios:')
        for scenario in result['scenarios']:
            scenario_pct = amount_to_pct(scenario['npv'], bond_data)
            print(f"  {scenario['call_date']}: {scenario_pct:.6f}%")
    print()


def print_bond_skip(bond_file: Path, error):
    print(f"{bond_file.name}")
    print(f"Skipped: {error}")
    print()


def get_bond_files(base_dir: Path):
    bond_files = []
    for path in sorted(base_dir.glob('*.json')):
        if path.name == CURVE_FILE.name:
            continue
        if 'curve' in path.name.lower():
            continue
        if path.name.startswith('.'):
            continue
        bond_files.append(path)
    return bond_files


def run_all_bonds(curve_json, bond_files=None):
    if bond_files is None:
        bond_files = get_bond_files(ASSETS_DIR)

    results = []
    for bond_file in bond_files:
        try:
            bond_data = load_json(bond_file)
            evaluation_date = parse_date(bond_data['evaluation_date'])
            discount_curve_cfg = select_discount_curve_config(curve_json, bond_data)
            curve = build_discount_curve(discount_curve_cfg, evaluation_date)
            result = price_bond(
                curve,
                bond_data,
                curve_json=curve_json,
                discount_curve_name=discount_curve_cfg.get('curve_name'),
            )
        except Exception as exc:
            print_bond_skip(bond_file, exc)
            continue

        model_price = get_model_price(result)
        model_price_pct = amount_to_pct(model_price, bond_data)
        market_price = bond_data.get('market_price')
        if market_price is not None:
            diff = model_price_pct - float(market_price)
        else:
            diff = None

        results.append(
            {
                'bond_file': bond_file.name,
                'instrument_id': bond_data['instrument_id'],
                'description': bond_data.get('description', bond_data['instrument_id']),
                'valuation_mode': result['valuation_mode'],
                'selected_call_date': result['selected_call_date'],
                'model_price': model_price_pct,
                'market_price': market_price,
                'model_minus_market': diff,
                'implied_spread_bp': (
                    implied_spread_bp(curve, bond_data, pct_to_amount(float(market_price), bond_data), curve_json=curve_json)
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
        help='Path to swap curve JSON input file (single curve or catalog)',
    )
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    curve_json = load_json(Path(args.curve_file))

    if args.all_bonds:
        bond_files = get_bond_files(ASSETS_DIR)
        for bond_file in bond_files:
            try:
                bond_data = load_json(bond_file)
                evaluation_date = parse_date(bond_data['evaluation_date'])
                discount_curve_cfg = select_discount_curve_config(curve_json, bond_data)
                curve = build_discount_curve(discount_curve_cfg, evaluation_date)
                result = price_bond(
                    curve,
                    bond_data,
                    curve_json=curve_json,
                    discount_curve_name=discount_curve_cfg.get('curve_name'),
                )
            except Exception as exc:
                print_bond_skip(bond_file, exc)
                continue
            print_bond_result(bond_data, result, curve, curve_json=curve_json)
        raise SystemExit(0)

    bond_data = load_json(Path(args.bond_file))
    evaluation_date = parse_date(bond_data['evaluation_date'])
    discount_curve_cfg = select_discount_curve_config(curve_json, bond_data)
    curve = build_discount_curve(discount_curve_cfg, evaluation_date)
    result = price_bond(
        curve,
        bond_data,
        curve_json=curve_json,
        discount_curve_name=discount_curve_cfg.get('curve_name'),
    )
    print_bond_result(bond_data, result, curve, curve_json=curve_json)
    pdf_path = pdf_report.create_pdf_report(
        model_name='hullwhite',
        instrument_id=bond_data.get('instrument_id', 'unknown'),
        input_payload=bond_data,
        output_payload=result,
    )
    print(f'PDF report: {pdf_path}')
