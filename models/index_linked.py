import argparse
from pathlib import Path

import QuantLib as ql

try:
    from models import spire
except ModuleNotFoundError:
    import spire

try:
    from models import pdf_report
except ModuleNotFoundError:
    import pdf_report


BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = spire.ASSETS_DIR
CURVES_DIR = spire.CURVES_DIR
CURVE_FILE = CURVES_DIR / 'swap_curves.json'
BOND_FILE = ASSETS_DIR / 'XS0316010023.json'

REQUIRED_CONTRACT_FIELDS = [
    'underlying_name',
    'reference_index_name',
    'initial_reference_level',
    'current_reference_level',
    'pricing_formula_type',
    'coupon_formula_type',
    'redemption_formula_type',
    'principal_protection_pct',
    'observation_dates',
    'issuer_call_rights',
]


def assess_contract_completeness(note_data):
    terms = dict(note_data.get('index_linked_terms', {}))
    missing = []
    for field in REQUIRED_CONTRACT_FIELDS:
        value = terms.get(field)
        if value is None:
            missing.append(field)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(field)
            continue
        if isinstance(value, list) and not value:
            missing.append(field)
            continue

    declared_missing = note_data.get('missing_contractual_terms', [])
    for field in declared_missing:
        if field not in missing:
            missing.append(field)

    return {
        'is_complete': not missing,
        'missing_fields': missing,
        'terms': terms,
    }


def get_index_assumption(note_data):
    assumption = dict(note_data.get('index_linked_assumption', {}))
    if not assumption:
        assumption = dict(note_data.get('collateral', {}).get('inflation_assumption', {}))
    return {
        'index_ratio_at_eval': float(assumption.get('index_ratio_at_eval', 1.0)),
        'annual_inflation_rate': float(
            assumption.get('annual_index_growth_rate', assumption.get('annual_inflation_rate', 0.0))
        ),
        'coupon_multiplier': float(assumption.get('coupon_multiplier', note_data.get('fixed_coupon_rate', 0.0))),
    }


def price_note(note_data, curve, curve_day_count):
    eval_date = ql.Settings.instance().evaluationDate
    note_day_count = spire.get_day_count(note_data.get('accrual_day_count', 'Actual365Fixed'))
    coupon_structure = note_data.get('coupon_structure', 'index_linked')
    if coupon_structure != 'index_linked':
        raise ValueError(
            'index_linked pricer supports coupon_structure="index_linked" only. '
            f'Received coupon_structure="{coupon_structure}" for {note_data.get("instrument_id", "unknown")}. '
        )

    notional = float(note_data.get('note_notional', 100000000.0))
    issuer_spread_bp = float(note_data.get('credit_spread_bp', 0.0))
    index_assumption = get_index_assumption(note_data)
    dates = spire.build_note_dates(note_data)

    pv_coupons = 0.0
    pv_redemption = 0.0
    cashflows = []

    for i in range(1, len(dates)):
        d0 = dates[i - 1]
        d1 = dates[i]
        if d1 <= eval_date:
            continue

        accrual = note_day_count.yearFraction(d0, d1)
        index_ratio = spire.inflation_factor(eval_date, d1, index_assumption)
        coupon_cf = notional * index_assumption['coupon_multiplier'] * accrual * index_ratio
        df = spire.discount_factor_with_issuer_spread(curve, curve_day_count, eval_date, d1, issuer_spread_bp)
        pv = coupon_cf * df
        pv_coupons += pv
        cashflows.append({'date': d1.ISO(), 'type': 'coupon', 'amount': coupon_cf, 'df': df, 'pv': pv})

    maturity_date = dates[-1]
    if maturity_date > eval_date:
        index_ratio_mat = spire.inflation_factor(eval_date, maturity_date, index_assumption)
        redemption_cf = notional * index_ratio_mat
        df_maturity = spire.discount_factor_with_issuer_spread(
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
        'index_assumption': index_assumption,
    }


