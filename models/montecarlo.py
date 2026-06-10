import argparse
import math
from pathlib import Path

import numpy as np
import QuantLib as ql

from models.hullwhite import (
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
)

BASE_DIR = Path(__file__).resolve().parent


def build_spreaded_curve(base_curve, issuer_spread_bp):
    spread_decimal = issuer_spread_bp / 10000.0
    spread_handle = ql.QuoteHandle(ql.SimpleQuote(spread_decimal))
    base_handle = ql.YieldTermStructureHandle(base_curve)
    spreaded_curve = ql.ZeroSpreadedTermStructure(base_handle, spread_handle)
    return ql.YieldTermStructureHandle(spreaded_curve)


def parse_callability_type(name):
    name = name.lower()
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
    call_price = float(bond_data.get('call_price', bond_data.get('par', 100.0)))
    call_type = parse_callability_type(bond_data.get('callable_type', 'bermudan'))
    schedule = []
    for raw_date in call_dates:
        call_date = parse_date(raw_date)
        schedule.append((call_date, call_price, call_type))
    return schedule


def build_bond_schedule(bond_data):
    issue_date = parse_date(bond_data['issue_date'])
    if 'maturity_date' in bond_data:
        maturity_date = parse_date(bond_data['maturity_date'])
    elif 'end_date' in bond_data:
        maturity_date = parse_date(bond_data['end_date'])
    else:
        raise ValueError('Bond JSON must include maturity_date or end_date')

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
    return schedule, issue_date, maturity_date, business_day_convention


def build_cashflows(bond_data, projection_curve, eval_date):
    schedule, issue_date, maturity_date, business_day_convention = build_bond_schedule(bond_data)
    face_amount = float(bond_data.get('par', 100.0))
    redemption = float(bond_data.get('redemption', face_amount))
    accrual_day_count = get_day_count(bond_data.get('accrual_day_count', 'Actual365Fixed'))

    cashflows = []
    for i in range(1, len(schedule)):
        d0 = schedule[i - 1]
        d1 = schedule[i]
        pay_date = d1
        coupon_rate = float(get_coupon_rate(projection_curve, d0, d1, bond_data, eval_date))
        accrual = accrual_day_count.yearFraction(d0, d1)
        amount = face_amount * coupon_rate * accrual
        if i == len(schedule) - 1:
            amount += redemption
        cashflows.append({
            'start_date': d0,
            'end_date': d1,
            'pay_date': pay_date,
            'coupon_rate': coupon_rate,
            'accrual': accrual,
            'amount': amount,
        })
    return cashflows, issue_date, maturity_date, business_day_convention, accrual_day_count


def ql_date_to_time(dc, eval_date, date):
    return max(0.0, dc.yearFraction(eval_date, date))


def discount_factor_path(short_rates, times):
    integral = np.trapz(short_rates, times)
    return math.exp(-integral)


def hw_B(a, t, T):
    if abs(a) < 1e-12:
        return T - t
    return (1.0 - math.exp(-a * (T - t))) / a


def hw_var_integral_B2(a, sigma, t, T):
    dt = T - t
    if dt <= 0:
        return 0.0
    if abs(a) < 1e-12:
        return sigma * sigma * dt**3 / 3.0
    exp1 = math.exp(-a * dt)
    exp2 = math.exp(-2.0 * a * dt)
    return (sigma * sigma / (a * a)) * (dt - 2.0 * (1.0 - exp1) / a + (1.0 - exp2) / (2.0 * a))


def discount_bond_given_r(curve_handle, a, sigma, t, T, r_t):
    if T <= t:
        return 1.0
    P0T = curve_handle.discount(T)
    P0t = curve_handle.discount(t)
    f0t = curve_handle.forwardRate(t, t, ql.Continuous, ql.NoFrequency).rate()
    B = hw_B(a, t, T)
    var_term = hw_var_integral_B2(a, sigma, t, T)
    A = math.exp(math.log(P0T / P0t) + B * f0t - 0.5 * var_term)
    return A * math.exp(-B * r_t)


def generate_hw_paths(curve_handle, a, sigma, maturity_time, time_steps, num_paths, seed=42):
    if time_steps <= 0:
        raise ValueError('time_steps must be positive')
    process = ql.HullWhiteProcess(curve_handle, a, sigma)
    rng = ql.GaussianRandomSequenceGenerator(
        ql.UniformRandomSequenceGenerator(time_steps, ql.UniformRandomGenerator(seed))
    )
    seq = ql.GaussianPathGenerator(process, maturity_time, time_steps, rng, False)

    times = None
    paths = np.zeros((num_paths, time_steps + 1))
    for i in range(num_paths):
        sample = seq.next()
        path = sample.value()
        if times is None:
            times = np.array([path.time(j) for j in range(len(path))], dtype=float)
        paths[i, :] = np.array([path[j] for j in range(len(path))], dtype=float)
    return times, paths


