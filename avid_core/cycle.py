"""Execution engine: one rebalancing cycle against an exchange adapter (BTC, PAXG, USDT as hub).

Module map (responsibilities, report section 3.2):
  rebalance.py   strategy  - NAV over managed assets, deadband, ONE order, Decimal sizing
  risk.py        risk      - pre-trade checks (cash floor, day lock, daily caps)
  cycle.py       execution - this file: order state machine in action, lost replies, cancels, STOP
  records.py     adapter   - interpretation of exchange records (status words, amounts)
  store.py       ledger    - state.json, engine log, submission ledger and recovery
  reconcile.py   reconciliation of executions against the trade history
  cli.py         the avid-engine command line (python -m avid_core.cycle still works)

Safety (all enforced in code, see _cycle()):
- dry-run unless --live is given explicitly
- kill switch: if data/bot/STOP (customer) or data/bot/STOP.cloud (cloud) exists, no new orders and the
  bot's own open orders are cancelled; the cloud only ever touches STOP.cloud
- only LIMIT orders on <ASSET>USDT markets, resting at best bid (buy) / best ask (sell); this aims
  at maker execution but Nobitex has no post-only flag, so a fast market can still match it as taker
- every order is at least the exchange minimum (more than 11 USDT) and at most --max-order-usdt
- at most one bot order open; never trades while a non-bot order is open on ANY managed market
- per-order, per-day count and per-day notional caps; every risk number is validated (finite, typed,
  in range) and an invalid one stops new orders instead of disabling the cap (Invariant C)
- orders follow an explicit state machine (orderstate.py). An answer the engine does not understand
  is UNKNOWN: nothing is released and no new order is placed until a definite final record arrives or
  the customer settles it by hand (Invariants A and B). A cancel REQUEST is not a cancellation.
- each order carries a unique clientOrderId saved durably (fsync) before it is sent; an order whose
  reply was lost is found again by that id (open list, status-by-id lookup, full list) - never by
  similar fields. Until it is settled no new order is placed. The account's clientOrderId support is
  VERIFIED after the first order (status lookup by that id) before it is relied on again.
- a durable submission ledger (submissions.jsonl) survives a corrupt or deleted state.json: the day's
  counters are restored from it, unsettled orders are tracked again and new orders stay off that day
- the cap day never moves backwards; a clock that went back stops new orders until it is right
- accounting: every executed order stays in reconciliation until the trade history accounts for its
  whole filled amount (RECONCILED), or is closed as AGGREGATE_ONLY / INCOMPLETE_RECONCILIATION with
  the gap stated - never reported complete when it is not (Invariant F)
- control.json (dashboard / agent) is re-read every cycle. It may change weights/band, TIGHTEN caps and
  switch dry<->live, but never above the CLI ceilings and never to live unless started with --live. An
  invalid file, or a missing one after a valid one was in use, keeps the last valid file in force and
  stops new orders (fail closed, report 10.2).
- LIVE orders need a constant-time Ed25519 signer (python3-cryptography) in API-key mode
- not being allowed to trade never takes away the right to CANCEL: dry mode, STOP, a run without
  --live, or an exchange listing error still cancel the bot's own real orders (only the read-only
  `plan` preview never touches them). A real order is never forgotten.
"""
from __future__ import annotations
import contextlib
import datetime as dt
import fcntl
import os
import secrets
import sys
import time
from dataclasses import asdict

from . import control, ed25519, fsio
from . import orderstate as ost
from . import risk
from .client import NobitexError
from .rebalance import (MARKET_SPECS_META, MARKETS, ValuationIncomplete, apply_market_overrides, exchange_min_order,
                        plan_multi, reset_markets)
from .validation import loads_strict
# re-exported names: the public surface of avid_core.cycle stays what it was in 0.1.0a1
from .records import (FINAL_CLOSED, OPEN_STATES, _definitely_rejected, _f, _final_status, _log_info,  # noqa: F401
                      _open_state, _order_id, _step, _wallets, decimals, order_numbers, today)
from .reconcile import (ACCOUNTING_LABEL, R_AGGREGATE, R_DONE, R_INCOMPLETE, R_PARTIAL, R_PENDING,  # noqa: F401
                        SEEN_TRADES_MAX, TRADE_HISTORY_S, TRADE_PAGES_MAX, _fetch_trades, _reconcile_fills,
                        _recon_status)
from .store import PreviewStore, Store, _recover_from_ledger, check_state  # noqa: F401

DEFAULT_FEE_RESERVE = 0.003          # buy-side reserve for the exchange fee (taker rate, conservative)
PENDING_ALERT_CHECKS = 3             # unresolved lost reply: escalate to the customer after this many lookups
STATUS_ALERT_ERRORS = 6              # unknown final state: escalate after this many failed lookups
CLOCK_TOLERANCE_S = 300              # the wall clock may step back this much (NTP) before new orders stop
RESOLVE_MIN_AGE_S = 600              # 'resolve --not-placed' only after the lists had time to catch up
LEDGER_KEEP_DAYS = 7
CID_PREFIX = 'av'


class ManualResolution(RuntimeError):
    pass


def _instance_tag(st) -> str:
    tag = st.get('instance')
    if not (isinstance(tag, str) and len(tag) == 4 and all(c in '0123456789abcdef' for c in tag)):
        tag = secrets.token_hex(2)
        st['instance'] = tag
    return tag


def _new_cid(st, now) -> str:
    """32 chars (Nobitex maximum): 'av' + data-folder instance tag + sequence + 80 random bits."""
    st['seq'] = int(st.get('seq', 0)) + 1
    return f'{CID_PREFIX}{_instance_tag(st)}{st["seq"] % 10**6:06d}{secrets.token_hex(10)}'


