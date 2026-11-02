"""Interpretation of exchange records and small shared helpers (no I/O, no state).

Everything here turns what the exchange said into facts the engine may act on, conservatively: a
record that is missing amounts, or whose amounts do not add up, or whose status word is unknown, is
NOT turned into a guess (AVID-01).
"""
from __future__ import annotations

import datetime as dt
import math

from . import orderstate as ost
from .rebalance import MARKETS

FINAL_CLOSED = {'done': 'filled', 'canceled': 'canceled', 'cancelled': 'canceled', 'rejected': 'rejected'}
OPEN_STATES = {'new', 'active', 'inactive', 'open'}


def today(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y-%m-%d')


def decimals(step):
    return max(0, -int(math.floor(math.log10(step) + 1e-9)))


def _f(x, default=None):
    if isinstance(x, bool):
        return default
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _order_id(o, asset, mine) -> str:
    """Exchange id, else the bot's clientOrderId. Ownership is NEVER inferred from similar price and
    amount (a customer's own order can look exactly the same - audit 1.1 P0): a listing row without
    id or our clientOrderId is foreign."""
    if o.get('id') is not None:
        return str(o['id'])
    cid = o.get('clientOrderId')
    if cid:
        for oid, info in mine.items():
            if info.get('cid') == cid:
                return oid
    return 'unknown:' + str(o.get('type')) + ':' + str(o.get('amount')) + '@' + str(o.get('price'))


def _step(info) -> float:
    return float(MARKETS.get(info.get('asset'), {}).get('amount_step', 1e-8))


def order_numbers(o, info):
    """((amount, matched, unfilled), None) from an exchange order record, or (None, why) when the record
    is missing amounts or they do not add up (AVID-01: inconsistent numbers are UNKNOWN, never a guess)."""
    sent = _f(info.get('amount'), 0.0) or 0.0
    amount = _f(o.get('amount'))
    if amount is None:
        amount = sent
    if amount <= 0:
        return None, 'the record has no order amount'
    tol = max(1e-12, 1e-9 * amount)
    if sent > 0 and abs(amount - sent) > tol + _step(info) / 2:
        return None, f'amount {amount} differs from the amount sent {sent}'
    m, u = _f(o.get('matchedAmount')), _f(o.get('unmatchedAmount'))
    if m is None and u is None:
        return None, 'the record has neither matchedAmount nor unmatchedAmount'
    if m is None:
        m = amount - u
    if u is None:
        u = amount - m
    if m < -tol or u < -tol:
        return None, 'negative matched/unmatched amount'
    if abs(m + u - amount) > tol:
        return None, f'matched {m} + unmatched {u} != amount {amount}'
    m = min(max(m, 0.0), amount)
    if m < float(info.get('matched') or 0.0) - tol:
        return None, f'filled amount went backwards ({info.get("matched")} -> {m})'
    return (amount, m, max(0.0, amount - m)), None


def _final_status(order) -> str | None:
    """'filled' | 'canceled' | 'rejected' for a closed order, None while it is still open, 'unknown' for
    any status word the engine does not know - which is NEVER settled from the amounts (AVID-01)."""
    s = str(order.get('status', '')).lower()
    if s in OPEN_STATES:
        return None
    return FINAL_CLOSED.get(s, 'unknown')


def _open_state(info, matched):
    if info.get('cancel_requested'):
        return ost.CANCEL_REQUESTED
    return ost.PARTIALLY_FILLED if matched > 1e-12 else ost.OPEN


def _definitely_rejected(err) -> bool:
    """True when the exchange explicitly refused the order (so it cannot exist).
    Timeouts, network errors, 5xx, 408/409/425/429, unreadable replies and every 'duplicate' answer
    (which means an order with these parameters MAY exist) are NOT definite."""
    code = str(getattr(err, 'code', None) or '')
    if code == 'NOT_SENT':                            # DNS failure / connection refused: nothing left us
        return True
    if code in ('NETWORK', 'BAD_RESPONSE', 'REDIRECT') or 'duplicate' in code.lower():
        return False
    status = getattr(err, 'http_status', None)
    if status is not None:
        if status in (408, 409, 425, 429) or status >= 500:
            return False
        if 400 <= status < 500:
            return True
    return str(err).startswith('API error')


def _log_info(info) -> dict:
    return {k: v for k, v in info.items() if k not in ('history', 'checks', 'alerted', 'state', 'status_errors')}


def _wallets(client, currencies):
    fn = getattr(client, 'wallets', None)
    if fn is not None:
        w = fn(currencies=currencies)
        return {c: float(w[c]['total']) for c in currencies}, {c: float(w[c]['available']) for c in currencies}
    b = client.balances(currencies=currencies)
    return dict(b), dict(b)