def price_noncallable_from_path(cashflows, eval_date, day_count, curve_handle, a, sigma, path_times, path_rates):
    pv = 0.0
    for cf in cashflows:
        t_pay = ql_date_to_time(day_count, eval_date, cf['pay_date'])
        idx = np.searchsorted(path_times, t_pay, side='right') - 1
        idx = max(0, min(idx, len(path_times) - 1))
        times_slice = path_times[: idx + 1]
        rates_slice = path_rates[: idx + 1]
        if times_slice[-1] < t_pay:
            times_slice = np.append(times_slice, t_pay)
            rates_slice = np.append(rates_slice, path_rates[idx])
        df = discount_factor_path(rates_slice, times_slice)
        pv += cf['amount'] * df
    return pv


def price_callable_from_path_lsm(cashflows, call_schedule, eval_date, day_count, curve_handle, a, sigma, path_times, path_rates):
    if not call_schedule:
        return price_noncallable_from_path(cashflows, eval_date, day_count, curve_handle, a, sigma, path_times, path_rates)

    payment_times = np.array([ql_date_to_time(day_count, eval_date, cf['pay_date']) for cf in cashflows], dtype=float)
    payment_amounts = np.array([cf['amount'] for cf in cashflows], dtype=float)
    call_times = np.array([ql_date_to_time(day_count, eval_date, d) for d, _, _ in call_schedule], dtype=float)
    call_prices = np.array([p for _, p, _ in call_schedule], dtype=float)

    num_paths = path_rates.shape[0] if path_rates.ndim == 2 else 1
    if num_paths == 1:
        path_rates = path_rates.reshape(1, -1)

    values = np.zeros(num_paths, dtype=float)
    alive = np.ones(num_paths, dtype=bool)

    for call_idx in range(len(call_times) - 1, -1, -1):
        t_call = call_times[call_idx]
        call_price = call_prices[call_idx]
        idx = np.searchsorted(path_times, t_call, side='right') - 1
        idx = max(0, min(idx, len(path_times) - 1))

        cf_mask = payment_times > t_call
        future_times = payment_times[cf_mask]
        future_amounts = payment_amounts[cf_mask]
        if future_times.size == 0:
            continue

        continuation = np.zeros(num_paths, dtype=float)
        state_r = np.zeros(num_paths, dtype=float)
        for p in range(num_paths):
            r_t = path_rates[p, idx]
            state_r[p] = r_t
            cont = 0.0
            for T, amt in zip(future_times, future_amounts):
                cont += amt * discount_bond_given_r(curve_handle, a, sigma, t_call, T, r_t)
            continuation[p] = cont

        itm = continuation > call_price
        eligible = alive & itm
        if np.count_nonzero(eligible) >= 3:
            x = state_r[eligible]
            y = continuation[eligible]
            X = np.column_stack([np.ones_like(x), x, x * x])
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
            continuation_est = beta[0] + beta[1] * state_r + beta[2] * state_r * state_r
        else:
            continuation_est = continuation

        exercise = alive & (call_price < continuation_est)
        values[exercise] = call_price
        alive[exercise] = False

    for p in range(num_paths):
        if alive[p]:
            values[p] = price_noncallable_from_path(cashflows, eval_date, day_count, curve_handle, a, sigma, path_times, path_rates[p])
        else:
            first_call_time = None
            for t_call, _, _ in call_schedule:
                t = ql_date_to_time(day_count, eval_date, t_call)
                if values[p] > 0:
                    first_call_time = t
                    break
            if first_call_time is not None:
                idx = np.searchsorted(path_times, first_call_time, side='right') - 1
                idx = max(0, min(idx, len(path_times) - 1))
                times_slice = path_times[: idx + 1]
                rates_slice = path_rates[p, : idx + 1]
                if times_slice[-1] < first_call_time:
                    times_slice = np.append(times_slice, first_call_time)
                    rates_slice = np.append(rates_slice, path_rates[p, idx])
                values[p] *= discount_factor_path(rates_slice, times_slice)

    return float(values.mean())