def _finalize(client, store, st, oid, info, day, *, reason) -> dict | None:
    """Read the order's final record and settle the caps. None = not settled (state UNKNOWN; keep it)."""
    if info.get('settled'):                           # never settle twice
        return {'already_settled': True}
    try:
        o = client.order_status(oid)
        if not isinstance(o, dict):
            raise NobitexError('order-status reply is not an object', code='BAD_RESPONSE')
    except Exception as e:                            # unknown: never guess filled or canceled
        info['status_errors'] = int(info.get('status_errors', 0)) + 1
        ost.transition(store, info, ost.UNKNOWN, oid=oid, reason=f'status lookup failed: {str(e)[:120]}',
                       level='warning')
        store.log('order_status_unknown', id=oid, level='warning',
                  message=f'final state of order {oid} unknown ({str(e)[:160]}); caps stay counted')
        return None
    final = _final_status(o)
    if final is None:
        return {'still_open': True, 'order': o}
    why = None
    nums = None
    if final == 'unknown':
        why = f'unrecognised status {str(o.get("status"))[:40]!r}'
    else:
        nums, why = order_numbers(o, info)
    if nums is not None:
        amount, matched, unfilled = nums
        tol = max(1e-12, 1e-9 * amount)
        if final == 'filled' and unfilled > tol:
            why, nums = f'status says filled but {unfilled} is unfilled', None
        elif final == 'rejected' and matched > tol:
            why, nums = f'status says rejected but {matched} is filled', None
    if nums is None:
        info['status_errors'] = int(info.get('status_errors', 0)) + 1
        ost.transition(store, info, ost.UNKNOWN, oid=oid, reason=why, level='warning')
        store.log('order_status_unknown', id=oid, level='warning',
                  message=f'order {oid}: {why}; nothing is released and no new order is placed until the exchange '
                          'gives a definite final record')
        return None
    amount, matched, unfilled = nums
    tol = max(1e-12, 1e-9 * amount)
    if final == 'canceled' and matched > tol:
        final = 'partially_filled_canceled'          # final: part executed, the rest cancelled
    avg = _f(o.get('averagePrice'))
    if not avg or avg <= 0:
        avg = info['price']
    fee = _f(o.get('fee'), 0.0) or 0.0
    refund = 0.0
    if info.get('day') == day:
        refund = min(st['notional_today'], unfilled * info['price'])
        st['notional_today'] = max(0.0, st['notional_today'] - refund)
        if matched > tol:
            st['filled_orders_today'] = int(st.get('filled_orders_today', 0)) + 1
            st['filled_usdt_today'] = float(st.get('filled_usdt_today', 0.0)) + matched * avg
    info['settled'] = True
    new_state = ost.FINAL_BY_STATUS[final]
    ost.transition(store, info, new_state, oid=oid, reason=reason, filled_amount=round(matched, 12),
                   unfilled_amount=round(unfilled, 12), cap_refund_usdt=round(refund, 4))
    has_fills = matched > tol
    rec = store.log('order_final', eid=f'final:{oid}', id=oid, cid=info.get('cid'), asset=info.get('asset'),
                    side=info.get('side'), amount=info['amount'], price=info['price'], status=final, state=new_state,
                    filled_amount=round(matched, 12), unfilled_amount=round(unfilled, 12), avg_price=avg, fee=fee,
                    fee_asset=info.get('asset') if info.get('side') == 'buy' else 'usdt',
                    cap_refund_usdt=round(refund, 4), reason=reason,
                    accounting='reconciliation_pending' if has_fills else 'none')
    store.ledger_append({'t': 'final', 'id': oid, 'cid': info.get('cid'), 'state': new_state, 'day': info.get('day'),
                         'asset': info.get('asset'), 'side': info.get('side'), 'amount': info.get('amount'),
                         'filled': matched if has_fills else 0.0, 'avg_price': avg, 'fee': fee,
                         'since': info.get('placed_at')})
    if has_fills:                                     # accounting is settled separately (_reconcile_fills)
        st.setdefault('fills_pending', {})[oid] = {
            'asset': info['asset'], 'side': info['side'], 'filled': matched, 'avg_price': avg, 'fee': fee,
            'amount': info['amount'], 'since': info.get('placed_at') or time.time(), 'recorded': 0.0,
            'trade_ids': [], 'attempts': 0, 'status': R_PENDING, 'pagination_complete': False,
            'last_reconciliation_at': None}
    return rec


def _lookup_pending(client, pend, open_orders, mine):
    """Find the order of a lost placement reply by its clientOrderId only: open list, status lookup by
    clientOrderId, full order list. (found record | None, where)."""
    cid = pend.get('cid')
    if not cid:
        return None, None
    for o in open_orders:
        if o.get('clientOrderId') == cid and o.get('id') is not None and str(o['id']) not in mine:
            return o, 'open'
    try:
        o = client.order_status(client_order_id=cid)
        if isinstance(o, dict) and o.get('id') is not None and str(o['id']) not in mine \
                and o.get('clientOrderId') in (None, cid):
            return o, 'status'
    except Exception:
        pass
    try:
        for o in client.list_orders(src=pend['asset'], dst='usdt', status='all'):
            if o.get('clientOrderId') == cid and o.get('id') is not None and str(o['id']) not in mine:
                return o, 'all'
    except Exception:
        pass
    return None, None


def _matches_pending(o, pend) -> bool:
    """The found record must describe the order we sent (same market, side and amount)."""
    src = o.get('srcCurrency')
    if src is not None and str(src).lower() != pend.get('asset'):
        return False
    side = o.get('type')
    if side is not None and str(side).lower() != pend.get('side'):
        return False
    amt = _f(o.get('amount'))
    if amt is not None and abs(amt - float(pend.get('amount') or 0)) > max(1e-12, 1e-9 * amt) + _step(pend) / 2:
        return False
    return True


