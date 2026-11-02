"""AVID portfolio performance from the bot's own log (no network).

Each bot cycle logs balances and order-book prices. From consecutive snapshots:

- time-weighted return: the market return of the holdings at the previous snapshot,
  r_t = (balances_{t-1} @ prices_t) / (balances_{t-1} @ prices_{t-1}) - 1.
  Deposits and withdrawals therefore do not count as profit or loss, and bot trades
  (value-neutral apart from fees) only change the weights for the next step.
- benchmarks over the same steps: HODL (the first snapshot's balances, never rebalanced)
  and 100% BTC.
- drawdown, annualised volatility and Sharpe from daily-resampled returns (only with
  >= 7 days of history; otherwise reported as null instead of a misleading number).
- detected external flows: snapshots where the value changed by more than the price
  move explains (likely a deposit/withdrawal or a manual trade).

Honesty rules (report 10.6):
- `basis` says what the numbers are: 'live' (every snapshot came from a LIVE cycle), 'simulation'
  (none did: the return is the market return of the ACTUAL holdings - a dry run never trades, so there
  is no simulated profit to report) or 'mixed'. Only 'live' sets realized=true.
- fees: the balances already include them; `fees_usdt` additionally sums the fees of the recorded
  executions (buy fees, charged in the asset, are converted at the execution price).
- `data_quality` counts executions known only from order totals (AGGREGATE_ONLY) and orders whose
  accounting is INCOMPLETE; when either is non-zero the realized figures are not exact.
"""
from __future__ import annotations
import datetime as dt
import math


def _ts(s):
    try:
        return dt.datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


