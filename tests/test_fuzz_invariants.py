"""Randomised state-machine fuzzing of the engine against a misbehaving simulated exchange.

Each run drives `cycle()` through thousands of random steps: price moves and spread blow-outs, partial and
full fills, foreign (customer) orders, lost placement replies, network errors on every call, listing
lag, unrecognised status words, delayed cancels, STOP and dry/live switches, a clock that goes back,
restarts and (rarely) a corrupted or deleted state file. After EVERY step the invariants of the audit
prompt (I1-I7) and of the report (A, B) are checked against the exchange's TRUTH:

  I1/B  never two bot orders open at once; no placement while a bot order is open on the exchange
  I2    orders created per UTC day <= max_orders_per_day
  I3    filled + still-open notional of the orders created per UTC day <= max_daily_usdt
  I4    no order the bot did not create is ever cancelled
  I5    every bot execution is reported exactly once (one event id per trade id) or explicitly flagged
        AGGREGATE_ONLY / INCOMPLETE after the history window
  I6    with STOP or dry mode no new order is created
  I7    every bot order carries a clientOrderId
  A     the bot's committed notional is never below the truth (nothing released without confirmation)

AVID_FUZZ_STEPS (default 2500) and AVID_FUZZ_SEEDS (default 4) scale the run; the audit run used
10 000 steps x 4 seeds (see docs/audits/AUDIT_RESPONSE_0.1.0a1_FA.md).
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import random
from pathlib import Path

import pytest

from fakes import FakeExchange, api_error, make_cfg, net_error
from avid_core import control
from avid_core.client import NobitexError
from avid_core.cycle import TRADE_HISTORY_S, EngineBusy, Store, cycle

T0 = 1_700_000_000.0
STEPS = int(os.environ.get('AVID_FUZZ_STEPS', '2500'))
SEEDS = int(os.environ.get('AVID_FUZZ_SEEDS', '4'))


def _day(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%d')


class Model:
    def __init__(self, seed, root: Path):
        self.rng = r = random.Random(seed)
        self.root = root
        self.ex = FakeExchange(usdt=r.uniform(60, 400), btc=r.choice([0.0, 0.0005, 0.002]),
                               paxg=r.choice([0.0, 0.0, 0.01]), now=T0)
        self.cfg = make_cfg(max_orders_per_day=r.choice([2, 3, 6]), max_daily_usdt=r.choice([30.0, 45.0, 100.0]),
                            max_order_usdt=20.0, stale_minutes=r.choice([20, 30, 60]))
        self.skew = 0.0
        self.violations: list[str] = []
        self.step_no = 0
        self.stop = False
        self.mode = 'live'
        self.created_at_add: dict[int, float] = {}
        self._wrap_add()
        self.write_control()

    # ------------------------------------------------------------ harness
    def _wrap_add(self):
        orig = self.ex.add_limit_order

        def add(*a, **kw):
            if self.ex.open_bot_orders():
                self.violations.append(f'step {self.step_no}: I1/B placement while bot order(s) '
                                       f'{[o["id"] for o in self.ex.open_bot_orders()]} open')
            if self.stop or self.mode == 'dry':
                self.violations.append(f'step {self.step_no}: I6 placement under STOP/dry')
            before = set(self.ex.orders)
            try:
                return orig(*a, **kw)
            finally:
                for oid in set(self.ex.orders) - before:
                    self.created_at_add[oid] = self.ex.now
        self.ex.add_limit_order = add

    def write_control(self):
        r = self.rng
        w = r.choice([{'btc': 0.5, 'usdt': 0.5}, {'btc': 0.34, 'paxg': 0.33, 'usdt': 0.33}, {'btc': 0.7, 'usdt': 0.3},
                      {'paxg': 0.5, 'usdt': 0.5}, {'usdt': 1.0}, {'btc': 0.2, 'paxg': 0.2, 'usdt': 0.6}])
        ctl = {'mode': self.mode, 'weights': w, 'band_rel': r.choice([0.05, 0.1, 0.25])}
        if r.random() < 0.3:
            ctl['max_order_usdt'] = r.choice([12.0, 15.0, 20.0])
        control.save(self.root, ctl)

    def now(self):
        return self.ex.now + self.skew

    def inject(self):
        r, ex = self.rng, self.ex
        if r.random() < 0.15:
            ex.faults.append(('add', r.choice(['lose_reply', 'lose_reply_fill', 'network_before', 'not_sent',
                                               'reject:InsufficientBalance', 'reject:SmallOrder', 'fill_now'])))
        for m in ('order_status', 'open_orders', 'list_orders', 'trades_page', 'cancel', 'best_bid_ask', 'wallets'):
            if r.random() < 0.04:
                ex.faults.append((m, net_error(m)))
        if r.random() < 0.02:
            ex.faults.append(('cancel', api_error('InvalidOrderStatus')))
        bots = [o['id'] for o in ex.bot_orders()]
        if bots and r.random() < 0.08:
            ex.hide_open.add(r.choice(bots))
        if bots and r.random() < 0.05:
            ex.hide_all.add(r.choice(bots))
        if bots and r.random() < 0.06:
            ex.status_patch[r.choice(bots)] = r.choice([{'status': 'Expiring'}, {'matchedAmount': None,
                                                        'unmatchedAmount': None}, {'status': 'Done'},
                                                        {'matchedAmount': '-1'}])
        if bots and r.random() < 0.05:
            ex.status_fail.add(r.choice(bots))
        if bots and r.random() < 0.05:
            ex.cancel_lag.add(r.choice(bots))

    def heal(self, full=False):
        r, ex = self.rng, self.ex
        for s in (ex.hide_open, ex.hide_all, ex.status_fail, ex.cancel_lag):
            for x in list(s):
                if full or r.random() < 0.5:
                    s.discard(x)
        for k in list(ex.status_patch):
            if full or r.random() < 0.4:
                ex.status_patch.pop(k)
        if full:
            ex.faults.clear()
        ex.apply_cancels()

    def customer_resolves_now(self, pend):
        import inspect
        from avid_core.cycle import ManualResolution, resolve_manually
        kw = {'client': self.ex, 'confirm_cid': pend['cid'], 'now': self.now() + 3600}
        if 'client' not in inspect.signature(resolve_manually).parameters:
            kw = {}
        try:
            resolve_manually(Store(self.root), pending_not_placed=True, **kw)
        except (ManualResolution, NobitexError):
            pass

    def customer_resolves(self):
        """Sometimes the customer looks at the panel and settles a lost placement that really does not
        exist (exactly what `avid-agent resolve --not-placed` is for)."""
        if self.rng.random() > 0.3:
            return
        try:
            st = Store(self.root).load()
        except Exception:
            return
        pend = st.get('pending')
        if pend and int(pend.get('checks', 0)) >= 3 and not any(o['cid'] == pend.get('cid')
                                                                 for o in self.ex.orders.values()):
            import inspect
            from avid_core.cycle import ManualResolution, resolve_manually
            kw = {'client': self.ex, 'confirm_cid': pend['cid'], 'now': self.now() + 3600}
            if 'client' not in inspect.signature(resolve_manually).parameters:     # v1.2 (AVID_SRC runs)
                kw = {}
            try:
                resolve_manually(Store(self.root), pending_not_placed=True, **kw)
            except (ManualResolution, NobitexError):
                pass

    def run_cycle(self):
        try:
            cycle(self.ex, Store(self.root), self.cfg, live=True, now=self.now())
        except (NobitexError, EngineBusy):
            pass
        except Exception as e:                        # anything else escaping a cycle is a bug
            self.violations.append(f'step {self.step_no}: unexpected {type(e).__name__}: {e}')

    def action(self):
        r, ex = self.rng, self.ex
        a = r.choices(['cycle', 'market', 'fill', 'foreign', 'foreign_done', 'stop', 'mode', 'time', 'skew',
                       'corrupt'], weights=[40, 10, 12, 3, 3, 3, 4, 20, 2, 0.4])[0]
        if a == 'cycle':
            self.inject()
            self.run_cycle()
            self.heal()
            self.customer_resolves()
        elif a == 'market':
            for asset, (b, k) in list(ex.books.items()):
                mid = (b + k) / 2 * (1 + r.uniform(-0.02, 0.02))
                spread = r.choice([0.0002, 0.001, 0.003, 0.03])
                ex.books[asset] = (round(mid * (1 - spread / 2), 2), round(mid * (1 + spread / 2), 2))
        elif a == 'fill':
            opens = [o for o in ex.orders.values() if o['status'] == 'Active']
            if opens:
                o = r.choice(opens)
                ex.fill(o['id'], None if r.random() < 0.5 else (o['amount'] - o['matched']) * r.uniform(0.1, 0.9))
        elif a == 'foreign':
            src = r.choice(['btc', 'paxg'])
            ex.place_foreign(r.choice(['buy', 'sell']), src, 0.0002 if src == 'btc' else 0.005,
                             ex.books[src][0] * 0.9)
        elif a == 'foreign_done':
            for o in ex.orders.values():
                if o['foreign'] and o['status'] == 'Active' and r.random() < 0.7:
                    o['status'] = 'Canceled'
        elif a == 'stop':
            self.stop = (not self.stop) if self.stop else r.random() < 0.5
            p = Store(self.root).dir / 'STOP'
            if self.stop:
                p.write_text('stop')
            elif p.exists():
                p.unlink()
        elif a == 'mode':
            self.mode = r.choice(['live', 'live', 'dry'])
            self.write_control()
        elif a == 'time':
            ex.now += r.choice([60, 300, 600, 600, 1800, 3600, 6 * 3600])
        elif a == 'skew':
            self.skew = 0.0 if self.skew else -r.choice([600, 3600, 86400, 2 * 86400])
        elif a == 'corrupt':
            p = Store(self.root).state_path
            if p.exists():
                if r.random() < 0.5:
                    p.write_text('{"bot_orders": {}, "notional_today": NaN')
                else:
                    p.unlink()

    # ------------------------------------------------------------ invariants
    def check(self):
        ex, v = self.ex, self.violations
        bots = ex.bot_orders()
        if len(ex.open_bot_orders()) > 1:
            v.append(f'step {self.step_no}: I1 {len(ex.open_bot_orders())} bot orders open')
        for oid, foreign in ex.cancels:
            if foreign:
                v.append(f'step {self.step_no}: I4 foreign order {oid} cancelled')
        ex.cancels = [c for c in ex.cancels if not c[1]]
        for o in bots:
            if not o['cid']:
                v.append(f'step {self.step_no}: I7 order {o["id"]} without clientOrderId')
        by_day = collections.defaultdict(list)
        for o in bots:
            by_day[_day(self.created_at_add.get(o['id'], o['created']))].append(o)
        for day, orders in by_day.items():
            if len(orders) > self.cfg.max_orders_per_day:
                v.append(f'step {self.step_no}: I2 {len(orders)} orders created on {day}')
            committed = sum((o['matched'] + ((o['amount'] - o['matched']) if o['status'] == 'Active' else 0)) * o['price']
                            for o in orders)
            if committed > self.cfg.max_daily_usdt + 1e-6:
                v.append(f'step {self.step_no}: I3 {committed:.4f} USDT committed on {day}')
        try:
            st = Store(self.root).load() if Store(self.root).state_path.exists() else None
        except Exception:
            st = None
        if st and st.get('day'):
            truth = sum((o['matched'] + ((o['amount'] - o['matched']) if o['status'] == 'Active' else 0)) * o['price']
                        for o in by_day.get(st['day'], []))
            if st['notional_today'] + 1e-6 < truth:
                v.append(f'step {self.step_no}: A bot counts {st["notional_today"]:.4f} committed, truth {truth:.4f}')

    def events(self):
        out = []
        for p in sorted(Store(self.root).dir.glob('log.jsonl*'), reverse=True):
            for line in p.read_text().splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out

    def settle_and_check_reporting(self):
        """Quiet phase: no faults, no STOP, dry mode; every bot execution must end up reported."""
        self.heal(full=True)
        self.skew = 0.0
        self.stop = False
        p = Store(self.root).dir / 'STOP'
        if p.exists():
            p.unlink()
        self.mode = 'dry'                                # no new orders; the bot's own orders get cancelled
        control.save(self.root, {'mode': 'dry', 'weights': {'usdt': 1.0}, 'band_rel': 0.9})
        self.ex.now += 2 * 86400                         # a fresh day: caps free, nothing locked
        for _ in range(40):
            for o in self.ex.open_bot_orders():
                self.ex.fill(o['id'])
            self.run_cycle()
            self.check()
            st = Store(self.root).load()
            if not (st['bot_orders'] or st.get('pending') or st.get('closing') or st.get('fills_pending')):
                break
            pend = st.get('pending')
            if pend and not any(o['cid'] == pend.get('cid') for o in self.ex.orders.values()):
                # the customer checks the panel: the order really does not exist -> resolve (as the CLI does)
                self.rng.random()
                self.customer_resolves_now(pend)
            self.ex.now += 600
        self.ex.now += TRADE_HISTORY_S + 3600            # let any unreconcilable order reach its final status
        for _ in range(3):
            self.run_cycle()
        events = self.events()
        eids = collections.defaultdict(set)
        for e in events:
            if e.get('event') == 'trade':
                eids[str(e.get('trade_id'))].add(e.get('eid'))
        for tid, s in eids.items():
            if len(s) != 1:
                self.violations.append(f'I5 trade {tid} reported under {len(s)} event ids')
        flagged = {str(e.get('id')) for e in events if e.get('event') in ('fill_aggregate', 'reconciliation_incomplete')}
        bot_ids = {o['id'] for o in self.ex.bot_orders()}
        for t in self.ex.trades:
            if t['orderId'] in bot_ids and str(t['id']) not in eids and str(t['orderId']) not in flagged:
                self.violations.append(f'I5 execution {t["id"]} of bot order {t["orderId"]} never reported or flagged')
        st = Store(self.root).load()
        if st.get('pending') or st.get('closing'):
            self.violations.append(f'end: unresolved pending={bool(st.get("pending"))} closing={list(st.get("closing"))}')


@pytest.mark.parametrize('seed', range(SEEDS))
def test_engine_invariants_hold_under_random_faults(tmp_path, seed):
    m = Model(1000 + seed, tmp_path)
    for i in range(STEPS):
        m.step_no = i
        m.action()
        m.check()
        if len(m.violations) > 20:
            break
    m.settle_and_check_reporting()
    placed = len(m.ex.bot_orders())
    assert not m.violations, '\n'.join(m.violations[:20])
    assert placed > 0 or STEPS < 2000, 'the fuzzer never got the engine to place an order (weak run)'