def reset_client_order_id(store):
    """After Nobitex enabled clientOrderId for the account: allow live trading to try it again."""
    with _exclusive(store):
        st = store.load()
        st['cid_supported'] = None
        st['cid_verify'] = None
        store.save(st)
        store.log('client_order_id_reset', message='clientOrderId will be tried (and verified) again on the next live order')


def resolve_manually(store, *, pending_not_placed: bool = False, closing_ids=(), client=None,
                     confirm_cid: str | None = None, now: float | None = None, operator: str | None = None) -> list:
    """Customer decision after checking the Nobitex panel (CLI: avid-agent resolve).
    - pending_not_placed: the order whose placement reply was lost does NOT exist on the exchange.
      Refused unless a FRESH exchange lookup by clientOrderId (open list, status, full list) finds nothing,
      the placement is at least RESOLVE_MIN_AGE_S old, and (when given) confirm_cid matches - a message on
      a dashboard cannot talk the customer into a duplicate order (audit 45). Caps stay counted.
    - closing_ids: these bot orders are no longer open (their final record could not be read); their
      caps stay counted and any executions are still collected from the trade history; their accounting
      can only end INCOMPLETE unless the final record becomes readable again.
    Every decision is audited (event + ledger). Returns what was changed."""
    operator = operator or os.environ.get('USER') or str(os.getuid())
    with _exclusive(store):
        st = store.load()
        done = []
        if pending_not_placed:
            p = st.get('pending')
            if not p:
                raise ManualResolution('there is no unresolved placement')
            if confirm_cid is not None and confirm_cid.strip() != p.get('cid'):
                raise ManualResolution('the clientOrderId typed does not match the unresolved placement')
            if client is None:
                raise ManualResolution('a fresh look at the exchange is required: use  avid-agent resolve  '
                                       '(it checks Nobitex by clientOrderId first)')
            age = (time.time() if now is None else now) - float(p.get('placed_at') or 0)
            if age < RESOLVE_MIN_AGE_S:
                raise ManualResolution(f'too early: the placement is {age / 60:.0f} min old; exchange lists can lag - '
                                       f'try again in {(RESOLVE_MIN_AGE_S - age) / 60:.0f} min')
            try:
                open_orders = list(client.open_orders(src=p['asset'], dst='usdt'))
            except Exception as e:
                raise ManualResolution(f'could not read open orders from Nobitex now ({str(e)[:80]}); nothing changed') from None
            found, where = _lookup_pending(client, p, open_orders, st['bot_orders'])
            if found is not None:
                raise ManualResolution(f'the order EXISTS on Nobitex (id {found.get("id")}, found in the {where} lookup); '
                                       'it is adopted automatically by the next cycle - nothing to resolve')
            st['pending'] = None
            ost.transition(store, p, ost.MANUAL_RECONCILIATION, reason=f'customer ({operator}): not placed')
            store.log('pending_resolved_manually', level='warning', cid=p.get('cid'), asset=p.get('asset'),
                      side=p.get('side'), amount=p.get('amount'), price=p.get('price'), operator=operator,
                      message='customer confirmed in the Nobitex panel that the order does not exist and a fresh '
                              'lookup by clientOrderId found nothing; its caps stay counted')
            store.ledger_append({'t': 'outcome', 'cid': p.get('cid'), 'result': 'manual_not_placed', 'day': p.get('day'),
                                 'operator': operator})
            store.ledger_append({'t': 'final', 'cid': p.get('cid'), 'state': ost.MANUAL_RECONCILIATION, 'day': p.get('day')})
            done.append('pending')
        for oid in closing_ids:
            info = (st.get('closing') or {}).pop(str(oid), None)
            if info is None:
                raise ManualResolution(f'order {oid} is not waiting for a final state')
            ost.transition(store, info, ost.MANUAL_RECONCILIATION, oid=str(oid), reason=f'customer ({operator}): closed')
            st.setdefault('fills_pending', {})[str(oid)] = {
                'asset': info['asset'], 'side': info['side'], 'filled': 0.0, 'avg_price': info['price'], 'fee': 0.0,
                'amount': info['amount'], 'since': info.get('placed_at') or time.time(), 'recorded': 0.0,
                'trade_ids': [], 'attempts': 0, 'unknown_total': True, 'status': R_PENDING,
                'pagination_complete': False, 'last_reconciliation_at': None}
            store.log('order_resolved_manually', id=str(oid), level='warning', status='unknown', operator=operator,
                      message='customer confirmed the order is closed; caps stay counted, executions are '
                              'collected from the trade history, accounting stays INCOMPLETE unless the final '
                              'record becomes readable')
            store.ledger_append({'t': 'final', 'id': str(oid), 'cid': info.get('cid'), 'state': ost.MANUAL_RECONCILIATION,
                                 'day': info.get('day'), 'operator': operator})
            done.append(str(oid))
        store.save(st)
        return done


def _cancel_and_settle(client, store, st, mine, oid, day, reason):
    """Cancel one bot order and settle it. Returns (record | None, cancel_request_accepted)."""
    info = mine[oid]
    try:
        client.cancel(oid)
    except Exception as e:
        # maybe it filled or was cancelled meanwhile: let the final status decide
        res = _finalize(client, store, st, oid, info, day, reason=reason)
        if res and not res.get('still_open'):
            mine.pop(oid)
            return res, True
        store.log('cancel_failed', id=oid, level='error',
                  message=f'could not cancel bot order {oid}: {str(e)[:160]} - it may still fill; '
                          'check it in the Nobitex panel')
        return None, False
    info['cancel_requested'] = True
    ost.transition(store, info, ost.CANCEL_REQUESTED, oid=oid, reason=reason)
    res = _finalize(client, store, st, oid, info, day, reason=reason)
    if res is None:                                   # cancel accepted, final record unknown: settle later
        st.setdefault('closing', {})[oid] = mine.pop(oid)
        return None, True
    if res.get('still_open'):                         # exchange has not applied the cancel yet
        return None, True
    mine.pop(oid)
    return res, True


