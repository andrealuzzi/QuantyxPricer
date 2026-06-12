from pathlib import Path
import math
from typing import List, Dict, Any

import QuantLib as ql

from . import hullwhite


def find_cds_curve_config(curve_json, bond_data):
    # Prefer explicit name
    name = bond_data.get('reference_cds_curve_name') or bond_data.get('reference_cds_curve')
    if name:
        return hullwhite.select_named_curve_config(curve_json, name)

    # Try matching by reference_obligation_isin or reference_entity
    catalog = hullwhite.normalize_curve_catalog(curve_json)
    if catalog is None:
        return None

    ref_isin = bond_data.get('reference_obligation_isin')
    ref_entity = bond_data.get('reference_entity')
    for cfg_name, cfg in catalog.items():
        if cfg.get('quote_type') != 'cds_spreads':
            continue
        refs = cfg.get('reference_obligations', []) or cfg.get('reference_entities', [])
        if ref_isin and ref_isin in refs:
            return cfg
        if ref_entity and cfg.get('reference_entity') and ref_entity == cfg.get('reference_entity'):
            return cfg
    return None


def build_piecewise_hazard(cds_cfg: Dict[str, Any], recovery_rate: float) -> List[Dict[str, float]]:
    # Return list of segments with 't' (years) and 'lambda' (hazard per year)
    pillars = cds_cfg.get('pillars', [])
    segments = []
    for p in pillars:
        tenor = p.get('tenor')
        spread_bp = p.get('spread_bp') if 'spread_bp' in p else p.get('spread')
        if tenor is None or spread_bp is None:
            continue
        t = hullwhite.tenor_to_years(tenor)
        s = float(spread_bp) / 10000.0
        if recovery_rate is None:
            raise ValueError('CDS curve requires recovery_rate in bond JSON for hazard conversion')
        # Approximate hazard: s ≈ (1 - R) * lambda  => lambda = s / (1 - R)
        lam = s / max(1e-9, (1.0 - float(recovery_rate)))
        segments.append({'t': float(t), 'lambda': float(lam)})
    segments.sort(key=lambda x: x['t'])
    return segments


def survival_at(t_years: float, segments: List[Dict[str, float]]) -> float:
    if t_years <= 0:
        return 1.0
    cum = 0.0
    prev_t = 0.0
    for seg in segments:
        t_seg = seg['t']
        lam = seg['lambda']
        if t_years <= t_seg:
            cum += lam * (t_years - prev_t)
            return math.exp(-cum)
        cum += lam * (t_seg - prev_t)
        prev_t = t_seg
    # beyond last pillar assume last hazard
    if segments:
        lam = segments[-1]['lambda']
        cum += lam * (t_years - prev_t)
    return math.exp(-cum)


