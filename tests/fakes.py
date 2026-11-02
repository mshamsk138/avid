"""Simulated Nobitex exchange and AVID Cloud for tests and fuzzing. Never touches the network.

FakeExchange keeps the TRUTH (every order, fill and cancel) so a test can check invariants against
what really happened on the "exchange", not against what the bot believes. Faults are injected per
call: lost replies, network errors, listing lag, unknown or inconsistent status records, delayed
cancels and immediate fills.
"""
from __future__ import annotations

import copy
import datetime as dt

from avid_core.client import NobitexError

SYMBOL_ASSET = {'BTCUSDT': 'btc', 'PAXGUSDT': 'paxg'}
KINDS = ('snapshots', 'intents', 'orders', 'fills', 'events')


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()


def net_error(where='call'):
    return NobitexError(f'NETWORK error on {where}: TimeoutError: timed out', code='NETWORK')


def api_error(code, where='call'):
    return NobitexError(f'API error on {where}: {code}: refused', code=code)


class FakeExchange:
    auth_mode = 'api_key'
    network = 'fake'
    signer = 'fake'                            # what signs requests; the engine refuses only 'pure' on mainnet

    def __init__(self, *, usdt=100.0, btc=0.0, paxg=0.0, books=None, fee=0.0035, now=1_700_000_000.0):
        self.total = {'usdt': float(usdt), 'btc': float(btc), 'paxg': float(paxg)}
        self.books = dict(books or {'btc': (60000.0, 60010.0), 'paxg': (2400.0, 2401.0)})
        self.fee = fee
        self.now = now
        self.orders: dict[int, dict] = {}
        self.trades: list[dict] = []
        self.next_id = 1000
        self.next_trade = 1
        self.calls: list[tuple] = []
        self.faults: list[tuple] = []          # (method, action) consumed in order
        self.hide_open: set[int] = set()        # listing lag: not in the open list
        self.hide_all: set[int] = set()         # not in list(all) either
        self.status_patch: dict[int, dict] = {}
        self.status_fail: set[int] = set()      # order_status(id) raises a network error
        self.cancel_lag: set[int] = set()       # cancel accepted, applied by apply_cancels()
        self.cid_feature = True
        self.cancels: list[tuple] = []          # (id, foreign?)
        self.add_attempts = 0                   # add_limit_order calls that reached this object

    # ------------------------------------------------------------ helpers
    def _fault(self, method):
        for i, (m, action) in enumerate(self.faults):
            if m == method:
                self.faults.pop(i)
                return action
        return None

    def _raise_if(self, method):
        a = self._fault(method)
        if isinstance(a, BaseException):
            raise a
        return a

    def _rec(self, o):
        un = max(0.0, o['amount'] - o['matched'])
        r = {'id': o['id'], 'clientOrderId': o['cid'], 'type': o['type'], 'execution': 'Limit',
             'srcCurrency': o['src'], 'dstCurrency': 'usdt', 'amount': repr(o['amount']),
             'matchedAmount': repr(o['matched']), 'unmatchedAmount': repr(un), 'price': repr(o['price']),
             'status': o['status'], 'fee': repr(o['fee']),
             'averagePrice': repr(o['price'] if o['matched'] > 0 else 0.0), 'created_at': _iso(o['created'])}
        return r

    def bot_orders(self):
        return [o for o in self.orders.values() if not o['foreign']]

    def open_bot_orders(self):
        return [o for o in self.bot_orders() if o['status'] == 'Active']

    def server_time(self):
        return self.now

    def market_specs(self):
        from avid_core.rebalance import MARKET_SPECS_META, MARKETS
        return {'markets': {a: dict(m) for a, m in MARKETS.items()}, 'meta': dict(MARKET_SPECS_META)}

    # ------------------------------------------------------------ public API (NobitexClient-like)
    def best_bid_ask(self, symbol='BTCUSDT'):
        self.calls.append(('best_bid_ask', symbol))
        self._raise_if('best_bid_ask')
        return self.books[SYMBOL_ASSET[symbol]]

    def _blocked(self):
        b = {k: 0.0 for k in self.total}
        for o in self.orders.values():
            if o['status'] != 'Active':
                continue
            un = o['amount'] - o['matched']
            if o['type'] == 'buy':
                b['usdt'] += un * o['price']
            else:
                b[o['src']] += un
        return b

    def balances(self, currencies=('btc', 'usdt')):
        self.calls.append(('balances',))
        self._raise_if('balances')
        b = self._blocked()
        return {c: max(0.0, self.total.get(c, 0.0) - b.get(c, 0.0)) for c in currencies}

    def wallets(self, currencies=('btc', 'usdt')):
        self.calls.append(('wallets',))
        self._raise_if('wallets')
        b = self._blocked()
        return {c: {'total': self.total.get(c, 0.0), 'available': max(0.0, self.total.get(c, 0.0) - b.get(c, 0.0))}
                for c in currencies}

    def open_orders(self, src='btc', dst='usdt'):
        self.calls.append(('open_orders', src))
        self._raise_if('open_orders')
        return [self._rec(o) for o in self.orders.values()
                if o['src'] == src and o['status'] == 'Active' and o['id'] not in self.hide_open]

    def list_orders(self, src='btc', dst='usdt', status='open'):
        if status == 'open':
            return self.open_orders(src, dst)
        self.calls.append(('list_orders', src, status))
        self._raise_if('list_orders')
        rows = [self._rec(o) for o in sorted(self.orders.values(), key=lambda o: -o['id'])
                if o['src'] == src and o['id'] not in self.hide_all and o['id'] not in self.hide_open]
        return rows[:100]

    def add_limit_order(self, side, amount, price, src='btc', dst='usdt', amount_decimals=6, price_decimals=2,
                        client_order_id=None):
        self.calls.append(('add_limit_order', side, amount, price, src, client_order_id))
        self.add_attempts += 1
        action = self._raise_if('add')
        if action == 'not_sent':
            raise NobitexError('NETWORK error on /market/orders/add: gaierror', code='NOT_SENT')
        if action == 'network_before':
            raise net_error('/market/orders/add')
        if isinstance(action, str) and action.startswith('reject:'):
            raise api_error(action.split(':', 1)[1], '/market/orders/add')
        amount = round(float(amount), amount_decimals)
        price = round(float(price), price_decimals)
        if amount <= 0 or price <= 0:
            raise api_error('ParseError')
        if amount * price <= 11:
            raise api_error('SmallOrder')
        if client_order_id and any(o['cid'] == client_order_id and o['status'] == 'Active'
                                   for o in self.orders.values()):
            raise api_error('DuplicateClientOrderId')
        avail = self.balances(currencies=('usdt', src))
        if (side == 'buy' and amount * price > avail['usdt'] + 1e-9) or (side == 'sell' and amount > avail[src] + 1e-12):
            raise api_error('InsufficientBalance')
        oid = self.next_id
        self.next_id += 1
        o = {'id': oid, 'cid': client_order_id, 'type': side, 'src': src, 'amount': amount, 'price': price,
             'matched': 0.0, 'status': 'Active', 'created': self.now, 'foreign': False, 'fee': 0.0}
        self.orders[oid] = o
        if action == 'fill_now':
            self.fill(oid)
        if action == 'lose_reply':
            raise net_error('/market/orders/add')
        if action == 'lose_reply_fill':
            self.fill(oid)
            raise net_error('/market/orders/add')
        return self._rec(o)

    def order_status(self, order_id=None, *, client_order_id=None):
        self.calls.append(('order_status', order_id, client_order_id))
        self._raise_if('order_status')
        if order_id is not None:
            oid = int(order_id)
            if oid in self.status_fail:
                raise net_error('/market/orders/status')
            o = self.orders.get(oid)
        else:
            if not self.cid_feature:
                raise api_error('FeatureUnavailable', '/market/orders/status')
            o = next((x for x in self.orders.values() if x['cid'] and x['cid'] == client_order_id), None)
        if o is None:
            raise api_error('NotFound', '/market/orders/status')
        r = self._rec(o)
        r.update(self.status_patch.get(o['id'], {}))
        return r

    def cancel(self, order_id=None, *, client_order_id=None):
        self.calls.append(('cancel', order_id, client_order_id))
        self._raise_if('cancel')
        if order_id is not None:
            o = self.orders.get(int(order_id))
        else:
            o = next((x for x in self.orders.values() if x['cid'] and x['cid'] == client_order_id), None)
        if o is None:
            raise api_error('NotFound', '/market/orders/update-status')
        self.cancels.append((o['id'], o['foreign']))
        if o['status'] != 'Active':
            raise api_error('InvalidOrderStatus', '/market/orders/update-status')
        if o['id'] in self.cancel_lag:
            o['cancel_pending'] = True
            return None
        o['status'] = 'Canceled'
        return None

    def trades_page(self, src='btc', dst='usdt', page=1):
        self.calls.append(('trades_page', src, page))
        self._raise_if('trades_page')
        rows = [t for t in reversed(self.trades) if t['srcCurrency'] == src]
        chunk = rows[(page - 1) * 30: page * 30]
        return {'trades': [dict(t) for t in chunk], 'hasNext': len(rows) > page * 30}

    def trades(self, src='btc', dst='usdt'):
        return self.trades_page(src, dst)['trades']

    # ------------------------------------------------------------ the "market" (test controls)
    def fill(self, oid, amount=None, *, at=None):
        o = self.orders[int(oid)]
        rem = o['amount'] - o['matched']
        q = rem if amount is None else min(rem, float(amount))
        if q <= 0:
            return
        if (o['type'] == 'buy' and q * o['price'] > self.total['usdt'] + 1e-9) or \
                (o['type'] == 'sell' and q > self.total[o['src']] + 1e-12):
            return                                   # the account cannot pay for it (foreign order): no fill
        o['matched'] = round(o['matched'] + q, 12)
        t = self.now if at is None else at
        if o['type'] == 'buy':
            fee = q * self.fee
            self.total['usdt'] -= q * o['price']
            self.total[o['src']] += q - fee
        else:
            fee = q * o['price'] * self.fee
            self.total[o['src']] -= q
            self.total['usdt'] += q * o['price'] - fee
        o['fee'] += fee
        self.trades.append({'id': self.next_trade, 'orderId': o['id'], 'srcCurrency': o['src'], 'dstCurrency': 'usdt',
                            'type': o['type'], 'amount': repr(q), 'price': repr(o['price']), 'fee': repr(fee),
                            'timestamp': _iso(t)})
        self.next_trade += 1
        if o['matched'] >= o['amount'] - 1e-12:
            o['status'] = 'Done'

    def apply_cancels(self):
        for o in self.orders.values():
            if o.pop('cancel_pending', False) and o['status'] == 'Active':
                o['status'] = 'Canceled'

    def place_foreign(self, side, src, amount, price, cid=None):
        oid = self.next_id
        self.next_id += 1
        self.orders[oid] = {'id': oid, 'cid': cid, 'type': side, 'src': src, 'amount': amount, 'price': price,
                            'matched': 0.0, 'status': 'Active', 'created': self.now, 'foreign': True, 'fee': 0.0}
        return oid

    def foreign_trade(self, src='btc', amount=0.0001, price=60000.0, at=None):
        self.trades.append({'id': self.next_trade, 'orderId': 999_999, 'srcCurrency': src, 'dstCurrency': 'usdt',
                            'type': 'buy', 'amount': repr(amount), 'price': repr(price), 'fee': '0',
                            'timestamp': _iso(self.now if at is None else at)})
        self.next_trade += 1