def _cancel_unknown(client, store, closing, reason):
    """STOP / dry mode also reaches orders whose state is UNKNOWN (they may still be on the book)."""
    for oid, info in closing.items():
        if info.get('cancel_requested') or info.get('state') in ost.TERMINAL:
            continue
        try:
            client.cancel(oid)
        except Exception:
            continue                                  # not open any more (or unreachable): final record decides
        info['cancel_requested'] = True
        ost.transition(store, info, ost.CANCEL_REQUESTED, oid=oid, reason=reason)


class EngineBusy(RuntimeError):
    """Another process holds this data folder (two engines on one state would double-trade)."""


@contextlib.contextmanager
def _exclusive(store):
    if getattr(store, 'read_only', False):            # the preview never writes: no lock needed
        yield
        return
    f = fsio.open_lock(store.dir / '.lock')
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise EngineBusy(f'another AVID engine process is using {store.dir}; only one may run') from None
        yield
    finally:
        f.close()                                     # closing releases the lock


def cycle(client, store, cfg, **kw) -> dict:
    """One rebalancing step under an exclusive lock on the data folder (see _cycle).

    If the cycle cannot write (disk full, read-only or failing file system) and the bot should not be
    trading (STOP, dry mode, caller not live), cancel requests are still sent for every order the state
    knows (emergency_cancel) before the storage error is raised: a full disk must not disable STOP."""
    with _exclusive(store):
        try:
            return _cycle(client, store, cfg, **kw)
        except OSError as e:
            if kw.get('may_cancel', True) and not getattr(store, 'read_only', False) \
                    and _cancel_wanted(store, kw.get('live', False)):
                e.emergency = emergency_cancel(client, store)
            raise


def _cancel_wanted(store, live) -> bool:
    if store.stop_requested or not live:
        return True
    ctl, err, _present = control.read(store.root)
    return bool(err) or ctl.get('mode') == 'dry'


def emergency_cancel(client, store) -> dict:
    """Send a cancel request for every bot order the state knows - WITHOUT writing anything. Nothing is
    released and no state changes; the next normal cycle settles the outcome from the exchange's own
    records (a cancel request is not a cancellation)."""
    st = store.peek()
    sent, failed = [], []
    for group in ('bot_orders', 'closing'):
        for oid, info in (st.get(group) or {}).items():
            if info.get('state') in ost.TERMINAL:
                continue
            try:
                client.cancel(oid)
                sent.append(oid)
            except Exception as e:                    # not open any more, or unreachable
                failed.append(f'{oid}: {str(e)[:80]}')
    pend = st.get('pending')
    if pend and pend.get('cid'):
        try:
            client.cancel(client_order_id=pend['cid'])
            sent.append(f'cid:{pend["cid"]}')
        except Exception as e:
            failed.append(f'cid:{pend["cid"]}: {str(e)[:80]}')
    return {'cancel_requests_sent': sent, 'failed': failed}


def _conflict(store, st, reason, info):
    """A configuration problem is an error the first time (notifies the customer), then a warning
    while the same reason persists - no notification every cycle."""
    level = 'warning' if st.get('last_conflict') == reason else 'error'
    st['last_conflict'] = reason
    store.save(st)
    return store.log('config_conflict', level=level, **{**info, 'reason': reason})


def _alert_once(store, st, key, event, message, level='error', **kw):
    """Log an alert when `key` changes; nothing while the same condition persists."""
    alerts = st.setdefault('alerts', {})
    if alerts.get(event) == key:
        return
    alerts[event] = key
    store.log(event, level=level, message=message, **kw)


def _clear_alert(st, event):
    (st.get('alerts') or {}).pop(event, None)


def _apply_markets_file(store):
    """Optional operator override of the market specs (report 10.5). Returns an error message or None."""
    p = store.dir / 'markets.json'
    if not p.exists():
        reset_markets()
        return None
    try:
        apply_market_overrides(loads_strict(p.read_text()))
        return None
    except (ValueError, OSError) as e:
        reset_markets()
        return f'markets.json invalid: {str(e)[:160]}'


def _verify_cid(client, store, st) -> str:
    """'verified' | 'unsupported' | 'unknown' for the account's clientOrderId support (AVID-07)."""
    v = st.get('cid_verify')
    if not v:
        return 'verified'
    try:
        o = client.order_status(client_order_id=v['cid'])
    except NobitexError as e:
        if not _definitely_rejected(e):
            return 'unknown'
        o = None
    except Exception:
        return 'unknown'
    if isinstance(o, dict) and str(o.get('id')) == str(v['id']) and o.get('clientOrderId') in (None, v['cid']):
        st['cid_supported'], st['cid_verify'] = True, None
        store.log('client_order_id_verified', id=str(v['id']),
                  message='clientOrderId confirmed: the exchange finds the order by the id the bot chose')
        return 'verified'
    st['cid_supported'], st['cid_verify'] = False, None
    store.log('client_order_id_unavailable', level='error',
              message='the exchange could not find the last order by its clientOrderId: live trading stays off, '
                      'because a lost reply could not be matched to its order with certainty')
    return 'unsupported'


