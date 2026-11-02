"""Offline simulation: the REAL engine (cycle.cycle) against a deterministic in-process exchange.

    avid-engine simulate --example btc-usdt

Nothing here opens a network connection, reads credentials or touches a real account. The scenario
(start balances, targets, limits, and a stored price path) is plain JSON; the same file always gives
the same result. This is NOT the live dry run: `avid-engine plan` reads the real Nobitex account and
shows what the engine would do now; `simulate` replays synthetic prices through the same engine code.

Fill model (stated, simple and conservative): a resting bot BUY fills completely at its own price when
a LATER step's best ask is at or below that price; a SELL when a later best bid is at or above it.
Orders never fill in the step they are placed, there is no partial fill, no slippage, no queue
position and no outage. Fees are the scenario's assumed rate, not the exchange's fee schedule.
Results therefore show how the engine DECIDES; they say nothing about future returns.
"""
from __future__ import annotations

import datetime as dt
import math
import shutil
import tempfile
import types
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from importlib import resources
from pathlib import Path

from .client import NobitexError
from .rebalance import MARKETS, full_targets
from .validation import ValidationError, loads_strict, number

FORMAT = 'avid-sim-scenario/1'
EXAMPLES = ('btc-usdt', 'btc-paxg-usdt')
SIM_MARKER = 'SIMULATION'                 # written into a --root used by the simulator
MAX_STEPS = 10_000
SETTING_KEYS = ('band_rel', 'min_order_usdt', 'max_order_usdt', 'max_orders_per_day', 'max_daily_usdt',
                'stale_minutes', 'max_spread', 'min_cash_usdt', 'fee_reserve')
DEFAULT_SETTINGS = {'band_rel': 0.25, 'min_order_usdt': 11.5, 'max_order_usdt': 20.0, 'max_orders_per_day': 6,
                    'max_daily_usdt': 100.0, 'stale_minutes': 30.0, 'max_spread': 0.01, 'min_cash_usdt': 0.0,
                    'fee_reserve': 0.003}


class ScenarioError(ValueError):
    pass


def _D(x) -> Decimal:
    return Decimal(str(x))


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _err(name):
    return NobitexError(f'API error on simulated exchange: {name}', code=name)


