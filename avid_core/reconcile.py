"""Accounting reconciliation of executed bot orders against the exchange's trade history (AVID-08)."""
from __future__ import annotations

import datetime as dt

from .client import NobitexError
from .records import _f, _final_status, order_numbers, today

SEEN_TRADES_MAX = 2000
TRADE_PAGES_MAX = 40                 # hard stop; paging normally ends at trades older than the oldest order
TRADE_HISTORY_S = 2.5 * 86400        # Nobitex lists the last 3 days of trades; after that the gap is final

# accounting statuses (AVID-08)
# accounting (AVID-08)
R_PENDING, R_PARTIAL, R_DONE = 'PENDING', 'PARTIALLY_RECONCILED', 'RECONCILED'
R_AGGREGATE, R_INCOMPLETE = 'AGGREGATE_ONLY', 'INCOMPLETE_RECONCILIATION'
ACCOUNTING_LABEL = {R_PENDING: 'reconciliation_pending', R_PARTIAL: 'reconciliation_pending', R_DONE: 'reconciled',
                    R_AGGREGATE: 'aggregate_only', R_INCOMPLETE: 'incomplete_reconciliation'}


def _parse_ts(x):
    try:
        t = dt.datetime.fromisoformat(str(x).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def _fetch_trades(client, asset, since):
    """The account's own trades on one market, newest first, back to `since` (minus 5 minutes).
    Returns (trades, pagination_complete)."""
    page_fn = getattr(client, 'trades_page', None)
    if page_fn is None:                               # simple clients: one page, completeness unknown
        return list(client.trades(src=asset, dst='usdt')), False
    out = []
    for page in range(1, TRADE_PAGES_MAX + 1):
        p = page_fn(src=asset, dst='usdt', page=page)
        rows = p.get('trades') if isinstance(p, dict) else None
        if not isinstance(rows, list):
            raise NobitexError('unexpected trades page', code='BAD_RESPONSE')
        out.extend(r for r in rows if isinstance(r, dict))
        if not p.get('hasNext'):
            return out, True
        stamps = [s for s in (_parse_ts(r.get('timestamp')) for r in rows if isinstance(r, dict)) if s is not None]
        if stamps and min(stamps) < since - 300:
            return out, True
    return out, False


def _recon_status(p, now) -> str:
    exp, obs = float(p.get('filled', 0.0)), float(p.get('recorded', 0.0))
    tol = max(1e-12, 1e-9 * max(exp, obs, 1e-12))
    window_over = now - float(p.get('since', now)) > TRADE_HISTORY_S
    if p.get('unknown_total'):
        return R_INCOMPLETE if window_over else (R_PARTIAL if obs > 0 else R_PENDING)
    if obs > exp + tol:
        return R_INCOMPLETE                            # more executions than the order filled
    if obs >= exp - tol and not p.get('anomaly'):
        return R_DONE
    if window_over:
        return R_INCOMPLETE if p.get('anomaly') else R_AGGREGATE
    return R_PARTIAL if obs > 0 else R_PENDING


def _recon_fields(p) -> dict:
    exp, obs = float(p.get('filled', 0.0)), float(p.get('recorded', 0.0))
    return {'expected_filled_amount': None if p.get('unknown_total') else round(exp, 12),
            'observed_filled_amount': round(obs, 12),
            'missing_filled_amount': None if p.get('unknown_total') else round(max(0.0, exp - obs), 12),
            'executions_count': len(p.get('trade_ids') or []), 'pagination_complete': bool(p.get('pagination_complete')),
            'last_reconciliation_at': p.get('last_reconciliation_at')}


def _reconcile_fills(client, store, st, now):
    """Accounting reconciliation, separate from the order cycle (AVID-08).

    Every order that executed anything stays in reconciliation until the exchange's trade history
    accounts for its whole filled amount, one 'trade' record per execution (exchange trade id = event id
    and idempotency key, so a retry never duplicates). Paging continues until trades older than the
    oldest pending order are reached (pagination_complete). Per order the expected / observed / missing
    amounts, execution count and status are kept. Closing an order is honest:
      RECONCILED                 observed == expected
      AGGREGATE_ONLY             history window over, gap recorded from the order totals (flagged)
      INCOMPLETE_RECONCILIATION  expected unknown, more executions than filled, or bad trade records"""
    pending = st.setdefault('fills_pending', {})
    if not pending or getattr(store, 'read_only', False):
        return
    seen = st.setdefault('seen_trades', [])
    for asset in sorted({p['asset'] for p in pending.values()}):
        mine = {oid: p for oid, p in pending.items() if p['asset'] == asset}
        for oid, p in mine.items():                   # expected total unknown: try the final record again
            if not p.get('unknown_total'):
                continue
            try:
                o = client.order_status(oid)
                fs = _final_status(o) if isinstance(o, dict) else 'unknown'
                nums, _ = order_numbers(o, {'amount': p.get('amount'), 'asset': asset}) if fs not in (None, 'unknown') \
                    else (None, None)
            except Exception:
                nums = None
            if nums is not None:
                p['filled'] = nums[1]
                p.pop('unknown_total', None)
                store.log('reconciliation_total_found', id=oid, asset=asset, filled_amount=nums[1],
                          message=f'final record of order {oid} is readable again: {nums[1]} {asset} filled')
        since = min(float(p.get('since', now)) for p in mine.values())
        try:
            trades, complete = _fetch_trades(client, asset, since)
        except Exception as e:
            for p in mine.values():
                p['attempts'] = int(p.get('attempts', 0)) + 1
            store.log('trades_unavailable', level='warning', asset=asset,
                      message=f'trade history for {asset} not available now ({str(e)[:120]}); '
                              f'{len(mine)} order(s) stay in reconciliation and are retried')
            continue
        for t in trades:
            oid, tid = str(t.get('orderId')), t.get('id')
            p = mine.get(oid)
            if p is None or tid is None or str(tid) in p['trade_ids']:
                continue
            amount = _f(t.get('amount'))
            if amount is None or amount <= 0:
                p['anomaly'] = True
                store.log('trade_record_invalid', eid=f'trade-invalid:{tid}', level='warning', id=oid, trade_id=str(tid),
                          message=f'trade {tid} of order {oid} has no usable amount; the order cannot be RECONCILED')
                continue
            p['trade_ids'].append(str(tid))
            p['recorded'] = float(p.get('recorded', 0.0)) + amount
            if str(tid) not in seen:
                seen.append(str(tid))
            store.log('trade', eid=f'trade:{tid}', trade_id=str(tid), id=oid, asset=asset, side=p['side'],
                      amount=amount, price=_f(t.get('price'), 0.0), fee=_f(t.get('fee'), 0.0),
                      fee_asset=asset if p['side'] == 'buy' else 'usdt', exchange_at=t.get('timestamp'))
        for oid, p in mine.items():
            p['pagination_complete'] = complete
            p['last_reconciliation_at'] = now
            status = _recon_status(p, now)
            changed = status != p.get('status')
            p['status'] = status
            fields = _recon_fields(p)
            if changed:
                store.log('reconciliation', eid=f'recon:{oid}:{status}', id=oid, asset=asset, reconciliation_status=status,
                          accounting=ACCOUNTING_LABEL[status], **fields)
            if status in (R_DONE, R_AGGREGATE, R_INCOMPLETE):
                store.ledger_append({'t': 'reconciled', 'id': oid, 'status': status,
                                     'day': today(float(p.get('since', now)))})
            if status == R_DONE:
                store.log('fills_reconciled', eid=f'recon-final:{oid}', id=oid, asset=asset, trades=fields['executions_count'],
                          filled_amount=p['filled'], accounting='reconciled', reconciliation_status=status, **fields)
                pending.pop(oid)
            elif status == R_AGGREGATE:
                gap = max(0.0, p['filled'] - p['recorded'])
                store.log('fill_aggregate', eid=f'recon-final:{oid}', id=oid, asset=asset, side=p['side'], level='error',
                          filled_amount=gap, avg_price=p['avg_price'], fee=p['fee'], accounting='aggregate_only',
                          reconciliation_status=status, **fields,
                          message=f'order {oid}: {gap:.8f} {asset} executed but not found in the trade history window; '
                                  'recorded from the order totals and flagged AGGREGATE_ONLY (not a reconciliation)')
                pending.pop(oid)
            elif status == R_INCOMPLETE:
                store.log('reconciliation_incomplete', eid=f'recon-final:{oid}', id=oid, asset=asset, level='error',
                          accounting='incomplete_reconciliation', reconciliation_status=status, **fields,
                          message=f'order {oid}: accounting is INCOMPLETE (expected '
                                  f'{fields["expected_filled_amount"]}, observed {fields["observed_filled_amount"]}); '
                                  'check it in the Nobitex panel - it is not reported as reconciled')
                pending.pop(oid)
            else:
                p['attempts'] = int(p.get('attempts', 0)) + 1
    del seen[:-SEEN_TRADES_MAX]
