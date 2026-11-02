"""Minimal Nobitex REST client. Credentials are read from files, never printed or logged.

Two auth modes for private endpoints:
  token    Authorization: Token <token>
  api key  Nobitex-Key / Nobitex-Timestamp / Nobitex-Signature, where the signature is
           Ed25519 over  timestamp + METHOD + path(+query) + body  (base64url).
The signed body is byte-for-byte the body sent. Unexpected responses raise.

Transport rules (audit P0-1, P0-2, P1-26):
- only the official host (or the official testnet host, opt-in) is ever contacted;
- HTTP redirects are NEVER followed: any 3xx is an error, so credentials cannot be carried
  to another host;
- endpoints use the documented methods (wallets / order list / trade list are GET with a
  query string; order add / status / update-status are POST with a JSON body);
- a local per-endpoint rate limiter keeps calls under the documented limits; HTTP 429 honours
  the exchange's `backOff`; only read-only calls are retried (exponential backoff + jitter);
  order placement is never retried here (the engine reconciles a lost reply instead).
- NOBITEX_NETWORK must be empty, 'mainnet' or 'testnet'; anything else is an error (a typo such as
  'testnett' used to mean PRODUCTION, audit 43);
- replies larger than 2 MB, non-JSON, NaN/Infinity, duplicate keys or deep nesting are BAD_RESPONSE;
  balances and order books are checked (finite, >= 0, bid < ask) before anything uses them (audit 40, 42).
"""
from __future__ import annotations
import base64
import email.utils
import json
import math
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from . import __version__, ed25519, fsio
from .validation import ValidationError, loads_strict

BASE_URL = 'https://apiv2.nobitex.ir'
TESTNET_URL = 'https://testnetapiv2.nobitex.ir'          # documented in the Nobitex API FAQ
ALLOWED_BASES = (BASE_URL, TESTNET_URL)
NETWORKS = {'': BASE_URL, 'mainnet': BASE_URL, 'production': BASE_URL, 'testnet': TESTNET_URL}
UA = f'TraderBot/avid-{__version__}'   # format recommended by Nobitex API docs
MAX_RESPONSE_BYTES = 2_000_000

# Documented limits (requests, seconds). The local limiter stays a little below them.
RATE_LIMITS = {
    '/users/wallets/list': (18, 120),
    '/market/orders/list': (27, 60),
    '/market/trades/list': (27, 60),
    '/market/orders/status': (250, 60),
    '/market/orders/update-status': (80, 60),
    '/market/orders/add': (250, 600),
    'orderbook': (50, 60),
}
READ_ONLY = {'/users/wallets/list', '/market/orders/list', '/market/trades/list', '/market/orders/status',
             'orderbook'}