# ------------------------------------------------------------------------------------------ exchange
class SimExchange:
    """Deterministic exchange with the ExchangeAdapter surface (models.ExchangeAdapter).

    Balances are Decimal; records use the Nobitex wire shapes (amounts as strings), so the engine
    parses them exactly as it parses real answers."""

    network = 'simulation'
    signer = 'none'                # nothing is signed: there is nothing to sign for
    auth_mode = 'simulation'

    def __init__(self, balances: dict, *, fee_rate: float, spread_rel: float, now: float, markets=None):
        self.markets = {a: dict(m) for a, m in (markets or MARKETS).items()}
        self.total = {a: Decimal(0) for a in list(self.markets) + ['usdt']}
        for a, v in balances.items():
            self.total[a] = _D(v)
        self.fee_rate = _D(fee_rate)
        self.spread_rel = _D(spread_rel)
        self.now = float(now)
        self.books: dict[str, tuple[float, float]] = {}
        self.orders: dict[int, dict] = {}
        self.trades: list[dict] = []
        self.next_id, self.next_trade = 1, 1
        self.deadline = None

    # ---- market (driven by the scenario runner)
    def set_mid(self, asset: str, mid: float):
        tick = _D(self.markets[asset]['price_tick'])
        m = _D(mid)
        half = m * self.spread_rel / 2
        bid = ((m - half) / tick).to_integral_value(ROUND_FLOOR) * tick
        ask = ((m + half) / tick).to_integral_value(ROUND_CEILING) * tick
        if ask <= bid:
            ask = bid + tick
        self.books[asset] = (float(bid), float(ask))

    def match(self) -> list[dict]:
        """Fill resting orders the current book has crossed (see the module docstring)."""
        fills = []
        for o in sorted(self.orders.values(), key=lambda x: x['id']):
            if o['status'] != 'Active' or o['created'] >= self.now:
                continue
            bid, ask = (_D(x) for x in self.books[o['src']])
            if (o['type'] == 'buy' and ask <= o['price']) or (o['type'] == 'sell' and bid >= o['price']):
                fills.append(self._fill(o))
        return fills

    def _fill(self, o):
        q = o['amount'] - o['matched']
        if o['type'] == 'buy':
            fee = q * self.fee_rate                          # paid in the asset received
            self.total['usdt'] -= q * o['price']
            self.total[o['src']] += q - fee
            fee_asset = o['src']
        else:
            fee = q * o['price'] * self.fee_rate
            self.total[o['src']] -= q
            self.total['usdt'] += q * o['price'] - fee
            fee_asset = 'usdt'
        o['matched'] += q
        o['fee'] += fee
        o['status'], o['closed'] = 'Done', self.now
        t = {'id': self.next_trade, 'orderId': o['id'], 'srcCurrency': o['src'], 'dstCurrency': 'usdt',
             'type': o['type'], 'amount': str(q), 'price': str(o['price']), 'fee': str(fee),
             'timestamp': _iso(self.now)}
        self.next_trade += 1
        self.trades.append(t)
        return {'order_id': o['id'], 'asset': o['src'], 'side': o['type'], 'amount': float(q),
                'price': float(o['price']), 'fee': float(fee), 'fee_asset': fee_asset, 'at': _iso(self.now)}

    # ---- adapter surface
    def server_time(self):
        return self.now

    def market_specs(self) -> dict:
        return {'markets': {a: dict(m) for a, m in self.markets.items()},
                'meta': {'source': 'simulation (the documented Nobitex values in rebalance.MARKETS)'}}

    def best_bid_ask(self, symbol='BTCUSDT'):
        for a, m in self.markets.items():
            if m['symbol'] == symbol:
                if a not in self.books:
                    raise _err('NoPrice')
                return self.books[a]
        raise _err('UnknownMarket')

    def _blocked(self):
        b = {k: Decimal(0) for k in self.total}
        for o in self.orders.values():
            if o['status'] == 'Active':
                un = o['amount'] - o['matched']
                if o['type'] == 'buy':
                    b['usdt'] += un * o['price']
                else:
                    b[o['src']] += un
        return b

    def wallets(self, currencies=('btc', 'usdt')):
        b = self._blocked()
        return {c: {'total': float(self.total.get(c, 0)), 'available': float(max(Decimal(0), self.total.get(c, Decimal(0))
                                                                               - b.get(c, Decimal(0))))}
                for c in currencies}

    def balances(self, currencies=('btc', 'usdt')):
        return {c: w['available'] for c, w in self.wallets(currencies).items()}

    def _rec(self, o):
        return {'id': o['id'], 'clientOrderId': o['cid'], 'type': o['type'], 'execution': 'Limit',
                'srcCurrency': o['src'], 'dstCurrency': 'usdt', 'amount': str(o['amount']),
                'matchedAmount': str(o['matched']), 'unmatchedAmount': str(o['amount'] - o['matched']),
                'price': str(o['price']), 'status': o['status'], 'fee': str(o['fee']),
                'averagePrice': str(o['price'] if o['matched'] > 0 else 0), 'created_at': _iso(o['created'])}

    def open_orders(self, src='btc', dst='usdt'):
        return [self._rec(o) for o in self.orders.values() if o['src'] == src and o['status'] == 'Active']

    def list_orders(self, src='btc', dst='usdt', status='open'):
        if status == 'open':
            return self.open_orders(src, dst)
        return [self._rec(o) for o in sorted(self.orders.values(), key=lambda o: -o['id']) if o['src'] == src][:100]

    def add_limit_order(self, side, amount, price, src='btc', dst='usdt', amount_decimals=6, price_decimals=2,
                        client_order_id=None):
        if side not in ('buy', 'sell') or src not in self.markets or dst != 'usdt':
            raise _err('ParseError')
        spec = self.markets[src]
        a, p = _D(amount), _D(price)
        if not (a.is_finite() and p.is_finite()) or a <= 0 or p <= 0:
            raise _err('ParseError')
        if a % _D(spec['amount_step']) or p % _D(spec['price_tick']):
            raise _err('InvalidPrecision')                  # the engine must send values on the grid
        if a * p <= _D(spec['min_notional']):
            raise _err('SmallOrder')
        if client_order_id and any(o['cid'] == client_order_id for o in self.orders.values()):
            raise _err('DuplicateClientOrderId')
        b = self._blocked()
        if side == 'buy' and a * p > self.total['usdt'] - b['usdt']:
            raise _err('InsufficientBalance')
        if side == 'sell' and a > self.total[src] - b[src]:
            raise _err('InsufficientBalance')
        o = {'id': self.next_id, 'cid': client_order_id, 'type': side, 'src': src, 'amount': a, 'price': p,
             'matched': Decimal(0), 'fee': Decimal(0), 'status': 'Active', 'created': self.now}
        self.next_id += 1
        self.orders[o['id']] = o
        return self._rec(o)

    def _find(self, order_id, client_order_id):
        if order_id is not None:
            return self.orders.get(int(order_id))
        return next((o for o in self.orders.values() if o['cid'] and o['cid'] == client_order_id), None)

    def order_status(self, order_id=None, *, client_order_id=None):
        o = self._find(order_id, client_order_id)
        if o is None:
            raise _err('NotFound')
        return self._rec(o)

    def cancel(self, order_id=None, *, client_order_id=None):
        o = self._find(order_id, client_order_id)
        if o is None:
            raise _err('NotFound')
        if o['status'] != 'Active':
            raise _err('InvalidOrderStatus')
        o['status'], o['closed'] = 'Canceled', self.now

    def trades_page(self, src='btc', dst='usdt', page=1):
        rows = [dict(t) for t in reversed(self.trades) if t['srcCurrency'] == src]
        return {'trades': rows[(page - 1) * 30: page * 30], 'hasNext': len(rows) > page * 30}