def price_cln(curve, bond_data: Dict[str, Any], curve_json=None):
    # Build schedule and basic settings from hullwhite helpers
    schedule, maturity_date = hullwhite.build_coupon_schedule(bond_data)
    eval_date = ql.Settings.instance().evaluationDate
    day_count = hullwhite.get_day_count(bond_data.get('accrual_day_count', 'Actual365Fixed'))
    par = float(bond_data.get('par', 100.0))
    recovery = float(bond_data.get('recovery_rate', 0.4))

    # Load CDS curve config and build hazard segments
    cds_cfg = None
    if curve_json is not None:
        cds_cfg = find_cds_curve_config(curve_json, bond_data)
    if cds_cfg is None:
        raise ValueError('No CDS curve configuration found for CLN; set reference_cds_curve_name or add CDS curve to curves catalog')

    segments = build_piecewise_hazard(cds_cfg, recovery)

    # Helper to get survival given a QuantLib Date
    def S_at(d: ql.Date):
        t = day_count.yearFraction(eval_date, d)
        return survival_at(max(0.0, float(t)), segments)

    pv_coupons = 0.0
    pv_redemption = 0.0
    pv_recovery = 0.0

    prev_date = eval_date
    df_prev = curve.discount(prev_date)
    S_prev = S_at(prev_date)

    cashflows = []

    for i in range(1, len(schedule)):
        pay_date = schedule[i]
        accrual = day_count.yearFraction(schedule[i - 1], pay_date)
        coupon_rate = bond_data.get('fixed_coupon_rate', 0.0)
        coupon = par * float(coupon_rate) * accrual

        # Skip payments that already occurred; keep prev_date anchored at eval_date
        if pay_date <= eval_date:
            continue

        df = curve.discount(pay_date)
        S = S_at(pay_date)

        pv_coupon = coupon * df * S
        pv_coupons += pv_coupon

        # redemption at maturity: accept last scheduled payment or any pay_date >= maturity_date
        if pay_date == maturity_date or i == len(schedule) - 1 or pay_date >= maturity_date:
            pv_redemption = par * df * S

        # default probability in interval [prev_date, pay_date]
        S_curr = S
        dp = max(0.0, S_prev - S_curr)
        # approximate discount in interval by average
        df_avg = 0.5 * (df_prev + df)
        pv_recovery_piece = recovery * par * df_avg * dp
        pv_recovery += pv_recovery_piece

        cashflows.append({
            'pay_date': pay_date.ISO(),
            'accrual': accrual,
            'coupon': coupon,
            'df': df,
            'survival': S,
            'pv_coupon': pv_coupon,
            'default_prob_interval': dp,
            'pv_recovery_piece': pv_recovery_piece,
        })

        prev_date = pay_date
        df_prev = df
        S_prev = S_curr

    npv = pv_coupons + pv_redemption + pv_recovery

    result = {
        'selected_npv': npv,
        'pv_coupons': pv_coupons,
        'pv_redemption': pv_redemption,
        'pv_recovery': pv_recovery,
        'valuation_mode': 'to_maturity',
        'discount_curve_name': None,
        'redemption_pv': pv_redemption,
        'spread_bp': bond_data.get('credit_spread_bp'),
        'cashflows': cashflows,
        'scenarios': [],
        'cds_curve_used': cds_cfg.get('curve_name') if isinstance(cds_cfg, dict) and cds_cfg.get('curve_name') else None,
        'hazard_segments': segments,
    }
    # Compute YTMs: promised (ignore default/recovery) and expected (include survival & recovery)
    try:
        freq = hullwhite.get_compounding_frequency_per_year(bond_data)
        eval_date = ql.Settings.instance().evaluationDate
        # promised cashflows: coupon amounts and redemption at maturity
        promised_amounts = []
        promised_times = []
        for i in range(1, len(schedule)):
            pd = schedule[i]
            accrual = day_count.yearFraction(schedule[i - 1], pd)
            coupon_amt = par * float(bond_data.get('fixed_coupon_rate', 0.0)) * accrual
            if pd > eval_date:
                t = day_count.yearFraction(eval_date, pd)
                promised_amounts.append(float(coupon_amt))
                promised_times.append(float(t))
        # redemption time
        t_red = day_count.yearFraction(eval_date, maturity_date)
        if t_red > 0.0:
            promised_amounts.append(float(par))
            promised_times.append(float(t_red))

        # expected cashflows: coupon * survival + recovery*par*dp (allocated to period end)
        expected_amounts = []
        expected_times = []
        S_prev = S_at(eval_date)
        for cf in cashflows:
            pd = ql.DateParser.parseISO(cf['pay_date'])
            t = day_count.yearFraction(eval_date, pd)
            # expected coupon nominal at period end
            exp_coupon = cf['coupon'] * cf['survival']
            # recovery piece allocated at pay date (nominal)
            exp_rec = float(recovery) * par * float(cf['default_prob_interval'])
            expected_amounts.append(float(exp_coupon + exp_rec))
            expected_times.append(float(t))
        # include expected redemption at maturity (survival-weighted)
        if t_red > 0.0:
            S_mat = survival_at(t_red, segments)
            expected_amounts.append(float(par * S_mat))
            expected_times.append(float(t_red))

        ytm_promised = hullwhite.solve_ytm_from_cashflows(float(npv), promised_amounts, promised_times, freq)
        ytm_expected = hullwhite.solve_ytm_from_cashflows(float(npv), expected_amounts, expected_times, freq)
    except Exception:
        ytm_promised = None
        ytm_expected = None

    result['ytm_promised'] = ytm_promised
    result['ytm_expected'] = ytm_expected
    # write canonical 'ytm' as requested (expected)
    result['ytm'] = ytm_expected
    return result


def print_cln_result(bond_data, result):
    amt_to_pct = lambda v: v * 100.0 / float(bond_data.get('par', 100.0))
    print(f"{bond_data.get('description','CLN')} ({bond_data.get('instrument_id')})")
    print(f"Model: reduced-form CLN")
    print(f"Selected price (%): {amt_to_pct(result['selected_npv']):.6f}")
    print(f"PV coupons: {amt_to_pct(result.get('pv_coupons',0.0)):.6f} %")
    print(f"PV redemption: {amt_to_pct(result.get('pv_redemption',0.0)):.6f} %")
    print(f"PV expected recovery: {amt_to_pct(result.get('pv_recovery',0.0)):.6f} %")
    if result.get('cds_curve_used'):
        print(f"CDS curve used: {result['cds_curve_used']}")
    if result.get('hazard_segments'):
        print('Hazard segments (t years -> lambda):')
        for seg in result['hazard_segments']:
            print(f"  t={seg['t']:.3f} -> lambda={seg['lambda']:.6f}")

    cashflows = result.get('cashflows', [])
    if cashflows:
        print('Coupons and survival (selected path):')
        for cf in cashflows:
            print(
                f"  {cf['pay_date']}: accr={cf['accrual']:.6f}, coupon={cf['coupon']:.6f}, df={cf['df']:.6f}, S={cf['survival']:.6f}, pv_coupon={cf['pv_coupon']:.6f}, dp={cf['default_prob_interval']:.6f}, pv_rec={cf['pv_recovery_piece']:.6f}"
            )
    print()