class FakeCloud:
    """AVID Cloud stand-in. Answers in the v2 sync contract (and also puts the directive at the top
    level, the shape a v1.2 agent reads, so the same fake drives both versions in the findings tests)."""

    def __init__(self, agent_id='agent-test', directive=None):
        self.agent_id = agent_id
        self.directive = directive if directive is not None else {'global_halt': False, 'portfolio': None}
        self.reports: list[dict] = []
        self.fail: BaseException | None = None
        self.respond = None            # callable(report) -> response, overrides the default
        self.accept = True

    def sync(self, report):
        self.reports.append(copy.deepcopy(report))
        if self.fail is not None:
            raise self.fail
        if self.respond is not None:
            return self.respond(report)
        keys = [it.get('item_key') for k in KINDS for it in (report.get(k) or []) if it.get('item_key')]
        resp = {'protocol': 2, 'agent_id': self.agent_id, 'batch_id': report.get('batch_id'),
                'ack': {'accepted': keys if self.accept else [], 'rejected': []},
                'directive': copy.deepcopy(self.directive)}
        resp.update(copy.deepcopy(self.directive))
        return resp


def policy(**kw):
    p = {'version_id': 'pol-1', 'weights': {'btc': 0.5, 'usdt': 0.5}, 'band_rel': 0.25, 'max_order_usdt': 20,
         'max_orders_per_day': 6, 'max_daily_usdt': 100, 'min_cash_usdt': 0, 'max_spread': 0.01}
    p.update(kw)
    return p


def directive(mode='live', *, kill=False, halt=False, pol='default'):
    return {'global_halt': halt, 'portfolio': {'name': 'p1', 'mode': mode, 'kill_switch': kill,
                                              'policy': policy() if pol == 'default' else pol}}


def make_cfg(**kw):
    import types
    d = dict(weights={'btc': 0.5, 'paxg': 0.0, 'usdt': 0.5}, band_rel=0.25, min_order_usdt=11.5, max_order_usdt=20.0,
             max_orders_per_day=6, max_daily_usdt=100.0, stale_minutes=30, max_spread=0.01, min_cash_usdt=0.0,
             fee_reserve=0.003)
    d.update(kw)
    return types.SimpleNamespace(**d)


LIVE_CEILINGS = {'live_allowed': True, 'max_order_usdt': 20, 'max_orders_per_day': 6, 'max_daily_usdt': 100}