# ------------------------------------------------------------------------------------------ scenario
@dataclass
class Scenario:
    name: str
    title: str
    start: float
    interval_s: float
    balances: dict
    targets: dict
    settings: dict
    fee_rate: float
    spread_rel: float
    prices: dict                       # asset -> [mid, ...]
    notes: list = field(default_factory=list)

    @property
    def steps(self) -> int:
        return len(next(iter(self.prices.values())))


def _pos(name, v, *, allow_zero=False, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 \
            or (v == 0 and not allow_zero) or (hi is not None and v > hi):
        raise ScenarioError(f'{name} must be a finite number {">=" if allow_zero else ">"} 0'
                            + (f' and <= {hi}' if hi is not None else '') + f', got {v!r}')
    return float(v)


def parse_scenario(text: str) -> Scenario:
    try:
        d = loads_strict(text)
    except ValueError as e:
        raise ScenarioError(f'scenario is not valid JSON: {e}') from None
    if not isinstance(d, dict) or d.get('format') != FORMAT:
        raise ScenarioError(f'scenario must be an object with "format": "{FORMAT}"')
    try:
        start = dt.datetime.fromisoformat(str(d['start_time']).replace('Z', '+00:00'))
    except (KeyError, ValueError):
        raise ScenarioError('start_time must be an ISO-8601 UTC time, e.g. "2026-01-05T00:00:00Z"') from None
    if start.tzinfo is None:
        raise ScenarioError('start_time needs a time zone (use Z for UTC)')
    try:
        interval = number('interval_s', d.get('interval_s', 600))
        settings = dict(DEFAULT_SETTINGS)
        raw = d.get('settings') or {}
        if not isinstance(raw, dict) or set(raw) - set(SETTING_KEYS):
            raise ScenarioError(f'settings may only contain {", ".join(SETTING_KEYS)}')
        for k, v in raw.items():
            settings[k] = number(k, v)
        targets = full_targets(d.get('targets') or {}, sorted(MARKETS))
    except ValidationError as e:
        raise ScenarioError(str(e)) from None
    except ValueError as e:
        raise ScenarioError(str(e)) from None
    if settings['max_daily_usdt'] < settings['max_order_usdt']:
        raise ScenarioError('settings.max_daily_usdt must be at least settings.max_order_usdt')
    bal = d.get('balances')
    if not isinstance(bal, dict) or not bal or set(bal) - set(MARKETS) - {'usdt'}:
        raise ScenarioError(f'balances must name only {", ".join(sorted(MARKETS))} or usdt')
    balances = {k: _pos(f'balances.{k}', v, allow_zero=True) for k, v in bal.items()}
    ex = d.get('exchange') or {}
    if not isinstance(ex, dict):
        raise ScenarioError('exchange must be an object')
    fee = _pos('exchange.fee_rate', ex.get('fee_rate', 0.002), allow_zero=True, hi=0.05)
    spread = _pos('exchange.spread_rel', ex.get('spread_rel', 0.0004), hi=0.05)
    prices = d.get('prices')
    if not isinstance(prices, dict) or not prices or set(prices) - set(MARKETS):
        raise ScenarioError(f'prices must map some of {", ".join(sorted(MARKETS))} to lists of mid prices')
    lengths = {len(v) if isinstance(v, list) else -1 for v in prices.values()}
    if len(lengths) != 1 or min(lengths) < 1 or max(lengths) > MAX_STEPS:
        raise ScenarioError(f'every price list must have the same length, 1..{MAX_STEPS}')
    clean = {a: [_pos(f'prices.{a}[{i}]', x) for i, x in enumerate(v)] for a, v in prices.items()}
    for a in MARKETS:
        if (balances.get(a, 0) > 0 or targets.get(a, 0) > 0) and a not in clean:
            raise ScenarioError(f'{a} is held or targeted but has no price path')
    notes = d.get('notes') or []
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise ScenarioError('notes must be a list of strings')
    return Scenario(name=str(d.get('name') or 'custom')[:64], title=str(d.get('title') or '')[:200],
                    start=start.timestamp(), interval_s=interval, balances=balances, targets=targets,
                    settings=settings, fee_rate=fee, spread_rel=spread, prices=clean, notes=notes)


def load_example(name: str) -> Scenario:
    if name not in EXAMPLES:
        raise ScenarioError(f'unknown example {name!r}; available: {", ".join(EXAMPLES)}')
    text = resources.files('avid_core').joinpath('examples', f'{name}.json').read_text(encoding='utf-8')
    return parse_scenario(text)


def load_file(path) -> Scenario:
    p = Path(path)
    if not p.is_file() or p.stat().st_size > 8 * 1024 * 1024:
        raise ScenarioError(f'{p}: not a regular file of at most 8 MiB')
    return parse_scenario(p.read_text(encoding='utf-8'))


# ------------------------------------------------------------------------------------------ runner
def _prepare_root(root):
    """A private engine root for the run. A --root that holds anything but an earlier simulation is
    refused: simulated orders must never mix with the state of a real installation."""
    if root is None:
        return Path(tempfile.mkdtemp(prefix='avid-sim-')), True
    r = Path(root).expanduser().resolve()
    bot = r / 'data' / 'bot'
    if bot.exists() and any(bot.iterdir()) and not (bot / SIM_MARKER).is_file():
        raise ScenarioError(f'{r} already holds engine data that is not from a simulation; use an empty folder')
    if bot.exists():
        shutil.rmtree(bot)                           # an earlier simulation: start clean, results reproducible
    return r, False


def _value(total: dict, books: dict) -> tuple[float, dict]:
    vals = {'usdt': float(total.get('usdt', 0.0))}
    for a, (bid, ask) in books.items():
        if total.get(a, 0.0) > 0:
            vals[a] = float(total[a]) * (bid + ask) / 2
    v = sum(vals.values())
    return v, ({a: x / v for a, x in vals.items()} if v > 0 else {})


def decision_view(rec: dict, settings: dict, venue: str | None = None) -> dict:
    """The fields an operator reads to understand one engine decision (README 'Example')."""
    targets = rec.get('targets') or {}
    weights = rec.get('weights') or {}
    band = float(settings['band_rel'])
    side = str(rec.get('action') or '').upper()
    ev = rec.get('event')
    decision = {'order_placed': f'{side} order placed' + (f' on the {venue}' if venue else ''),
                'dry_run_would_place': f'{side} would be placed (dry run: nothing sent)',
                'no_trade': 'no trade', 'cap_reached': 'no trade (risk limit reached)',
                'hold': 'no trade (new orders on hold)', 'config_conflict': 'no trade (configuration conflict)',
                'valuation_incomplete': 'no trade (valuation incomplete)'}.get(ev, ev)
    out = {'event': rec.get('event'), 'decision': decision,
           'portfolio_value_usdt': round(float(rec['value_usdt']), 2) if rec.get('value_usdt') is not None else None,
           'current_weights': weights, 'target_weights': {a: round(float(w), 4) for a, w in targets.items()},
           'band_rel': band,
           'band': {a: [round(max(0.0, w - band * w), 4), round(min(1.0, w + band * w), 4)]
                    for a, w in targets.items() if w > 0},
           'order': None, 'risk_checks': rec.get('risk_checks') or [], 'reason': rec.get('reason') or rec.get('cap')}
    if rec.get('action') in ('buy', 'sell') and rec.get('amount'):
        out['order'] = {'side': rec['action'], 'asset': rec.get('asset'), 'market': rec.get('market'),
                        'amount': rec['amount'], 'price': rec['price'],
                        'notional_usdt': round(float(rec.get('notional_usdt', 0.0)), 2), 'type': 'limit (resting side)'}
    return out


def run(scn: Scenario, *, root=None, steps: int | None = None, keep: bool = False) -> dict:
    """Replay the scenario through cycle.cycle(). Returns a JSON-compatible report."""
    from . import cycle as engine                    # late import: cycle imports this package's modules
    from .store import Store
    n = scn.steps if steps is None else max(1, min(int(steps), scn.steps))
    sim = SimExchange(scn.balances, fee_rate=scn.fee_rate, spread_rel=scn.spread_rel, now=scn.start)
    cfg = types.SimpleNamespace(weights=dict(scn.targets), interval=scn.interval_s, **scn.settings)
    path, temporary = _prepare_root(root)
    try:
        store = Store(path)
        (store.dir / SIM_MARKER).write_text('Engine data written by avid-engine simulate. Not a real account.\n')
        start_val, start_w = None, None
        timeline, fills, first_decision, errors = [], [], None, 0
        counts = {'placed': 0, 'filled': 0, 'canceled': 0}
        for i in range(n):
            sim.now = scn.start + i * scn.interval_s
            for a, path_ in scn.prices.items():
                sim.set_mid(a, path_[i])
            if i == 0:
                start_val, start_w = _value({a: float(v) for a, v in sim.total.items()}, sim.books)
            new_fills = sim.match()
            fills.extend(new_fills)
            counts['filled'] += len(new_fills)
            try:
                rec = engine.cycle(sim, store, cfg, live=True, now=sim.now)
            except NobitexError as e:
                errors += 1
                rec = {'event': 'error', 'reason': str(e)[:200]}
            ev = rec.get('event')
            if ev == 'order_placed':
                counts['placed'] += 1
            if ev in ('cancel_stale', 'cancel_mode_dry'):
                counts['canceled'] += len(rec.get('ids') or [rec.get('id')])
            if first_decision is None and 'value_usdt' in rec:
                first_decision = dict(decision_view(rec, scn.settings, 'simulated exchange'), step=i + 1, at=_iso(sim.now))
            if new_fills or ev not in ('no_trade', 'waiting'):
                timeline.append({'step': i + 1, 'at': _iso(sim.now), 'event': ev,
                                 'fills': new_fills, 'summary': _summary(rec)})
        total = {a: float(v) for a, v in sim.total.items() if v != 0 or a in scn.balances}
        end_val, end_w = _value(total, sim.books)
        fees_usdt = sum(f['fee'] if f['fee_asset'] == 'usdt' else f['fee'] * f['price'] for f in fills)
        hold_val = sum(float(scn.balances.get(a, 0.0)) * sum(sim.books[a]) / 2 for a in sim.books) \
            + float(scn.balances.get('usdt', 0.0))
        report = {
            'kind': 'SIMULATION', 'scenario': scn.name, 'title': scn.title,
            'notice': 'Offline simulation with stored synthetic prices: no exchange, no network, no real order. '
                      'Shows how the engine decides; it is not a forecast or a profit guarantee.',
            'assumptions': {'fill_rule': 'resting order fills completely at its price once a later book crosses it',
                            'fee_rate': scn.fee_rate, 'spread_rel': scn.spread_rel, 'partial_fills': False,
                            'slippage': False, 'outages': False},
            'steps': n, 'interval_s': scn.interval_s, 'start': _iso(scn.start), 'end': _iso(sim.now),
            'settings': scn.settings, 'targets': scn.targets,
            'start_value_usdt_estimated': round(start_val, 2),
            'start_weights': {a: round(w, 4) for a, w in start_w.items()},
            'first_decision': first_decision,
            'orders': counts, 'errors': errors, 'fills': fills,
            'fees_usdt_realized': round(fees_usdt, 4),
            'end_balances': total,
            'end_value_usdt_estimated': round(end_val, 2),
            'end_weights': {a: round(w, 4) for a, w in end_w.items()},
            'hold_value_usdt_estimated': round(hold_val, 2),
            'timeline': timeline, 'notes': scn.notes,
            'exchange_orders': [{'id': o['id'], 'side': o['type'], 'asset': o['src'], 'amount': float(o['amount']),
                                 'price': float(o['price']), 'notional_usdt': float(o['amount'] * o['price']),
                                 'filled': float(o['matched']), 'status': o['status'], 'placed_at': _iso(o['created']),
                                 'closed_at': _iso(o['closed']) if o.get('closed') is not None else None,
                                 # the clientOrderId itself is random by design (unique per order); its
                                 # length is reported so the output stays reproducible
                                 'client_order_id_len': len(o['cid'] or '')} for o in sim.orders.values()],
            'engine_root': None if temporary and not keep else str(path)}
        return report
    finally:
        if temporary and not keep:
            shutil.rmtree(path, ignore_errors=True)


def _summary(rec: dict) -> str:
    ev = rec.get('event')
    if ev == 'order_placed':
        return f'{rec.get("action")} {rec.get("amount")} {rec.get("asset")} @ {rec.get("price")} ' \
               f'({float(rec.get("notional_usdt", 0)):.2f} USDT) - {rec.get("reason")}'
    if ev in ('cancel_stale', 'cancel_mode_dry'):
        return f'cancelled bot order(s) {",".join(map(str, rec.get("ids") or []))} ({rec.get("final_status")})'
    if ev == 'cap_reached':
        return f'limit reached: {rec.get("cap")}'
    return str(rec.get('reason') or rec.get('message') or '')[:200]