class NobitexError(RuntimeError):
    def __init__(self, msg, *, code: str | None = None, http_status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(msg)
        self.code, self.http_status, self.retry_after = code, http_status, retry_after


def default_base_url() -> str:
    """NOBITEX_NETWORK: '' / 'mainnet' -> production, 'testnet' -> the official testnet, anything else
    is refused (never silently production)."""
    name = os.environ.get('NOBITEX_NETWORK', '').strip().lower()
    if name not in NETWORKS:
        raise NobitexError(f'NOBITEX_NETWORK={name[:20]!r} is not valid: use mainnet or testnet', code='CONFIG')
    return NETWORKS[name]


def network_name(base_url: str) -> str:
    return 'testnet' if base_url == TESTNET_URL else 'mainnet'


def _read_secret_file(p: Path, what: str, allow_multiline=False) -> str:
    """Read through one O_NOFOLLOW descriptor (fsio.read_private). NOBITEX_ALLOW_LOOSE_TOKEN_PERMS=1
    (for read-only container secrets, mode 0444) relaxes only the READ bits: a file that others can
    write, a symlink or a file owned by another user is still refused."""
    loose = os.environ.get('NOBITEX_ALLOW_LOOSE_TOKEN_PERMS') == '1'
    try:
        val = fsio.read_private(p, what, allow_shared_read=loose).strip()
    except (OSError, UnicodeDecodeError) as e:
        raise NobitexError(str(e)[:200]) from None
    if not val or (not allow_multiline and any(c.isspace() for c in val)):
        raise NobitexError(f'{what} file is empty or malformed')
    return val


def read_token(path) -> str:
    p = Path(path)
    if not p.exists():
        raise NobitexError(f'token file not found: {p}')
    return _read_secret_file(p, 'token')


def read_credentials(directory) -> dict:
    """api_key + secret files -> API-key mode; else token file -> token mode."""
    d = Path(directory)
    key, sec, tok = d / 'api_key', d / 'secret', d / 'token'
    if key.exists() and sec.exists():
        seed = ed25519.parse_private_key(_read_secret_file(sec, 'secret', allow_multiline=True))
        return {'mode': 'api_key', 'api_key': _read_secret_file(key, 'api_key'), 'seed': seed}
    if tok.exists():
        return {'mode': 'token', 'token': _read_secret_file(tok, 'token')}
    raise NobitexError(f'no credentials in {d}: need api_key + secret files, or a token file')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib would otherwise re-send our headers to the new location."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def no_redirect_opener():
    """An opener that raises HTTPError for any 3xx instead of following it."""
    return urllib.request.build_opener(_NoRedirect()).open


def _num(x) -> float:
    """A finite float from an exchange field (numbers often arrive as strings)."""
    if isinstance(x, bool):
        raise ValueError('boolean is not a number')
    v = float(x)
    if not math.isfinite(v):
        raise ValueError('non-finite number')
    return v


def fmt_decimal(x, step, rounding=ROUND_DOWN) -> str:
    """Exact decimal text of x on the given step (amounts round DOWN, never above the balance)."""
    q = Decimal(str(step)).normalize()
    v = Decimal(str(x)).quantize(q, rounding=rounding)
    return format(v, 'f')


class _Limiter:
    def __init__(self, clock, sleep):
        self.clock, self.sleep = clock, sleep
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def wait(self, key):
        lim = RATE_LIMITS.get(key)
        if not lim:
            return
        n, window = lim
        while True:
            with self.lock:
                now = self.clock()
                q = self.hits.setdefault(key, deque())
                while q and now - q[0] >= window:
                    q.popleft()
                if len(q) < n:
                    q.append(now)
                    return
                delay = window - (now - q[0]) + 0.01
            self.sleep(min(delay, window))


class NobitexClient:
    def __init__(self, token: str | None = None, *, api_key: str | None = None,
                 seed: bytes | None = None, opener=None, timeout=15.0, base_url: str | None = None,
                 clock=time.time, sleep=time.sleep, retries=2, monotonic=time.monotonic):
        base_url = base_url or default_base_url()
        if base_url not in ALLOWED_BASES:
            raise ValueError('client restricted to the official Nobitex hosts')
        self.base_url = base_url
        self.network = network_name(base_url)
        self._token = token
        self._api_key, self._seed, self._clock = api_key, seed, clock
        self.auth_mode = 'api_key' if (api_key and seed) else ('token' if token else 'none')
        self.opener = opener or no_redirect_opener()
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep
        self._mono = monotonic
        self._limiter = _Limiter(monotonic, sleep)
        # optional time budget (monotonic seconds): after it no request is SENT any more (controlled stop).
        # A refused request raises code NOT_SENT - which is exactly what it is.
        self.deadline: float | None = None

    def __repr__(self):
        return f'<NobitexClient auth={self.auth_mode} credentials=***>'

    @property
    def signer(self) -> str:
        """'cryptography' | 'pure' (API-key mode) or 'none' (token mode: nothing is signed)."""
        return ed25519.backend() if self.auth_mode == 'api_key' else 'none'

    def market_specs(self) -> dict:
        """Market specs in force (documented values or the operator override, see rebalance.MARKETS)."""
        from .rebalance import MARKET_SPECS_META, MARKETS
        return {'markets': {a: dict(m) for a, m in MARKETS.items()}, 'meta': dict(MARKET_SPECS_META)}

    def _note_server_time(self, headers):
        """Remember the exchange's clock (HTTP Date header, over TLS) to detect a wrong local clock."""
        try:
            date = headers.get('Date') if headers is not None else None
            if date:
                self._server_time = (email.utils.parsedate_to_datetime(date).timestamp(), time.monotonic())
        except (TypeError, ValueError, IndexError, OverflowError):
            pass

    def server_time(self) -> float | None:
        """The exchange's current time estimated from the last response, or None."""
        st = getattr(self, '_server_time', None)
        if not st:
            return None
        return st[0] + (time.monotonic() - st[1])

    # ------------------------------------------------------------ transport
    def _call(self, method, path, *, body=None, query=None, private=False):
        key = 'orderbook' if path.startswith('/v3/orderbook/') else path
        attempts = 1 + (self.retries if key in READ_ONLY else 0)
        for i in range(attempts):
            self._check_deadline(path)
            self._limiter.wait(key)
            self._check_deadline(path)
            try:
                return self._once(method, path, body=body, query=query, private=private)
            except NobitexError as e:
                last = i == attempts - 1
                transient = e.http_status in (429, 500, 502, 503, 504) or (e.http_status is None and e.code in ('NETWORK', 'NOT_SENT'))
                if last or not transient:
                    raise
                base = e.retry_after if e.retry_after else min(30.0, 1.5 * 2 ** i)
                delay = min(60.0, base + random.uniform(0, 0.5))
                if self.deadline is not None and self._mono() + delay >= self.deadline:
                    raise                                   # no time left for another attempt
                self._sleep(delay)
        raise AssertionError('unreachable')

    def _check_deadline(self, path):
        if self.deadline is not None and self._mono() >= self.deadline:
            raise NobitexError(f'time budget used up: {path} was not sent', code='NOT_SENT')

    def _once(self, method, path, *, body=None, query=None, private=False):
        path_q = path + ('?' + urllib.parse.urlencode(query) if query else '')
        url = self.base_url + path_q
        headers = {'User-Agent': UA, 'Accept': 'application/json'}
        data, body_str = None, ''
        if body is not None:
            body_str = json.dumps(body, separators=(',', ':'), ensure_ascii=False)
            data = body_str.encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if private:
            if self.auth_mode == 'api_key':
                ts = str(int(self._clock()))
                msg = f'{ts}{method.upper()}{path_q}{body_str}'.encode('utf-8')
                headers['Nobitex-Key'] = self._api_key
                headers['Nobitex-Timestamp'] = ts
                headers['Nobitex-Signature'] = base64.urlsafe_b64encode(
                    ed25519.sign(self._seed, msg)).decode('ascii')
            elif self.auth_mode == 'token':
                headers['Authorization'] = 'Token ' + self._token
            else:
                raise NobitexError('private call without credentials')
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener(req, timeout=self.timeout) as res:
                status = getattr(res, 'status', None) or getattr(res, 'code', 200)
                final = res.geturl() if hasattr(res, 'geturl') else url
                if (status and 300 <= int(status) < 400) or (final and not str(final).startswith(self.base_url + '/')):
                    raise NobitexError(f'redirect refused on {path} (credentials are never sent elsewhere)',
                                       code='REDIRECT', http_status=int(status or 0))
                raw = res.read(MAX_RESPONSE_BYTES + 1)
                self._note_server_time(getattr(res, 'headers', None))
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                raise NobitexError(f'redirect refused on {path}: HTTP {e.code} (credentials are never sent '
                                   'elsewhere)', code='REDIRECT', http_status=e.code) from None
            detail, code, back = '', None, None
            try:
                detail = e.read(2000).decode('utf8', 'replace')
                j = json.loads(detail)
                if isinstance(j, dict):
                    code = j.get('code')
                    back = max(0.0, min(300.0, _num(j['backOff']))) if j.get('backOff') is not None else None
            except Exception:
                pass
            if back is None and e.code == 429:
                try:
                    back = max(0.0, min(300.0, _num(e.headers.get('Retry-After'))))
                except (TypeError, ValueError, AttributeError):
                    back = None
            raise NobitexError(f'HTTP {e.code} on {path} (auth={self.auth_mode}): {detail[:300]}',
                               code=code, http_status=e.code, retry_after=back) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, 'reason', e)
            # DNS failure or a refused connection: the request certainly never left this machine.
            # Anything else (timeout, reset, TLS error mid-way) may have reached the exchange.
            not_sent = isinstance(reason, (socket.gaierror, ConnectionRefusedError))
            raise NobitexError(f'NETWORK error on {path}: {type(e).__name__}: {str(reason)[:200]}',
                               code='NOT_SENT' if not_sent else 'NETWORK') from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise NobitexError(f'response on {path} larger than {MAX_RESPONSE_BYTES} bytes', code='BAD_RESPONSE')
        try:
            payload = loads_strict(raw)
        except (ValidationError, UnicodeDecodeError) as e:
            raise NobitexError(f'unusable response on {path}: {str(e)[:120]}', code='BAD_RESPONSE') from None
        if not isinstance(payload, dict) or payload.get('status') != 'ok':
            msg = payload.get('message') or payload.get('code') if isinstance(payload, dict) else payload
            code = payload.get('code') if isinstance(payload, dict) else None
            raise NobitexError(f'API error on {path}: {code + ": " if code else ""}{str(msg)[:300]}', code=code)
        return payload

    def _get_or_post(self, path, params):
        """Documented as GET with a query string. A 405 (older server behaviour) falls back to the
        same read-only call as POST once; nothing else is retried with another method."""
        try:
            return self._call('GET', path, private=True, query=params or None)
        except NobitexError as e:
            if e.http_status != 405:
                raise
        return self._call('POST', path, private=True, body=params)

    # ------------------------------------------------------------ public
    def best_bid_ask(self, symbol='BTCUSDT'):
        p = self._call('GET', f'/v3/orderbook/{symbol}')
        try:
            bids = [_num(b[0]) for b in p.get('bids') or []]
            asks = [_num(a[0]) for a in p.get('asks') or []]
        except (TypeError, ValueError, IndexError, KeyError):
            raise NobitexError(f'unreadable order book for {symbol}', code='BAD_RESPONSE') from None
        if not bids or not asks:
            raise NobitexError(f'empty order book for {symbol}', code='BAD_RESPONSE')
        bid, ask = max(bids), min(asks)
        if not (0 < bid < ask):
            raise NobitexError(f'crossed or zero order book for {symbol} (bid {bid}, ask {ask})', code='BAD_RESPONSE')
        return bid, ask

    # ------------------------------------------------------------ private
    def wallets(self, currencies=('btc', 'usdt')) -> dict:
        """{currency: {'total', 'available'}} - total values the portfolio (AVID-03: blocked funds still
        belong to it), available sizes orders. Accepts both known wallet response shapes; any number
        that is not finite and >= 0, or available > total, is a BAD_RESPONSE (audit 40)."""
        p = self._get_or_post('/users/wallets/list', {})
        wallets = p.get('wallets')
        out = {}
        try:
            if isinstance(wallets, list):
                for w in wallets:
                    cur = str(w.get('currency', '')).lower()
                    if cur in currencies:
                        total = _num(w['balance'])
                        if w.get('activeBalance') is not None:
                            avail = _num(w['activeBalance'])
                        else:
                            avail = total - _num(w.get('blockedBalance') or 0)
                        out[cur] = (total, avail)
            elif isinstance(wallets, dict):
                for k, w in wallets.items():
                    cur = str(k).lower()
                    if cur in currencies:
                        total = _num(w['balance'])
                        out[cur] = (total, total - _num(w.get('blocked') or 0))
            else:
                raise NobitexError('unexpected wallets response shape; keys: ' + ','.join(sorted(p.keys())),
                                   code='BAD_RESPONSE')
        except (TypeError, ValueError, KeyError, AttributeError):
            raise NobitexError('unreadable wallet record', code='BAD_RESPONSE') from None
        res = {}
        for c in currencies:
            total, avail = out.get(c, (0.0, 0.0))
            if total < 0 or avail < -1e-12 or avail > total + max(1e-9, 1e-9 * total):
                raise NobitexError(f'inconsistent {c} balance (total {total}, available {avail})', code='BAD_RESPONSE')
            res[c] = {'total': total, 'available': max(0.0, avail)}
        return res

    def balances(self, currencies=('btc', 'usdt')) -> dict:
        """Available (not blocked) balances."""
        return {c: w['available'] for c, w in self.wallets(currencies).items()}

    def list_orders(self, src='btc', dst='usdt', status='open') -> list:
        p = self._get_or_post('/market/orders/list', {'status': status, 'srcCurrency': src,
                                                      'dstCurrency': dst, 'details': 2})
        # details=2 includes id, status, fee, averagePrice
        orders = p.get('orders')
        if not isinstance(orders, list):
            raise NobitexError('unexpected orders-list response')
        return orders

    def open_orders(self, src='btc', dst='usdt') -> list:
        return self.list_orders(src, dst, 'open')

    def add_limit_order(self, side, amount, price, src='btc', dst='usdt',
                        amount_decimals=6, price_decimals=2, client_order_id: str | None = None) -> dict:
        if side not in ('buy', 'sell'):
            raise ValueError(side)
        for name, v in (('amount', amount), ('price', price)):
            if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)) or not Decimal(str(v)).is_finite() \
                    or Decimal(str(v)) <= 0:
                raise ValueError(f'order {name} must be a finite number > 0')
        body = {'type': side, 'execution': 'limit', 'srcCurrency': src, 'dstCurrency': dst,
                'amount': fmt_decimal(amount, Decimal(1).scaleb(-amount_decimals), ROUND_DOWN),
                'price': fmt_decimal(price, Decimal(1).scaleb(-price_decimals), ROUND_HALF_EVEN)}
        if Decimal(body['amount']) <= 0 or Decimal(body['price']) <= 0:
            raise ValueError('order amount or price rounds to zero; nothing sent')
        if client_order_id:
            if not (isinstance(client_order_id, str) and 0 < len(client_order_id) <= 32 and client_order_id.isalnum()):
                raise ValueError('clientOrderId must be 1..32 letters/digits')
            body['clientOrderId'] = client_order_id
        p = self._call('POST', '/market/orders/add', private=True, body=body)
        order = p.get('order')
        if not isinstance(order, dict) or 'id' not in order:
            raise NobitexError('order response without id', code='BAD_RESPONSE')
        return order

    def order_status(self, order_id=None, *, client_order_id: str | None = None) -> dict:
        """One order's current record (amount, matchedAmount, unmatchedAmount, status, fee, averagePrice)."""
        if (order_id is None) == (not client_order_id):
            raise ValueError('exactly one of order id or clientOrderId is required')
        body = {'id': int(order_id)} if order_id is not None else {'clientOrderId': client_order_id}
        p = self._call('POST', '/market/orders/status', private=True, body=body)
        order = p.get('order')
        if not isinstance(order, dict):
            raise NobitexError('order-status response without order', code='BAD_RESPONSE')
        return order

    def trades_page(self, src='btc', dst='usdt', page: int = 1) -> dict:
        """One page of the account's own trades (last 3 days, newest first): {'trades', 'hasNext'}."""
        q = {'srcCurrency': src, 'dstCurrency': dst}
        if page > 1:
            q['page'] = int(page)
        p = self._call('GET', '/market/trades/list', private=True, query=q)
        t = p.get('trades')
        if not isinstance(t, list):
            raise NobitexError('unexpected trades response', code='BAD_RESPONSE')
        return {'trades': t, 'hasNext': bool(p.get('hasNext'))}

    def trades(self, src='btc', dst='usdt') -> list:
        return self.trades_page(src, dst)['trades']

    def cancel(self, order_id=None, *, client_order_id: str | None = None) -> None:
        """Cancel by exchange id, or (for an order whose placement reply was lost) by clientOrderId.
        An accepted request is NOT a confirmed cancellation: the engine waits for the final record."""
        if (order_id is None) == (not client_order_id):
            raise ValueError('exactly one of order id or clientOrderId is required')
        body = {'order': int(order_id)} if order_id is not None else {'clientOrderId': client_order_id}
        body['status'] = 'canceled'
        self._call('POST', '/market/orders/update-status', private=True, body=body)