def _cycle(client, store, cfg, *, live: bool, now=None, may_cancel: bool = True,
           allow_new: bool = True, hold_reason: str = '') -> dict:
    """One rebalancing step.

    live        this process may trade for real (started with --live / agent allowed); the control
                file can still keep it in dry mode.
    may_cancel  the bot may cancel ITS OWN real orders. True everywhere except the read-only preview:
                not being allowed to trade never takes away the ability to cancel (audit P0-4).
    allow_new   new orders may be placed this cycle (the agent sets False when the cloud link is not
                fresh, the agent was revoked, the outbox is under storage pressure, ...); open orders are
                still managed and cancelled.
    """
    now = time.time() if now is None else now
    if getattr(store, 'read_only', False):            # preview: observe only, never touch orders
        may_cancel, live = False, False
    live_allowed = live
    holds = [] if allow_new else [hold_reason or 'new orders paused']
    st = store.load()
    recovered = st.pop('recovered_from_corrupt', False) | st.pop('state_missing', False)

    # ---- clock: the cap day never moves backwards; a clock that went back stops new orders
    last_now = st.get('last_now')
    if last_now is not None and now < last_now - CLOCK_TOLERANCE_S:
        holds.append(f'system clock is {last_now - now:.0f} s behind the previous cycle; no new orders until it is '
                     'correct (check NTP)')
        _alert_once(store, st, 'back', 'clock_backwards', holds[-1])
    else:
        _clear_alert(st, 'clock_backwards')
    st['last_now'] = max(float(last_now or now), now)
    server_now = None
    try:
        server_now = client.server_time() if hasattr(client, 'server_time') else None
    except Exception:
        server_now = None
    if server_now is not None and abs(server_now - now) > CLOCK_TOLERANCE_S:
        holds.append(f'system clock differs from the exchange clock by {now - server_now:+.0f} s; no new orders '
                     'until it is correct (check NTP)')
        _alert_once(store, st, 'skew', 'clock_mismatch', holds[-1])
    else:
        _clear_alert(st, 'clock_mismatch')
    day = today(now)
    if st.get('day') and day < st['day']:
        day = st['day']
    if recovered:
        day = _recover_from_ledger(store, st, day)
        st['locked_day'] = day

    # ---- control file: fail closed
    ctl, ctl_err, present = control.read(store.root)
    if ctl_err:
        holds.append(ctl_err + '; the last valid control file stays in force')
        _alert_once(store, st, ctl_err, 'control_invalid', ctl_err + '; no new orders until it is valid again')
        ctl = st.get('last_valid_control') or {'mode': 'dry'}
    elif not present and st.get('control_seen'):
        msg = 'control.json is missing although it was in use; the last valid one stays in force'
        holds.append(msg)
        _alert_once(store, st, 'missing', 'control_invalid', msg + '; no new orders until it is back')
        ctl = st.get('last_valid_control') or {'mode': 'dry'}
    else:
        _clear_alert(st, 'control_invalid')
        if present:
            st['control_seen'], st['last_valid_control'] = True, ctl
    markets_err = _apply_markets_file(store)
    if markets_err:
        holds.append(markets_err)
        _alert_once(store, st, markets_err, 'markets_invalid', markets_err)
    base_cfg = cfg
    cfg, live = control.effective(cfg, ctl, live_allowed=live_allowed)
    bad = control.check_cfg(cfg)
    if bad:
        holds.append(f'invalid risk parameter: {bad}')
        _alert_once(store, st, bad, 'config_invalid', f'invalid risk parameter ({bad}); no new orders')
    min_order = exchange_min_order()
    if not bad:
        min_order = max(float(cfg.min_order_usdt), min_order)
    fee_reserve = DEFAULT_FEE_RESERVE if bad else float(getattr(cfg, 'fee_reserve', DEFAULT_FEE_RESERVE))
    store.write_effective({
        'at': dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec='seconds'),
        'live_allowed': live_allowed, 'mode': 'LIVE' if live else 'DRY_RUN',
        'targets': getattr(cfg, 'weights', None), 'band_rel': getattr(cfg, 'band_rel', None), 'min_order_usdt': min_order,
        'max_order_usdt': getattr(cfg, 'max_order_usdt', None), 'max_orders_per_day': getattr(cfg, 'max_orders_per_day', None),
        'max_daily_usdt': getattr(cfg, 'max_daily_usdt', None), 'stale_minutes': getattr(cfg, 'stale_minutes', None),
        'max_spread': getattr(cfg, 'max_spread', None), 'min_cash_usdt': getattr(cfg, 'min_cash_usdt', None),
        'allow_new_orders': not holds, 'holds': holds, 'control_error': ctl_err, 'market_specs': dict(MARKET_SPECS_META),
        'signer': getattr(client, 'signer', None) or ed25519.backend()})
    store.rotate_log()
    targets = cfg.weights if not bad else getattr(base_cfg, 'weights', {})
    managed = sorted(MARKETS)
    if st.get('day') != day:
        st.update(day=day, orders_today=0, notional_today=0.0, filled_orders_today=0, filled_usdt_today=0.0)
        store.ledger_compact(today(now - LEDGER_KEEP_DAYS * 86400))
    mine = st['bot_orders']
    closing = st.setdefault('closing', {})

    open_orders = []
    try:
        for a in managed:
            rows = client.open_orders(src=a, dst='usdt')
            if not isinstance(rows, list):
                raise NobitexError('open-order list is not a list', code='BAD_RESPONSE')
            for o in rows:
                if isinstance(o, dict):
                    open_orders.append(dict(o, _id=_order_id(o, a, mine), _asset=a))
    except NobitexError:
        # the lists are unreadable: STOP / dry mode still try to cancel what the bot knows it has
        if may_cancel and (store.stop_requested or not live):
            for oid in list(mine):
                _cancel_and_settle(client, store, st, mine, oid, day, 'exchange list unavailable; stop/dry')
            _cancel_unknown(client, store, closing, 'exchange list unavailable; stop/dry')
        store.save(st)
        raise

    # ---- 1. a placement whose reply was lost: found ONLY by its clientOrderId. Absence is not proof - a
    # list can lag - so an unresolved placement blocks new orders until it is found or the customer
    # resolves it (audit 1.1 P0, AVID-07).
    pend = st.get('pending')
    pending_blocks = False
    if pend:
        info = {k: v for k, v in pend.items() if k not in ('checks', 'alerted')}
        found, where = _lookup_pending(client, pend, open_orders, mine)
        if found is not None and not _matches_pending(found, pend):
            store.log('pending_mismatch', level='error', cid=pend.get('cid'), found_id=str(found.get('id')),
                      message='an order with the bot\'s clientOrderId does not match what was sent; it is NOT adopted')
            found = None
        if found is not None:
            oid = str(found['id'])
            st['pending'] = None
            store.log('adopted_after_error', id=oid, found_in=where, **_log_info(info))
            store.ledger_append({'t': 'outcome', 'cid': info.get('cid'), 'result': 'placed', 'id': oid, 'day': info.get('day')})
            if _final_status(found) is None:
                nums, _ = order_numbers(found, info)
                matched = nums[1] if nums else 0.0
                info['matched'] = matched
                mine[oid] = info
                ost.transition(store, info, _open_state(info, matched), oid=oid, reason=f'found in {where} lookup')
            else:
                closing[oid] = info                   # settled below from its final record
        else:
            pend['checks'] = int(pend.get('checks', 0)) + 1
            pending_blocks = True
            if pend['checks'] >= PENDING_ALERT_CHECKS and not pend.get('alerted'):
                pend['alerted'] = True
                store.log('pending_unresolved', level='error', cid=pend.get('cid'), asset=pend['asset'],
                          side=pend['side'], amount=pend['amount'], price=pend['price'],
                          message=f'a {pend["side"]} order of {pend["amount"]} {pend["asset"]} @ {pend["price"]} got no '
                                  'reply and is not visible on the exchange. No new orders until it is settled. Check '
                                  'the Nobitex panel (open AND recent orders); only if it does not exist, run  '
                                  f'avid-agent resolve --not-placed  (clientOrderId {pend.get("cid")})')
            # trading may not continue, but cancelling must: if the order is on the book after all, the
            # clientOrderId reaches it even without its exchange id
            if pend.get('cid') and (store.stop_requested or not live) and may_cancel and not pend.get('cancel_requested'):
                try:
                    client.cancel(client_order_id=pend['cid'])
                    pend['cancel_requested'] = True
                    ost.transition(store, pend, ost.CANCEL_REQUESTED, reason='stop/dry: cancel by clientOrderId')
                    store.log('pending_cancel_by_client_order_id', cid=pend['cid'], level='warning',
                              message='cancel by clientOrderId accepted: the order existed; it is settled from its '
                                      'final record (a cancel request is not a cancellation)')
                except Exception:
                    pass                              # not open / not found: stays unresolved
        store.save(st)

    # ---- 2. bot orders that left the open list: settle them from their final exchange record
    open_by_id = {o['_id']: o for o in open_orders}
    for oid in list(mine):
        if oid not in open_by_id:
            closing[oid] = mine.pop(oid)
            continue
        info = mine[oid]
        nums, why = order_numbers(open_by_id[oid], info)
        if nums is None:
            _alert_once(store, info, why, 'order_record_inconsistent', f'open order {oid}: {why}', level='warning', id=oid)
            continue
        m = nums[1]
        if m > float(info.get('matched', 0.0)) + 1e-12:
            info['matched'] = m
            store.log('order_progress', id=oid, asset=info['asset'], side=info['side'],
                      amount=info['amount'], price=info['price'], filled_amount=m)
        ost.transition(store, info, _open_state(info, info.get('matched', 0.0)), oid=oid, reason='seen in the open list')
    unsettled = []
    for oid in list(closing):
        res = _finalize(client, store, st, oid, closing[oid], day, reason='left the open-order list')
        if res is None:
            unsettled.append(oid)
            ci = closing[oid]
            if int(ci.get('status_errors', 0)) >= STATUS_ALERT_ERRORS and not ci.get('alerted'):
                ci['alerted'] = True
                store.log('order_status_unresolved', id=oid, level='error',
                          message=f'final state of bot order {oid} cannot be established. No new orders until it is '
                                  'settled. Check it in the Nobitex panel; if it is no longer open, run  '
                                  f'avid-agent resolve --closed {oid}')
        elif res.get('still_open'):
            info = closing.pop(oid)                   # listing lag: still open
            mine[oid] = info
            nums, _ = order_numbers(res['order'], info)
            ost.transition(store, info, _open_state(info, nums[1] if nums else info.get('matched', 0.0)), oid=oid,
                           reason='status says still open')
        else:
            closing.pop(oid)
    _reconcile_fills(client, store, st, now)
    store.save(st)

    # ---- 3. kill switch: cancel the bot's own orders, place nothing
    if store.stop_requested:
        kept = []
        for oid in list(mine):
            if may_cancel:
                res, ok = _cancel_and_settle(client, store, st, mine, oid, day, 'kill switch')
                store.log('kill_switch_cancel', id=oid, live=True, ok=ok)
                if not ok or oid in mine:
                    kept.append(oid)
            else:
                kept.append(oid)
        if may_cancel:
            _cancel_unknown(client, store, closing, 'kill switch')
        store.save(st)
        reason = f'kill switch {store.stop_source()} present'
        if kept:
            reason += (f'; bot order(s) {",".join(kept)} still open - '
                       + ('cancel not confirmed yet, retrying next cycle' if may_cancel
                          else 'this preview does not cancel; cancel them in the Nobitex panel'))
        return store.log('stopped', reason=reason)

    # ---- 4. trading not allowed (dry mode) or stale: cancel bot orders (all of them when not live)
    results, failed = [], []
    for oid in list(mine):
        info = mine[oid]
        age = now - info['placed_at']
        stale = age > float(getattr(cfg, 'stale_minutes', 30) if not bad else 30) * 60
        if not (stale or not live):
            continue
        if not may_cancel:
            continue
        res, ok = _cancel_and_settle(client, store, st, mine, oid, day,
                                     'stale' if live else 'trading disabled')
        if not ok:
            failed.append(oid)
        results.append((oid, res, stale, age))
    if not live and may_cancel:
        _cancel_unknown(client, store, closing, 'trading disabled')
    store.save(st)
    if results:
        oid, res, stale, age = results[0]
        return store.log('cancel_stale' if stale and live else 'cancel_mode_dry', id=oid,
                         age_min=round(age / 60, 1), live=True, ids=[r[0] for r in results],
                         final_status=(res or {}).get('status', 'unknown'),
                         cap_refund_usdt=sum((r[1] or {}).get('cap_refund_usdt', 0.0) for r in results),
                         failed=failed)

    if mine:
        oid = next(iter(mine))
        age = now - mine[oid]['placed_at']
        if not may_cancel:
            return store.log('waiting', reason=f'real bot order {oid} is still open ({age/60:.1f} min); this '
                             'preview never cancels - the running agent/bot manages it')
        return store.log('waiting', reason=f'bot order {oid} still open ({age/60:.1f} min)')
    if unsettled:
        return store.log('waiting', reason=f'final state of order(s) {",".join(unsettled)} not known yet; '
                         'no new orders until they are settled')
    if pending_blocks:
        return store.log('waiting', reason='an order whose placement reply was lost is not settled yet '
                         f'(lookup {st["pending"]["checks"]}); no new orders until it is found or resolved')

    foreign = [o for o in open_orders if o['_id'] not in mine and o['_asset'] in managed]
    if foreign:
        others = [o for o in foreign if str(o.get('clientOrderId') or '').startswith(CID_PREFIX)
                  and not str(o.get('clientOrderId')).startswith(CID_PREFIX + _instance_tag(st))]
        if others:
            _alert_once(store, st, ','.join(sorted(str(o.get('id')) for o in others)), 'other_avid_instance',
                        f'{len(others)} open order(s) carry another AVID instance\'s clientOrderId: two agents may be '
                        'trading this Nobitex account (only one may). No new orders while they are open.',
                        level='warning')
        return store.log('waiting', reason=f'{len(foreign)} non-bot open order(s) on '
                                           + ','.join(MARKETS[a]['symbol'] for a in sorted({o["_asset"] for o in foreign})))

    # ---- 5. plan one order (value EVERY managed asset - AVID-03)
    total, avail = _wallets(client, tuple(managed) + ('usdt',))
    need_price = [a for a in managed if total.get(a, 0.0) > 0 or float((targets or {}).get(a, 0.0) or 0.0) > 0]
    books, missing = {}, []
    for a in need_price:
        try:
            books[a] = client.best_bid_ask(MARKETS[a]['symbol'])
        except NobitexError:
            missing.append(a)
    info = {'balances': total, 'available': avail, 'books': books, 'targets': targets,
            'mode': 'LIVE' if live else 'DRY_RUN'}
    if missing:
        _alert_once(store, st, ','.join(missing), 'valuation_incomplete',
                    f'no valid price for {", ".join(missing)}: the portfolio value is incomplete; no orders until the '
                    'price is available again')
        store.save(st)
        return store.log('valuation_incomplete', level='warning', assets=missing, **info,
                         reason='valuation incomplete: ' + ', '.join(missing))
    _clear_alert(st, 'valuation_incomplete')
    if bad:
        store.save(st)
        return store.log('hold', **{**info, 'reason': f'invalid risk parameter: {bad}'})
    if float(cfg.max_order_usdt) < min_order:
        return _conflict(store, st, f'max order {cfg.max_order_usdt} USDT is below the exchange minimum '
                                    f'{min_order} USDT (Nobitex: more than 11 USDT per order on USDT markets); '
                                    'no order can be placed - raise the cap', info)
    try:
        p = plan_multi(balances=total, available=avail, books=books, targets=targets, managed=managed,
                       band_rel=cfg.band_rel, min_order_usdt=min_order, max_order_usdt=cfg.max_order_usdt,
                       max_spread=float(getattr(cfg, 'max_spread', 0.01)), fee_reserve=fee_reserve)
    except ValuationIncomplete as e:
        store.save(st)
        return store.log('valuation_incomplete', level='error', assets=e.assets, **info, reason=str(e))
    except ValueError as e:
        store.save(st)
        return store.log('plan_invalid', level='error', **info, reason=f'cannot plan: {str(e)[:200]}')
    store.save(st)
    info.update(asdict(p))
    if p.action == 'none':
        return store.log('no_trade', **info)
    checks = risk.pre_trade_checks(p, available_usdt=avail.get('usdt', 0.0), fee_reserve=fee_reserve,
                                   min_cash_usdt=float(getattr(cfg, 'min_cash_usdt', 0) or 0), state=st, day=day,
                                   max_orders_per_day=cfg.max_orders_per_day, max_daily_usdt=cfg.max_daily_usdt)
    info['risk_checks'] = [{'check': c.name, 'ok': c.ok, 'detail': c.detail} for c in checks]
    failed = risk.first_failure(checks)
    if failed is not None and failed.name == 'cash_reserve':
        return store.log('no_trade', **{**info, 'reason': f'min cash reserve {getattr(cfg, "min_cash_usdt", 0) or 0} USDT '
                                        f'(order {p.notional_usdt:.2f} + fee reserve {fee_reserve:.2%})'})
    if failed is not None:
        return store.log('cap_reached', cap=risk.CAP_MESSAGES[failed.name], **info)
    if not live:
        return store.log('dry_run_would_place', **info)
    if holds:
        return store.log('hold', **{**info, 'reason': holds[0]})
    spec = MARKETS[p.asset]
    signer = getattr(client, 'signer', None)
    if signer == 'pure' and not ed25519.pure_allowed(getattr(client, 'network', None)):
        return _conflict(store, st, 'live orders need a constant-time Ed25519 signer: install python3-cryptography '
                                    '(sudo apt install -y python3-cryptography) or pip install cryptography'
                                    + (f' (the installed one is broken: {ed25519.backend_error()})'
                                       if ed25519.backend_error() else '')
                                    + '. The pure-Python fallback signs only for simulation and the official testnet.',
                         info)
    if st.get('cid_supported') is not False and st.get('cid_verify'):
        v = _verify_cid(client, store, st)
        store.save(st)
        if v == 'unknown':
            return store.log('waiting', reason='clientOrderId support of this account is not verified yet (the lookup '
                                               'of the last order by its clientOrderId did not answer); no new order')
    if st.get('cid_supported') is False:
        return _conflict(store, st, 'clientOrderId is not enabled for this Nobitex account, so a lost reply could not '
                                    'be matched to its order with certainty. Live trading stays off (simulation and '
                                    'cancels still work). Ask Nobitex to enable clientOrderId, then run  avid-agent '
                                    'reset-cid', info)
    st.pop('last_conflict', None)
    entry = {'asset': p.asset, 'side': p.action, 'amount': p.amount, 'price': p.price, 'notional': p.notional_usdt,
             'placed_at': now, 'day': day, 'cid': _new_cid(st, now), 'matched': 0.0, 'state': None}
    # Count the caps and remember the attempt DURABLY BEFORE sending: if the reply is lost (timeout)
    # the order may still exist, and the next cycle reconciles it by its clientOrderId.
    ost.transition(store, entry, ost.SUBMISSION_PENDING, reason=p.reason)
    st['pending'] = entry
    st['orders_today'] += 1
    st['notional_today'] += p.notional_usdt
    store.ledger_append({'t': 'submit', 'cid': entry['cid'], 'day': day, 'asset': p.asset, 'side': p.action,
                         'amount': p.amount, 'price': p.price, 'notional': p.notional_usdt, 'at': now})
    store.save(st)

    def _rejected(code):
        st['pending'] = None
        st['notional_today'] = max(0.0, st['notional_today'] - p.notional_usdt)
        st['orders_today'] = max(0, st['orders_today'] - 1)   # refused, never on the book
        ost.transition(store, entry, ost.REJECTED_CONFIRMED, reason=f'exchange refused: {code}')
        store.ledger_append({'t': 'outcome', 'cid': entry['cid'], 'result': 'rejected', 'code': code, 'day': day})
        store.ledger_append({'t': 'final', 'cid': entry['cid'], 'state': ost.REJECTED_CONFIRMED, 'day': day})
        store.save(st)
    try:
        order = client.add_limit_order(p.action, p.amount, p.price, src=p.asset, dst='usdt',
                                       amount_decimals=decimals(spec['amount_step']),
                                       price_decimals=decimals(spec['price_tick']),
                                       client_order_id=entry['cid'])
    except NobitexError as e:
        if _definitely_rejected(e):               # exchange answered "no": nothing exists
            if getattr(e, 'code', None) == 'FeatureUnavailable':
                st['cid_supported'] = False       # experimental clientOrderId not enabled for this account
                store.log('client_order_id_unavailable', level='error',
                          message='clientOrderId is not enabled for this Nobitex account: live trading is turned off '
                                  'because without it a lost reply cannot be matched to its order with certainty')
            _rejected(getattr(e, 'code', None) or str(e)[:60])
        else:
            store.log('order_submit_unconfirmed', level='warning', cid=entry['cid'], asset=p.asset, side=p.action,
                      amount=p.amount, price=p.price,
                      message=f'no definite answer to the order ({str(e)[:120]}); it is looked up by clientOrderId '
                              'next cycle and nothing new is sent until it is settled')
            store.save(st)
        raise                                     # otherwise keep 'pending' for next cycle
    except ValueError:                            # refused by the client's own checks: never sent
        _rejected('LOCAL_VALIDATION')
        raise
    oid = str(order['id'])
    echoed = order.get('clientOrderId')
    if echoed not in (None, '', entry['cid']):
        st['cid_supported'] = False
        store.log('client_order_id_mismatch', level='error', id=oid, cid=entry['cid'], echoed=str(echoed)[:40],
                  message='the exchange answered with another clientOrderId: the order is tracked by its id, live '
                          'trading is turned off')
    mine[oid] = entry
    st['pending'] = None
    ost.transition(store, entry, ost.OPEN, oid=oid, reason='placement acknowledged')
    if st.get('cid_supported') is None:
        st['cid_verify'] = {'cid': entry['cid'], 'id': oid}
    store.ledger_append({'t': 'outcome', 'cid': entry['cid'], 'result': 'placed', 'id': oid, 'day': day})
    store.save(st)
    return store.log('order_placed', eid=f'placed:{oid}', id=oid, cid=entry['cid'], **info)


def main(argv=None):
    """Backward-compatible entry point (`python -m avid_core.cycle ...`): the CLI lives in cli.py."""
    from .cli import main as _main
    return _main(argv)


if __name__ == '__main__':
    sys.exit(main())