def _mids(books):
    out = {}
    for a, ba in (books or {}).items():
        try:
            b, k = float(ba[0]), float(ba[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if b > 0 and k > 0:
            out[a] = (b + k) / 2
    return out


def _value(bal, mids):
    v = float(bal.get('usdt', 0.0) or 0.0)
    for a, q in bal.items():
        if a == 'usdt':
            continue
        if a not in mids:
            if q:
                return None
            continue
        v += float(q or 0.0) * mids[a]
    return v


def snapshots(events):
    out = []
    for e in events:
        t = _ts(e.get('at'))
        bal, books = e.get('balances'), e.get('books')
        if t is None or not isinstance(bal, dict) or not isinstance(books, dict):
            continue
        mids = _mids(books)
        v = _value(bal, mids)
        if v is None or v <= 0:
            continue
        out.append({'t': t, 'bal': {k: float(x or 0) for k, x in bal.items()}, 'mids': mids, 'value': v})
    out.sort(key=lambda s: s['t'])
    return out


def _max_dd(eq):
    peak, dd = -math.inf, 0.0
    for x in eq:
        peak = max(peak, x)
        if peak > 0:
            dd = min(dd, x / peak - 1)
    return dd


def _daily(ts, eq):
    by_day = {}
    for t, x in zip(ts, eq):
        by_day[dt.datetime.fromtimestamp(t, dt.timezone.utc).date()] = x
    days = sorted(by_day)
    vals = [by_day[d] for d in days]
    return [b / a - 1 for a, b in zip(vals, vals[1:]) if a > 0]


def compute(events, *, flow_tolerance=0.02, flow_min_usdt=2.0, max_points=1500):
    snaps = snapshots(events)
    if len(snaps) < 2:
        return {'ready': False, 'reason': 'need at least two bot cycles with balances',
                'points': len(snaps)}
    s0 = snaps[0]
    btc0 = s0['mids'].get('btc')
    ts, eq, hodl, btc, val, flows = [s0['t']], [1.0], [1.0], [1.0], [s0['value']], []
    for prev, cur in zip(snaps, snaps[1:]):
        held = _value(prev['bal'], cur['mids'])
        r = (held / prev['value'] - 1) if held and prev['value'] > 0 else 0.0
        eq.append(eq[-1] * (1 + r))
        h = _value(s0['bal'], cur['mids'])
        hodl.append(h / s0['value'] if h else hodl[-1])
        b = cur['mids'].get('btc')
        btc.append(b / btc0 if b and btc0 else btc[-1])
        ts.append(cur['t'])
        val.append(cur['value'])
        if held:
            gap = cur['value'] - held
            if abs(gap) > max(flow_min_usdt, flow_tolerance * held):
                flows.append({'t': cur['t'], 'usdt': round(gap, 2)})
    span_days = (ts[-1] - ts[0]) / 86400
    daily = _daily(ts, eq)
    vol = sharpe = None
    if span_days >= 7 and len(daily) >= 5:
        m = sum(daily) / len(daily)
        sd = math.sqrt(sum((x - m) ** 2 for x in daily) / (len(daily) - 1))
        vol = sd * math.sqrt(365)
        sharpe = (m * 365) / vol if vol > 0 else None
    ann = None
    if span_days >= 30 and eq[-1] > 0:
        ann = eq[-1] ** (365 / span_days) - 1
    modes = {str(e.get('mode')) for e in events if isinstance(e.get('balances'), dict) and e.get('mode')}
    basis = 'live' if modes == {'LIVE'} else 'simulation' if 'LIVE' not in modes else 'mixed'
    fees = 0.0
    executions = 0
    for e in events:
        if e.get('event') != 'trade':
            continue
        executions += 1
        try:
            fee, price = float(e.get('fee') or 0.0), float(e.get('price') or 0.0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fee) and math.isfinite(price):
            fees += fee * price if e.get('fee_asset') not in (None, 'usdt') else fee
    aggregate = sum(1 for e in events if e.get('event') == 'fill_aggregate')
    incomplete = sum(1 for e in events if e.get('event') == 'reconciliation_incomplete')
    step = max(1, len(ts) // max_points)
    idx = list(range(0, len(ts), step))
    if idx[-1] != len(ts) - 1:
        idx.append(len(ts) - 1)
    r4 = lambda x: None if x is None else round(x, 4)
    return {
        'ready': True, 'points': len(snaps), 'span_days': round(span_days, 2),
        'start_value_usdt': round(s0['value'], 2), 'value_usdt': round(val[-1], 2),
        'twr': r4(eq[-1] - 1), 'hodl_return': r4(hodl[-1] - 1), 'btc_return': r4(btc[-1] - 1),
        'vs_hodl': r4(eq[-1] - hodl[-1]), 'max_drawdown': r4(_max_dd(eq)),
        'btc_max_drawdown': r4(_max_dd(btc)), 'annualised_return': r4(ann),
        'annualised_vol': r4(vol), 'sharpe': None if sharpe is None else round(sharpe, 2),
        'net_flows_usdt': round(sum(f['usdt'] for f in flows), 2), 'flows': flows[-20:],
        'series': {'t': [ts[i] for i in idx], 'twr': [round(eq[i], 5) for i in idx],
                   'hodl': [round(hodl[i], 5) for i in idx], 'btc': [round(btc[i], 5) for i in idx],
                   'value': [round(val[i], 2) for i in idx]},
        'basis': basis, 'realized': basis == 'live', 'fees_usdt': round(fees, 4), 'executions': executions,
        'data_quality': {'aggregate_only_orders': aggregate, 'incomplete_orders': incomplete,
                         'exact': aggregate == 0 and incomplete == 0},
        'note': 'Time-weighted return of the actual holdings; deposits/withdrawals excluded. Fees are in the '
                'balances; fees_usdt sums recorded executions. basis=simulation means no order was sent: the '
                'figure is the market return of the holdings, not a strategy profit. Short histories are noisy.'}


def trade_stats(events, day=None):
    placed = [e for e in events if e.get('event') == 'order_placed']
    stale = [e for e in events if e.get('event') in ('cancel_stale', 'cancel_mode_dry')]
    by_asset = {}
    for e in placed:
        a = e.get('asset') or 'btc'
        d = by_asset.setdefault(a, {'buys': 0, 'sells': 0, 'notional_usdt': 0.0})
        d['buys' if e.get('action') == 'buy' else 'sells'] += 1
        d['notional_usdt'] = round(d['notional_usdt'] + float(e.get('notional_usdt') or 0), 2)
    return {'orders_placed': len(placed), 'cancelled_unfilled_or_stale': len(stale),
            'by_asset': by_asset,
            'recent': [{k: e.get(k) for k in ('at', 'action', 'asset', 'amount', 'price',
                                              'notional_usdt', 'id', 'mode')} for e in placed[-30:]][::-1]}