def price_bond_monte_carlo(curve_json, bond_data, issuer_spread_bp=None):
    evaluation_date = parse_date(bond_data['evaluation_date'])
    ql.Settings.instance().evaluationDate = evaluation_date

    base_curve = build_discount_curve(curve_json, evaluation_date)
    if issuer_spread_bp is None:
        issuer_spread_bp = float(bond_data.get('issuer_spread_bp', bond_data.get('credit_spread_bp', 0.0)))
    spreaded_curve_handle = build_spreaded_curve(base_curve, issuer_spread_bp)

    a = float(bond_data.get('hw_a', 0.03))
    sigma = float(bond_data.get('hw_sigma', 0.01))
    time_steps = int(bond_data.get('mc_time_steps', bond_data.get('tree_time_steps', 360)))
    num_paths = int(bond_data.get('mc_num_paths', 5000))
    seed = int(bond_data.get('mc_seed', 42))

    cashflows, issue_date, maturity_date, _, accrual_day_count = build_cashflows(bond_data, base_curve, evaluation_date)
    call_schedule = build_callability_schedule(bond_data)
    maturity_time = ql_date_to_time(accrual_day_count, evaluation_date, maturity_date)

    path_times, path_rates = generate_hw_paths(spreaded_curve_handle, a, sigma, maturity_time, time_steps, num_paths, seed)

    has_calls = len(call_schedule) > 0
    if has_calls:
        npv = price_callable_from_path_lsm(
            cashflows, call_schedule, evaluation_date, accrual_day_count,
            spreaded_curve_handle, a, sigma, path_times, path_rates,
        )
    else:
        pvs = [
            price_noncallable_from_path(
                cashflows, evaluation_date, accrual_day_count,
                spreaded_curve_handle, a, sigma, path_times, path_rates[i],
            )
            for i in range(num_paths)
        ]
        npv = float(np.mean(pvs))

    settlement_days = int(bond_data.get('settlement_days', 2))
    settlement_date = get_calendar(bond_data['calendar']).advance(evaluation_date, settlement_days, ql.Days)
    accrued_amount = 0.0
    for cf in cashflows:
        if cf['start_date'] <= settlement_date < cf['end_date']:
            elapsed = accrual_day_count.yearFraction(cf['start_date'], settlement_date)
            accrued_amount = float(bond_data.get('par', 100.0)) * cf['coupon_rate'] * elapsed
            break

    clean_price = npv - accrued_amount
    dirty_price = npv

    return {
        'npv': npv,
        'clean_price': clean_price,
        'dirty_price': dirty_price,
        'accrued_amount': accrued_amount,
        'issuer_spread_bp': issuer_spread_bp,
        'model_a': a,
        'model_sigma': sigma,
        'mc_time_steps': time_steps,
        'mc_num_paths': num_paths,
        'mc_seed': seed,
        'evaluation_date': evaluation_date.ISO(),
    }


def print_mc_result(bond_data, result):
    print(f"{bond_data['description']} ({bond_data['instrument_id']})")
    print(f"Evaluation date: {result['evaluation_date']}")
    print(f"Issuer spread: {result['issuer_spread_bp']:.2f} bp")
    print(f"Hull-White a: {result['model_a']:.4f}")
    print(f"Hull-White sigma: {result['model_sigma']:.4f}")
    print(f"Monte Carlo time steps: {result['mc_time_steps']}")
    print(f"Monte Carlo paths: {result['mc_num_paths']}")
    print(f"Monte Carlo seed: {result['mc_seed']}")
    print(f"NPV: {result['npv']:.4f}")
    print(f"Clean price: {result['clean_price']:.4f}")
    print(f"Dirty price: {result['dirty_price']:.4f}")
    print(f"Accrued amount: {result['accrued_amount']:.4f}")
    if 'market_price' in bond_data:
        market_price = float(bond_data['market_price'])
        diff = result['clean_price'] - market_price
        print(f"Market price: {market_price:.4f}")
        print(f"Model - Market: {diff:.4f}")
    print()


def print_mc_skip(bond_data, error):
    print(f"{bond_data.get('description', bond_data['instrument_id'])} ({bond_data['instrument_id']})")
    print(f"Skipped: {error}")
    print()


def parse_args():
    parser = argparse.ArgumentParser(description='Price bonds with a Hull-White Monte Carlo model.')
    parser.add_argument('--all-bonds', action='store_true', help='Price all known bond JSON files in the project folder')
    parser.add_argument('--bond-file', default=str(BOND_FILE), help='Path to bond JSON input file')
    parser.add_argument('--curve-file', default=str(CURVE_FILE), help='Path to curve JSON input file')
    parser.add_argument('--issuer-spread-bp', type=float, default=None, help='Override issuer spread in basis points')
    parser.add_argument('--time-steps', type=int, default=None, help='Override Monte Carlo time steps')
    parser.add_argument('--num-paths', type=int, default=None, help='Override Monte Carlo number of paths')
    parser.add_argument('--seed', type=int, default=None, help='Override Monte Carlo random seed')
    return parser.parse_args()


def apply_overrides(bond_data, args):
    bond_data = dict(bond_data)
    if args.time_steps is not None:
        bond_data['mc_time_steps'] = args.time_steps
    if args.num_paths is not None:
        bond_data['mc_num_paths'] = args.num_paths
    if args.seed is not None:
        bond_data['mc_seed'] = args.seed
    return bond_data


def main():
    args = parse_args()
    curve_json = load_json(Path(args.curve_file))

    if args.all_bonds:
        for bond_file in get_bond_files(BASE_DIR):
            bond_data = apply_overrides(load_json(bond_file), args)
            try:
                result = price_bond_monte_carlo(curve_json, bond_data, issuer_spread_bp=args.issuer_spread_bp)
            except Exception as exc:
                print_mc_skip(bond_data, exc)
                continue
            print_mc_result(bond_data, result)
        return

    bond_data = apply_overrides(load_json(Path(args.bond_file)), args)
    result = price_bond_monte_carlo(curve_json, bond_data, issuer_spread_bp=args.issuer_spread_bp)
    print_mc_result(bond_data, result)


if __name__ == '__main__':
    main()
