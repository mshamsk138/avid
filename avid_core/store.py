"""Durable engine state: state.json, the engine log and the submission ledger (report 10.1, AVID-04).

state.json is written atomically with fsync and validated strictly on read; the log is append-only with
an event id and a severity on every record; submissions.jsonl is the ledger that survives a corrupt or
deleted state.json (_recover_from_ledger).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import secrets
import time
import uuid
from pathlib import Path

from . import fsio
from . import orderstate as ost
from .events import level_of
from .rebalance import MARKETS
from .reconcile import R_PENDING
from .records import _f
from .validation import finite_nonneg, loads_strict

_DAY_RE = re.compile(r'\d{4}-\d{2}-\d{2}')


def _check_order_info(info, where):
    if not isinstance(info, dict):
        raise ValueError(f'{where}: order record is not an object')
    for k in ('amount', 'price'):
        v = info.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise ValueError(f'{where}: {k} invalid')
    finite_nonneg(f'{where}.placed_at', info.get('placed_at', 0.0))
    finite_nonneg(f'{where}.matched', info.get('matched', 0.0))


def check_state(st) -> dict:
    """Raise ValueError unless the state file has the expected shape and only finite numbers."""
    if not isinstance(st, dict) or not isinstance(st.get('bot_orders'), dict):
        raise ValueError('bad shape')
    for k in ('closing', 'fills_pending'):
        if k in st and not isinstance(st[k], dict):
            raise ValueError(f'{k} is not an object')
    if st.get('pending') is not None:
        _check_order_info(st['pending'], 'pending')
    for k in ('orders_today', 'filled_orders_today', 'seq'):
        v = st.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f'{k} invalid')
    for k in ('notional_today', 'filled_usdt_today'):
        finite_nonneg(k, st.get(k, 0.0))
    for k in ('day', 'locked_day'):
        v = st.get(k)
        if v is not None and not (isinstance(v, str) and _DAY_RE.fullmatch(v)):
            raise ValueError(f'{k} invalid')
    if st.get('last_now') is not None:
        finite_nonneg('last_now', st['last_now'])
    for group in ('bot_orders', 'closing'):
        for oid, info in (st.get(group) or {}).items():
            _check_order_info(info, f'{group}[{oid}]')
    for oid, p in (st.get('fills_pending') or {}).items():
        if not isinstance(p, dict):
            raise ValueError(f'fills_pending[{oid}] invalid')
        for k in ('filled', 'recorded', 'since'):
            finite_nonneg(f'fills_pending[{oid}].{k}', p.get(k, 0.0))
    return st


class Store:
    LOG_MAX_BYTES = 20 * 1024 * 1024
    LOG_GENERATIONS = 3

    def __init__(self, root):
        self.root = Path(root)
        self.dir = fsio.private_dir(Path(root) / 'data/bot')
        self.state_path = self.dir / 'state.json'
        self.log_path = self.dir / 'log.jsonl'
        self.ledger_path = self.dir / 'submissions.jsonl'
        self._log_repaired = False

    @staticmethod
    def fresh():
        # orders_today   orders SUBMITTED today (never decreases when one is cancelled)
        # notional_today committed today = filled + still reserved by open orders (a confirmed
        #                cancel releases only the unfilled part; unknown outcomes stay counted)
        return {'bot_orders': {}, 'day': None, 'orders_today': 0, 'notional_today': 0.0,
                'filled_orders_today': 0, 'filled_usdt_today': 0.0, 'pending': None, 'closing': {},
                'cid_supported': None, 'cid_verify': None, 'seq': 0, 'seen_trades': [], 'fills_pending': {},
                'instance': None, 'last_now': None, 'control_seen': False, 'last_valid_control': None}

    @classmethod
    def upgrade(cls, st):
        for k, v in cls.fresh().items():
            st.setdefault(k, v)
        return st

    def _missing(self):
        st = self.fresh()
        if self.ledger_read():
            st['state_missing'] = True                    # it existed: the day's caps are in the ledger
        return st

    def load(self):
        try:
            text = self.state_path.read_text()
        except FileNotFoundError:                         # also when it vanishes between two checks
            return self._missing()
        try:
            st = check_state(loads_strict(text))
            return self.upgrade(st)
        except (ValueError, UnicodeDecodeError) as e:    # includes ValidationError and JSONDecodeError
            stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            aside = self.dir / f'state.corrupt-{stamp}-{secrets.token_hex(2)}.json'
            self.ledger_append({'t': 'state_corrupt', 'moved_to': aside.name, 'at': time.time()})
            try:
                self.state_path.replace(aside)
            except FileNotFoundError:
                pass
            self.log('state_corrupt', level='error', moved_to=aside.name, message=f'state.json unusable: {str(e)[:120]}')
            st = self.fresh()
            st['recovered_from_corrupt'] = True
            return st

    def save(self, st):
        fsio.atomic_write(self.state_path, json.dumps(st, indent=1, allow_nan=False, default=str))

    def peek(self) -> dict:
        """Read the state WITHOUT side effects (a corrupt file is left where it is): for status displays
        and 'is anything open?' checks. The engine itself always uses load()."""
        try:
            return self.upgrade(check_state(loads_strict(self.state_path.read_text())))
        except (OSError, ValueError, UnicodeDecodeError):
            return self.fresh()

    def rotate_log(self):
        """Keep the engine log bounded: at 20 MB it becomes log.jsonl.1 (.1 -> .2 -> .3; three previous
        files are kept). A reader tracks the file identity (device, inode), not only an offset (AVID-04)."""
        try:
            if self.log_path.stat().st_size <= self.LOG_MAX_BYTES:
                return
        except FileNotFoundError:
            return
        for i in range(self.LOG_GENERATIONS - 1, 0, -1):
            src = self.log_path.with_suffix(f'.jsonl.{i}')
            if src.exists():
                os.replace(src, self.log_path.with_suffix(f'.jsonl.{i + 1}'))
        os.replace(self.log_path, self.log_path.with_suffix('.jsonl.1'))
        fsio.fsync_dir(self.dir)

    def log(self, event, *, eid=None, level=None, **kw):
        rec = {'at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
               'event': event, 'eid': eid or uuid.uuid4().hex, 'level': level_of(event, level), **kw}
        fsio.append_line(self.log_path, json.dumps(rec, default=str), durable=True, repair=not self._log_repaired)
        self._log_repaired = True
        return rec

    @property
    def stop_requested(self):
        return (self.dir / 'STOP').exists() or (self.dir / 'STOP.cloud').exists()

    def stop_source(self):
        return ' + '.join(n for n in ('STOP', 'STOP.cloud') if (self.dir / n).exists())

    def write_effective(self, obj):
        fsio.atomic_write(self.dir / 'effective.json', json.dumps(obj, indent=2, default=str), durable=False)

    # ---- durable submission ledger (survives a lost state.json)
    def ledger_append(self, rec):
        fsio.append_line(self.ledger_path, json.dumps(rec, default=str, allow_nan=False), durable=True)

    def ledger_read(self) -> list:
        try:
            text = self.ledger_path.read_text()
        except (FileNotFoundError, UnicodeDecodeError):
            return []
        out = []
        for line in text.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
        return out

    def ledger_compact(self, keep_since_day: str):
        recs = self.ledger_read()
        done = {r.get('cid') for r in recs if r.get('t') == 'final'}
        reconciled = {str(r.get('id')) for r in recs if r.get('t') == 'reconciled'}
        open_accounting = {str(r.get('id')) for r in recs if r.get('t') == 'final' and (r.get('filled') or 0) > 0
                           and str(r.get('id')) not in reconciled}
        keep = [r for r in recs if str(r.get('day', '')) >= keep_since_day or r.get('cid') not in done
                or str(r.get('id')) in open_accounting]
        if len(keep) != len(recs):
            fsio.atomic_write(self.ledger_path, ''.join(json.dumps(r, default=str) + '\n' for r in keep))


class PreviewStore(Store):
    """For `plan`: reads the shared state but never writes state, log or status, so a
    preview run next to the service cannot disturb it. It never cancels or places orders."""
    read_only = True

    def save(self, st):
        pass

    def log(self, event, *, eid=None, level=None, **kw):
        return {'at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
                'event': event, 'level': level_of(event, level), 'preview': True, **kw}

    def write_effective(self, obj):
        pass

    def rotate_log(self):
        pass

    def ledger_append(self, rec):
        pass

    def ledger_compact(self, keep_since_day):
        pass

    def load(self):                                   # never moves a corrupt file aside
        st = self.fresh()
        if not self.state_path.exists():
            if self.ledger_read():
                st['state_missing'] = True            # state lost: a preview never claims the caps are free
            return st
        try:
            return self.upgrade(check_state(loads_strict(self.state_path.read_text())))
        except (OSError, ValueError, UnicodeDecodeError):
            st['recovered_from_corrupt'] = True       # unreadable state: same (the file is left untouched)
            return st


def _recover_from_ledger(store, st, day) -> str:
    """state.json was corrupt or deleted: rebuild what the durable ledger knows (conservatively).
    The cap day becomes the latest day the ledger knows (never earlier than `day`), and the latest
    submission time restores the clock guard, so a wrong clock cannot reopen a day. Returns the day."""
    recs = store.ledger_read()
    known_days = [r['day'] for r in recs if isinstance(r.get('day'), str) and _DAY_RE.fullmatch(r['day'])]
    day = max([day] + known_days)
    stamps = [float(r['at']) for r in recs if r.get('t') == 'submit' and isinstance(r.get('at'), (int, float))
              and not isinstance(r.get('at'), bool) and math.isfinite(r['at'])]
    if stamps:
        st['last_now'] = max(float(st.get('last_now') or 0.0), max(stamps))
    subs, outcome, final_c, final_i, finals, reconciled = {}, {}, set(), set(), {}, set()
    for r in recs:
        t = r.get('t')
        if t == 'submit' and r.get('cid'):
            subs[r['cid']] = r
        elif t == 'outcome' and r.get('cid'):
            outcome[r['cid']] = r
        elif t == 'final':
            if r.get('cid'):
                final_c.add(r['cid'])
            if r.get('id') is not None:
                final_i.add(str(r['id']))
                finals[str(r['id'])] = r
        elif t == 'reconciled' and r.get('id') is not None:
            reconciled.add(str(r['id']))
    for oid, r in finals.items():                     # executed orders whose accounting was not closed yet
        filled = _f(r.get('filled'), 0.0) or 0.0
        if filled <= 0 or oid in reconciled or oid in st.get('fills_pending', {}) or r.get('asset') not in MARKETS:
            continue
        st.setdefault('fills_pending', {})[oid] = {
            'asset': r['asset'], 'side': r.get('side'), 'filled': filled, 'avg_price': _f(r.get('avg_price'), 0.0) or 0.0,
            'fee': _f(r.get('fee'), 0.0) or 0.0, 'amount': _f(r.get('amount'), filled) or filled,
            'since': _f(r.get('since'), 0.0) or 0.0, 'recorded': 0.0, 'trade_ids': [], 'attempts': 0,
            'status': R_PENDING, 'pagination_complete': False, 'last_reconciliation_at': None}
    counted = [s for s in subs.values() if s.get('day') == day and outcome.get(s['cid'], {}).get('result') != 'rejected']
    st['day'] = day
    st['orders_today'] = max(int(st.get('orders_today', 0)), len(counted))
    st['notional_today'] = max(float(st.get('notional_today', 0.0)),
                               sum(_f(s.get('notional'), 0.0) or 0.0 for s in counted))
    restored = []
    for cid, s in subs.items():
        o = outcome.get(cid)
        if cid in final_c or (o and str(o.get('id')) in final_i) or (o and o.get('result') != 'placed'):
            continue
        try:
            info = {'asset': s['asset'], 'side': s['side'], 'amount': float(s['amount']), 'price': float(s['price']),
                    'placed_at': float(s.get('at') or 0.0), 'day': s.get('day'), 'cid': cid, 'matched': 0.0,
                    'state': ost.UNKNOWN}
        except (KeyError, TypeError, ValueError):
            continue
        if o is None:
            info['state'] = ost.SUBMISSION_PENDING
            st['pending'] = info
        else:
            st.setdefault('closing', {})[str(o.get('id'))] = info
        restored.append(cid)
    store.log('state_recovered_from_ledger', level='error', restored_orders=restored,
              orders_today=st['orders_today'], notional_today=round(st['notional_today'], 4),
              message='state.json was unusable or missing: the day\'s counters and unsettled orders were restored '
                      'from the submission ledger; no new orders for the rest of this UTC day')
    return day