def price_index_linked_note(note_data, curve_json):
    evaluation_date = spire.parse_date(note_data['evaluation_date'])
    note_curve_cfg, note_curve_name = spire.select_note_curve(note_data, curve_json)
    collateral_curve_cfg, collateral_curve_name = spire.select_collateral_curve(note_data, curve_json)
    note_curve, note_curve_day_count = spire.build_discount_curve(note_curve_cfg, evaluation_date)
    collateral_curve, collateral_curve_day_count = spire.build_discount_curve(collateral_curve_cfg, evaluation_date)
    note_notional = float(note_data.get('note_notional', 100000000.0))
    issue_price = float(note_data.get('issue_price', 100.0))

    note_leg = price_note(note_data, note_curve, note_curve_day_count)
    collateral_leg = spire.model_collateral_pv(note_data['collateral'], collateral_curve, collateral_curve_day_count)
    adjustments = spire.compute_valuation_adjustments(note_data, note_curve, note_curve_day_count)
    contract_completeness = assess_contract_completeness(note_data)

    swap_cfg = note_data.get('swap', {})
    swap_mode = swap_cfg.get('mode', 'calibration_residual')
    if swap_mode == 'calibration_residual':
        pv_swap = note_leg['pv_note'] - collateral_leg['pv_collateral'] + adjustments['pv_total_adjustments']
    else:
        raise ValueError(f'Unsupported swap mode: {swap_mode}')

    lhs = note_leg['pv_note']
    rhs = collateral_leg['pv_collateral'] + pv_swap - adjustments['pv_total_adjustments']
    scale_to_pct = 100.0 / note_notional

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
        'contract_completeness': contract_completeness,
        'price_pct': {
            'pv_note': lhs * scale_to_pct,
            'pv_collateral': collateral_leg['pv_collateral'] * scale_to_pct,
            'pv_collateral_model': collateral_leg['pv_collateral_model'] * scale_to_pct,
            'pv_swap': pv_swap * scale_to_pct,
            'pv_fees': adjustments['pv_fees'] * scale_to_pct,
            'pv_funding': adjustments['pv_funding'] * scale_to_pct,
            'pv_csa': adjustments['pv_csa'] * scale_to_pct,
            'pv_residual_basis': adjustments['pv_residual_basis'] * scale_to_pct,
            'pv_total_adjustments': adjustments['pv_total_adjustments'] * scale_to_pct,
            'identity_lhs_pv_note': lhs * scale_to_pct,
            'identity_rhs_reconstructed': rhs * scale_to_pct,
            'identity_error': (lhs - rhs) * scale_to_pct,
        },
        'note_leg': note_leg,
        'collateral_leg': collateral_leg,
        'swap_mode': swap_mode,
    }


def print_report(note_data, result):
    pct = result['price_pct']
    assumption = result['note_leg']['index_assumption']
    completeness = result['contract_completeness']
    print(f"{note_data['description']} ({note_data['instrument_id']})")
    print(f"Evaluation date: {result['evaluation_date']}")
    print(f"Note discount curve: {result['note_discount_curve_name']}")
    print(f"Collateral discount curve: {result['collateral_discount_curve_name']}")
    print(f"Issue price (%): {result['issue_price']:.4f}")
    print(f"Index ratio at eval: {assumption['index_ratio_at_eval']:.6f}")
    print(f"Annual index growth assumption: {assumption['annual_inflation_rate']:.6f}")
    print(f"Index coupon multiplier: {assumption['coupon_multiplier']:.6f}")
    print(f"Contract terms complete: {completeness['is_complete']}")
    if completeness['missing_fields']:
        print(f"Missing contractual fields: {', '.join(completeness['missing_fields'])}")
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
    parser = argparse.ArgumentParser(description='Price index-linked channel notes.')
    parser.add_argument('--bond-file', default=str(BOND_FILE), help='Path to index-linked bond JSON')
    parser.add_argument('--curve-file', default=str(CURVE_FILE), help='Path to swap curve JSON (single curve or catalog)')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    note_data = spire.load_json(Path(args.bond_file))
    curve_json = spire.load_json(Path(args.curve_file))
    result = price_index_linked_note(note_data, curve_json)
    print_report(note_data, result)
    pdf_path = pdf_report.create_pdf_report(
        model_name='index_linked',
        instrument_id=note_data.get('instrument_id', 'unknown'),
        input_payload=note_data,
        output_payload=result,
    )
    print(f'PDF report: {pdf_path}')
