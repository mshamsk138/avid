"""Explicit, conservative order state machine (AVID-01).

                CREATED
                   |
          SUBMISSION_PENDING ------------------------------+
           |       |        \\                              |
         OPEN  PARTIALLY_FILLED  REJECTED_CONFIRMED         |
           |  \\   |                                          v
           |  CANCEL_REQUESTED                             UNKNOWN --> MANUAL_RECONCILIATION
           v       v
   FILLED_CONFIRMED | CANCELED_CONFIRMED | PARTIALLY_FILLED_CANCELED

Rules (enforced by cycle.py, checked by tests):
- every answer the engine does not understand - an unknown status word, a record without amounts,
  amounts that do not add up, a fill that went backwards, a 'done' order with an unfilled rest - is
  UNKNOWN, never a guess;
- UNKNOWN keeps the reserved notional counted and blocks new orders until the exchange gives a
  definite final record or the customer settles it by hand (MANUAL_RECONCILIATION, audited);
- an accepted cancel REQUEST is CANCEL_REQUESTED, not a cancellation; only the final record decides;
- only a *_CONFIRMED / PARTIALLY_FILLED_CANCELED / REJECTED_CONFIRMED state releases anything, only
  the unfilled part, and only once (the order record carries `settled`);
- every transition is logged once with a deterministic event id (state:<cid>:<STATE>), so a replay
  after a crash cannot report it twice.
"""
from __future__ import annotations

import time

CREATED = 'CREATED'
SUBMISSION_PENDING = 'SUBMISSION_PENDING'
OPEN = 'OPEN'
PARTIALLY_FILLED = 'PARTIALLY_FILLED'
CANCEL_REQUESTED = 'CANCEL_REQUESTED'
UNKNOWN = 'UNKNOWN'
MANUAL_RECONCILIATION = 'MANUAL_RECONCILIATION'
FILLED_CONFIRMED = 'FILLED_CONFIRMED'
CANCELED_CONFIRMED = 'CANCELED_CONFIRMED'
PARTIALLY_FILLED_CANCELED = 'PARTIALLY_FILLED_CANCELED'
REJECTED_CONFIRMED = 'REJECTED_CONFIRMED'

TERMINAL = {FILLED_CONFIRMED, CANCELED_CONFIRMED, PARTIALLY_FILLED_CANCELED, REJECTED_CONFIRMED,
            MANUAL_RECONCILIATION}
RELEASING = {CANCELED_CONFIRMED, PARTIALLY_FILLED_CANCELED, REJECTED_CONFIRMED}
FINAL_BY_STATUS = {'filled': FILLED_CONFIRMED, 'canceled': CANCELED_CONFIRMED,
                   'partially_filled_canceled': PARTIALLY_FILLED_CANCELED, 'rejected': REJECTED_CONFIRMED}
_LIVE = {OPEN, PARTIALLY_FILLED, CANCEL_REQUESTED, UNKNOWN}
_FINAL = {FILLED_CONFIRMED, CANCELED_CONFIRMED, PARTIALLY_FILLED_CANCELED}
# MANUAL_RECONCILIATION (a customer decision, audited) is allowed from every non-terminal state.
ALLOWED = {
    None: {CREATED, SUBMISSION_PENDING, OPEN, PARTIALLY_FILLED, CANCEL_REQUESTED, UNKNOWN, MANUAL_RECONCILIATION}
    | _FINAL,                                                                         # v1.2 records had no state
    CREATED: {SUBMISSION_PENDING},
    SUBMISSION_PENDING: _LIVE | _FINAL | {REJECTED_CONFIRMED, MANUAL_RECONCILIATION},
    OPEN: {PARTIALLY_FILLED, CANCEL_REQUESTED, UNKNOWN, MANUAL_RECONCILIATION} | _FINAL,
    PARTIALLY_FILLED: {CANCEL_REQUESTED, UNKNOWN, MANUAL_RECONCILIATION} | _FINAL,
    CANCEL_REQUESTED: {PARTIALLY_FILLED, UNKNOWN, MANUAL_RECONCILIATION} | _FINAL,
    UNKNOWN: _LIVE | _FINAL | {MANUAL_RECONCILIATION},
}
HISTORY_MAX = 20


def state_of(info) -> str | None:
    return info.get('state')


def is_terminal(info) -> bool:
    return info.get('state') in TERMINAL


def transition(store, info: dict, new: str, *, oid=None, reason: str = '', level: str = 'info', **extra) -> bool:
    """Move one tracked order to `new`. Returns True when the state changed (and was logged once)."""
    old = info.get('state')
    if old == new:
        return False
    if new not in ALLOWED.get(old, set()):
        store.log('order_state_anomaly', level='error', id=oid, cid=info.get('cid'), from_state=old,
                  to_state=new, reason=reason,
                  message=f'refused transition {old} -> {new} for order {oid or info.get("cid")}; state kept')
        return False
    info['state'] = new
    hist = info.setdefault('history', [])
    hist.append([new, round(time.time(), 3)])
    del hist[:-HISTORY_MAX]
    key = info.get('cid') or oid or 'unknown'
    store.log('order_state', eid=f'state:{key}:{new}', level=level, id=oid, cid=info.get('cid'),
              asset=info.get('asset'), side=info.get('side'), amount=info.get('amount'), price=info.get('price'),
              from_state=old, to_state=new, reason=reason, **extra)
    return True
