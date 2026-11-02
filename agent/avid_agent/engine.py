"""AVID Private Agent engine.

Each step:
  1. read the local ceilings (invalid -> fail closed, see below) and queue new engine-log records;
  2. sync with the cloud (protocol v2): send queued reports, delete only what the cloud ACKNOWLEDGED,
     receive and VALIDATE the directive (mode, kill switch, global halt, approved strategy version);
  3. translate the directive into the AVID control file, bounded by the LOCAL ceilings;
  4. run one AVID engine cycle (state machine, caps, stale-order handling, lost-reply reconciliation by
     clientOrderId, final settlement of every order, accounting reconciliation);
  5. queue what happened (snapshots, intents, exchange orders, fills, events) and report it.

Fail-safe rules (all local, they hold even if the cloud is down or compromised - Invariant E):
  - live trading needs BOTH the cloud mode 'live' AND live_allowed=true in ceilings.json;
  - caps are min(cloud policy, local ceilings); max_spread is min(cloud, local); min_cash is max(cloud, local);
  - a directive that fails validation is not applied (sync not fresh -> no new orders);
  - a NEW order is placed only in a step whose cloud sync succeeded with a valid ACK and directive;
    while the link is down, open orders are still managed but nothing new is sent (audit P0-5);
  - no successful contact for longer than the offline grace (monotonic clock, audit P1-18) ->
    simulation, and the bot's open orders are cancelled;
  - the cloud answers 'agent revoked' -> permanent local stop, stored on disk (survives restarts,
    cleared only by pairing again); the bot's open orders are cancelled at once;
  - invalid local ceilings (AVID-02) -> no new orders, live off, the bot's own orders are cancelled; the
    step still runs, so STOP and cancellation keep working (audit 59);
  - outbox under storage pressure or failing (AVID-05) -> no new orders until it recovers;
  - not being allowed to trade never removes the right to cancel the bot's own orders (P0-4);
  - kill switch from the cloud -> data/bot/STOP.cloud (own orders cancelled). The cloud never touches
    data/bot/STOP, which belongs to the customer;
  - global halt -> simulation; an expired policy (expires_at) -> no new orders.

Log -> outbox (AVID-04): the engine log is read with a cursor (device, inode, offset) stored in the
outbox database and advanced in the same transaction as the items of each record. Rotated files are
found by their identity; a missing generation is reported as a log_gap error. Item keys come from the
record's event id, so a re-read never duplicates anything.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import time
import types
import uuid
from pathlib import Path

from avid_core import control as core_control
from avid_core import ed25519, fsio
from avid_core.client import NobitexError
from avid_core.cycle import DEFAULT_FEE_RESERVE, EngineBusy, Store, cycle
from avid_core.rebalance import MARKETS, exchange_min_order
from avid_core.events import level_of
from avid_core.validation import ValidationError, loads_strict

from . import __version__
from .cloud import CloudClient, CloudError, PROTOCOL, parse_sync_response
from .outbox import Outbox, OutboxError, OutboxFull

VERSION = f'avid-agent/{__version__}'
CLOUD_STOP_MARK = 'avid-cloud kill switch\n'
CLOUD_STOP = 'STOP.cloud'
MAX_BATCH = 400
MAX_BATCH_BYTES = 1_500_000
MAX_SYNC_ROUNDS = 50
SYNC_BUDGET_S = 90
SHUTDOWN_BUDGET_S = 150           # controlled stop: cancels + final report; systemd TimeoutStopSec is 300
CYCLE_RESERVE_S = 50              # a further shutdown cycle only if this much budget is left
FINAL_SYNC_S = 20
LOG_TX_LINES = 200
FALLBACK_WEIGHTS = {'btc': 0.34, 'paxg': 0.33, 'usdt': 0.33}
SAFE_CEILINGS = {'live_allowed': False, 'max_order_usdt': 20.0, 'max_orders_per_day': 1, 'max_daily_usdt': 20.0,
                 'max_spread': 0.01, 'min_cash_usdt': 0.0}
QUIET_EVENTS = {'snapshot', 'trade', 'order_progress'}
STATE_TO_STATUS = {'OPEN': 'open', 'PARTIALLY_FILLED': 'partially_filled', 'CANCEL_REQUESTED': 'cancel_requested',
                   'UNKNOWN': 'unknown', 'MANUAL_RECONCILIATION': 'unknown'}


def _stderr(rec: dict):
    import sys
    try:
        print(json.dumps(rec, default=str, ensure_ascii=False), file=sys.stderr, flush=True)
    except Exception:
        pass


def _ms(iso, default=None) -> int | None:
    try:
        return int(dt.datetime.fromisoformat(str(iso)).timestamp() * 1000)
    except (TypeError, ValueError):
        return default


class Agent:
    def __init__(self, cloud: CloudClient, exchange, root: Path, ceilings_fn, *, clock=time.time,
                 offline_grace_s: float = 1800, mono=None, outbox_max_bytes=None, outbox_max_items=None,
                 should_stop=None, sync_budget_s: float = SYNC_BUDGET_S):
        self.cloud, self.exchange = cloud, exchange
        self.root = Path(root)
        fsio.private_dir(self.root)
        self.store = Store(self.root)
        self.ceilings_fn = ceilings_fn
        self.clock = clock
        # monotonic time for "how long since the last successful sync" inside this process; an
        # injected test clock doubles as the monotonic source
        self.mono = mono or (clock if clock is not time.time else time.monotonic)
        self.offline_grace_s = offline_grace_s
        self.should_stop = should_stop or (lambda: False)
        self.sync_budget_s = sync_budget_s
        self.state_path = self.root / 'agent_state.json'
        self.state = self._load_state()
        kw = {}
        if outbox_max_bytes:
            kw['max_bytes'] = outbox_max_bytes
        if outbox_max_items:
            kw['max_items'] = outbox_max_items
        self.outbox = Outbox(self.root / 'outbox.sqlite3', **kw)
        self._migrate_outbox()
        self._migrate_stop()
        self._last_ok_mono = None
        self._backoff_until = None
        self._storage_error = None
        self._policy_error = None
        self._policy_expired = False

    # ------------------------------------------------------------ state
    def _load_state(self) -> dict:
        st = {}
        if self.state_path.exists():
            try:
                st = loads_strict(self.state_path.read_text())
                if not isinstance(st, dict):
                    raise ValidationError('not an object')
            except (OSError, ValueError, UnicodeDecodeError) as e:
                aside = self.state_path.with_name(f'agent_state.corrupt-{int(time.time())}.json')
                try:
                    self.state_path.replace(aside)
                except OSError:
                    pass
                self._log('state_corrupt', level='error', moved_to=aside.name,
                               message=f'agent_state.json unusable ({str(e)[:100]}): no new orders until the cloud '
                                       'answers a sync again')
                st = {}
        st.setdefault('last_sync_ok', None)
        st.setdefault('directive', None)
        if st['directive'] is not None:                 # a stored directive is re-validated (v1.2 kept it raw)
            from .directive import DirectiveError, validate
            try:
                st['directive'] = validate(st['directive'])
            except DirectiveError:
                st['directive'] = None
        return st

    def _migrate_outbox(self):
        old = self.state.pop('outbox', None)            # v1.0 kept the queue inside agent_state.json
        if isinstance(old, dict):
            for kind, items in old.items():
                if kind in ('snapshots', 'intents', 'orders', 'events') and isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict):
                            self.outbox.put(kind, it)
            self._save_state()

    def _migrate_stop(self):
        """v1.2 wrote the cloud kill switch into STOP itself; it now lives in STOP.cloud."""
        stop = self.store.dir / 'STOP'
        try:
            if stop.is_file() and not stop.is_symlink() and stop.read_text() == CLOUD_STOP_MARK:
                os.replace(stop, self.store.dir / CLOUD_STOP)
        except OSError:
            pass

    def _save_state(self):
        """Never lets a storage error abort a step (that would also stop STOP from cancelling)."""
        try:
            fsio.atomic_write(self.state_path, json.dumps(self.state, default=str, allow_nan=False))
        except OSError as e:
            self._storage_error = f'agent_state.json not saved: {e}'[:200]
            _stderr({'event': 'storage_error', 'level': 'error', 'message': self._storage_error})

    def _log(self, event, **kw) -> dict:
        """store.log that degrades to stderr when the disk cannot be written."""
        try:
            return self.store.log(event, **kw)
        except OSError as e:
            rec = {'event': event, 'level': kw.get('level') or level_of(event), 'log_write_failed': str(e)[:120],
                   **{k: v for k, v in kw.items() if k != 'level'}}
            self._storage_error = f'engine log not writable: {e}'[:200]
            _stderr(rec)
            return rec

    @property
    def revoked(self) -> bool:
        return bool(self.state.get('revoked'))

    def _alert(self, event, key, message, level='error'):
        """Log `event` once while the same condition (key) persists."""
        alerts = self.state.setdefault('alerts', {})
        if alerts.get(event) == key:
            return
        alerts[event] = key
        self._log(event, level=level, message=str(message)[:300])

    def _clear_alert(self, event):
        (self.state.get('alerts') or {}).pop(event, None)

    # ------------------------------------------------------------ main step
    def _offline_for(self, now) -> float:
        if self._last_ok_mono is not None:
            return max(0.0, self.mono() - self._last_ok_mono)
        last = self.state.get('last_sync_ok')
        if last is None:
            return float('inf')
        gap = now - last
        return gap if gap >= 0 else float('inf')       # clock moved backwards: do not trust it

    def _ceilings(self):
        from .config import validate_ceilings
        try:
            c = validate_ceilings(self.ceilings_fn())
        except Exception as e:
            msg = (f'local ceilings unusable ({str(e)[:160]}): no new orders, live trading off, the bot\'s own '
                   'orders are cancelled until ceilings.json is valid again')
            self._alert('ceilings_invalid', str(e)[:160], msg)
            return dict(SAFE_CEILINGS), msg
        self._clear_alert('ceilings_invalid')
        return c, None

    def _safe_collect(self):
        try:
            self._collect_log()
            self._storage_error = None
        except OutboxError as e:
            self._storage_error = str(e)[:200]
            self._alert('storage_pressure', 'error', f'outbox storage problem ({self._storage_error}): no new orders; '
                        'the records stay in the engine log and are queued once it recovers')

    def step(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        if self.should_stop():                                # a stop is pending: go straight to it
            return {'record': {'event': 'skipped', 'reason': 'stop requested'}, 'mode_reason': 'stopping',
                    'offline': False, 'fresh': False, 'revoked': self.revoked, 'holds': ['stopping']}
        ceil, ceil_err = self._ceilings()
        self._safe_collect()
        fresh = self._sync(ceil, now)
        offline = self._offline_for(now) > self.offline_grace_s
        revoked = self.revoked
        directive = None if revoked else self.state.get('directive')
        cfg, why = self._apply(directive, ceil, offline, revoked, now=now)
        holds = []
        if ceil_err:
            holds.append(ceil_err)
        if self._storage_error:
            holds.append(f'outbox storage problem: {self._storage_error}')
        else:
            try:
                if self.outbox.pressure:
                    holds.append('outbox under storage pressure (queue full of unacknowledged critical reports)')
                    self._alert('storage_pressure', 'pressure', 'outbox under storage pressure: no new orders until '
                                'the cloud has acknowledged enough reports; nothing financial is dropped')
                else:
                    self._clear_alert('storage_pressure')
            except OutboxError as e:
                holds.append(f'outbox storage problem: {e}')
        if self._policy_error:
            holds.append(f'policy rejected locally: {self._policy_error}')
        if self._policy_expired:
            holds.append('the approved policy has expired; waiting for a current one')
        allow_new = fresh and not revoked and not offline and not holds
        hold = ('agent revoked in the cloud' if revoked else
                'cloud link not fresh: no new orders until a sync succeeds' if not fresh else
                holds[0] if holds else '')
        st = self.store.peek()
        busy = st.get('bot_orders') or st.get('pending') or st.get('closing') or st.get('fills_pending')
        try:
            if not self.state.get('has_policy') and not busy:
                rec = self._snapshot_only(why)                # nothing approved yet: observe, never plan
            else:
                rec = cycle(self.exchange, self.store, cfg, live=ceil['live_allowed'] and not revoked, now=now,
                            allow_new=allow_new, hold_reason=hold)
        except NobitexError as e:
            # an exchange problem (IP not whitelisted, key revoked, Nobitex down...) must reach the
            # customer's dashboard, not only this terminal: error once, then a warning while it repeats
            rec = self._exchange_error(e, now)
        except EngineBusy as e:
            rec = self._log('engine_busy', level='warning', message=str(e))
        except OSError as e:                                  # the engine could not write its files
            em = getattr(e, 'emergency', None)
            self._storage_error = f'engine storage: {e}'[:200]
            rec = self._log('emergency_cancel' if em else 'error', level='error',
                            message=f'storage error ({str(e)[:120]}): no new orders'
                                    + (f'; cancel requests sent without writing: {em["cancel_requests_sent"]}'
                                       if em else ''), emergency=em)
        except Exception as e:                                # one bad cycle never stops reporting
            rec = self._log('error', level='error', message=f'UNEXPECTED {type(e).__name__}: {str(e)[:300]}')
        if rec.get('event') != 'error':
            self.state.pop('last_exchange_error', None)
        self._safe_collect()
        if fresh and not self.should_stop():
            self._sync(ceil, now)
        self._save_state()
        return {'record': rec, 'mode_reason': why, 'offline': offline, 'fresh': fresh, 'revoked': revoked,
                'holds': holds}

    def safe_shutdown(self, now: float | None = None, attempts: int = 3, budget_s: float = SHUTDOWN_BUDGET_S) -> dict:
        """Controlled stop: no new orders, cancel the bot's own open orders, report, say what is left.

        Bounded in time (systemd kills the service TimeoutStopSec after SIGTERM): the exchange client gets
        a deadline, after which it sends nothing more, and the final report sync gets what is left of the
        budget (at most FINAL_SYNC_S)."""
        now = self.clock() if now is None else now
        start = time.monotonic()
        if hasattr(self.exchange, 'deadline'):
            self.exchange.deadline = start + budget_s
        ceil, _ = self._ceilings()
        cfg, _ = self._apply(None, ceil, True, self.revoked, reason='shutdown', now=now)
        rec = None
        try:
            for i in range(attempts):
                if i and time.monotonic() - start > budget_s - CYCLE_RESERVE_S:
                    break
                try:
                    rec = cycle(self.exchange, self.store, cfg, live=ceil['live_allowed'], now=now + i,
                                allow_new=False, hold_reason='shutting down')
                except (NobitexError, EngineBusy) as e:
                    rec = {'event': 'error', 'message': str(e)[:200]}
                except OSError as e:
                    rec = {'event': 'emergency_cancel', 'message': str(e)[:200],
                           'emergency': getattr(e, 'emergency', None)}
                st = self.store.peek()
                if not st.get('bot_orders') and not st.get('closing') and not st.get('pending'):
                    break
        finally:
            if hasattr(self.exchange, 'deadline'):
                self.exchange.deadline = None
        st = self.store.peek()
        self._safe_collect()
        left_s = budget_s - (time.monotonic() - start)
        if left_s > 2:
            self._sync(ceil, now, budget_s=min(FINAL_SYNC_S, left_s), ignore_stop=True)
        self._save_state()
        left = sorted(set(st.get('bot_orders', {})) | set(st.get('closing', {}))
                      | ({f'lost-reply:{st["pending"].get("cid")}'} if st.get('pending') else set()))
        return {'open_bot_orders': left, 'last_event': (rec or {}).get('event'),
                'ok': not left, 'message': 'no bot order left open' if not left else
                'these bot orders could not be confirmed cancelled - check them in the Nobitex panel: '
                + ', '.join(left)}

    def _exchange_error(self, e, now):
        msg = str(e)[:300]
        hint = ''
        if 'not whitelisted' in msg or 'IP' in msg and e.http_status in (401, 403):
            hint = (' - the public IP of this machine is not in the API key\'s IP whitelist: add it in the '
                    'Nobitex panel (API keys) or use a key that allows this IP')
        elif e.http_status in (401, 403):
            hint = ' - the Nobitex API key was refused: check it in the Nobitex panel, then run  avid keys'
        same = self.state.get('last_exchange_error') == msg
        self.state['last_exchange_error'] = msg
        return self._log('error', level='warning' if same else 'error', message=msg + hint,
                              http_status=e.http_status, code=e.code)

    def _snapshot_only(self, why):
        cur = tuple(MARKETS) + ('usdt',)
        fn = getattr(self.exchange, 'wallets', None)
        if fn is not None:
            w = fn(currencies=cur)
            bal = {c: float(w[c]['total']) for c in cur}
        else:
            bal = self.exchange.balances(currencies=cur)
        books = {a: self.exchange.best_bid_ask(MARKETS[a]['symbol']) for a in MARKETS if bal.get(a, 0.0) > 0}
        value = bal.get('usdt', 0.0) + sum(bal.get(a, 0.0) * (b + k) / 2 for a, (b, k) in books.items())
        return self._log('snapshot', balances=bal, books=books, value_usdt=round(value, 6),
                              reason=f'observing only ({why}): no approved strategy yet')

    # ------------------------------------------------------------ cloud sync (protocol v2)
    def _sync(self, ceil: dict, now: float, *, budget_s: float | None = None, ignore_stop: bool = False) -> bool:
        if self.revoked:
            return False
        budget = self.sync_budget_s if budget_s is None else budget_s
        if self._backoff_until is not None and self.mono() < self._backoff_until:
            return False
        try:
            dropped = self.outbox.take_dropped()
        except OutboxError:
            dropped = {}
        if dropped:
            self._local_event('outbox_dropped', 'warning' if set(dropped) <= {'droppable'} else 'error',
                              f'queue limit: {dropped} reports were not kept (snapshots and info first; orders, fills '
                              'and errors are never dropped)')
            self._safe_collect()
        max_items, max_bytes = MAX_BATCH, MAX_BATCH_BYTES
        start = self.mono()
        ok = False
        for _ in range(MAX_SYNC_ROUNDS):
            if self.should_stop() and not ignore_stop:
                break                                         # a stop is pending: one request at most
            if self.mono() - start > budget:
                break
            try:
                batch, keys = self.outbox.batch(max_items, max_bytes)
                queued = self.outbox.stats()
            except OutboxError as e:
                self._storage_error = str(e)[:200]
                break
            batch_id = uuid.uuid4().hex
            report = {'protocol': PROTOCOL, 'agent_id': self.cloud.agent_id, 'batch_id': batch_id, 'version': VERSION,
                      'network': getattr(self.exchange, 'network', None), 'signer': ed25519.backend(),
                      'local_ceilings': ceil, 'outbox': queued, **batch}
            try:
                resp = self.cloud.sync(report)
            except CloudError as e:
                if e.status == 413 and max_items > 1:          # too big: smaller batches
                    max_items, max_bytes = max(1, max_items // 2), max(16_000, max_bytes // 2)
                    continue
                if e.revoked:
                    self._on_revoked(e, now)
                else:
                    if e.status in (429, 503) and e.retry_after:
                        self._backoff_until = self.mono() + e.retry_after
                    self._local_event('cloud_unreachable', 'warning', str(e))
                break
            try:
                res = parse_sync_response(resp, agent_id=self.cloud.agent_id, batch_id=batch_id, sent_keys=keys)
            except CloudError as e:
                self._alert('cloud_bad_ack', str(e)[:120], f'{e}; nothing was deleted from the queue and no new '
                            'orders are placed until the cloud answers correctly')
                break
            self._clear_alert('cloud_bad_ack')
            try:
                self.outbox.ack(res.accepted)
                if res.rejected:
                    self.outbox.reject(res.rejected)
                    self._local_event('cloud_items_rejected', 'warning',
                                      f'the cloud refused {len(res.rejected)} report(s) for good; they are kept '
                                      f'locally in outbox.sqlite3 (table rejected): {list(res.rejected.values())[:3]}')
            except OutboxError as e:
                self._storage_error = str(e)[:200]
                break
            self._backoff_until = None
            if res.directive_error:
                self._alert('directive_rejected', res.directive_error[:120],
                            f'the cloud directive was rejected locally ({res.directive_error}); it is not applied and '
                            'no new orders are placed until a valid one arrives')
            else:
                self._clear_alert('directive_rejected')
                self.state['directive'] = res.directive
                self.state['last_sync_ok'] = now
                self._last_ok_mono = self.mono()
                ok = True
            if not keys or not res.accepted:
                break
            try:
                if not len(self.outbox):
                    break
            except OutboxError:
                break
        self._save_state()
        return ok

    def _on_revoked(self, e: CloudError, now):
        if not self.revoked:
            self.state['revoked'] = {'at': now, 'status': e.status, 'message': str(e)[:200]}
            self.state['directive'] = None
            self._local_event('agent_revoked', 'error',
                              'the cloud revoked this agent: no new orders; the bot\'s open orders are being '
                              'cancelled. Pair again to reconnect (avid-agent pair).')

    def _local_event(self, kind, level, message):
        self._log(kind, message=message[:300], level=level)

    # ------------------------------------------------------------ directives -> AVID control
    def _sync_stop_file(self, port, d):
        cloud_stop = self.store.dir / CLOUD_STOP
        try:
            if port and port.get('kill_switch'):
                if not cloud_stop.exists():
                    fsio.atomic_write(cloud_stop, CLOUD_STOP_MARK)
            elif d is not None and cloud_stop.exists():
                cloud_stop.unlink()                           # only ever the cloud's own file; STOP is the customer's
        except OSError as e:
            self._alert('cloud_stop_file', str(e)[:80], f'could not update STOP.cloud: {e}')

    def _apply(self, d: dict | None, ceil: dict, offline: bool, revoked: bool = False, reason: str | None = None,
               now: float | None = None):
        port = (d or {}).get('portfolio')
        pol = (port or {}).get('policy')
        self._sync_stop_file(port, d)
        live = bool(port and pol and port.get('mode') == 'live' and not (d or {}).get('global_halt')
                    and not offline and not revoked and ceil['live_allowed'] and reason is None)
        why = (reason if reason else 'revoked' if revoked else 'live' if live else 'offline' if offline
               else 'global_halt' if (d or {}).get('global_halt')
               else 'no_portfolio' if not port else 'no_policy' if not pol
               else 'local_live_not_allowed' if port.get('mode') == 'live' and not ceil['live_allowed']
               else 'simulation')
        if pol is None and d is None:                         # no fresh directive: keep the last policy's shape
            pol_shape = (((self.state.get('directive') or {}).get('portfolio') or {}).get('policy'))
        else:
            pol_shape = pol
        if not isinstance(pol_shape, dict):
            pol_shape = None
        self._policy_error = None
        exp = (pol_shape or {}).get('expires_at')
        now_ms = int((self.clock() if now is None else now) * 1000)
        self._policy_expired = bool(exp and now_ms > exp)
        weights = dict(pol_shape['weights']) if pol_shape else dict(FALLBACK_WEIGHTS)
        for a in list(MARKETS) + ['usdt']:
            weights.setdefault(a, 0.0)
        ctl = {'mode': 'live' if live else 'dry', 'weights': weights,
               'band_rel': pol_shape['band_rel'] if pol_shape else 0.25}
        local_spread = float(ceil.get('max_spread', 0.01))
        local_cash = float(ceil.get('min_cash_usdt', 0.0))
        if pol_shape:
            ctl.update({'max_order_usdt': pol_shape['max_order_usdt'],
                        'max_orders_per_day': pol_shape['max_orders_per_day'],
                        'max_daily_usdt': pol_shape['max_daily_usdt'],
                        'max_spread': min(float(pol_shape.get('max_spread', 0.01)), local_spread),
                        'min_cash_usdt': max(float(pol_shape.get('min_cash_usdt', 0.0)), local_cash)})
        try:
            ctl = core_control.validate(ctl)                  # never write something the engine would reject
        except (core_control.ControlError, TypeError, KeyError) as e:
            self._policy_error = str(e)[:200]
            self._alert('policy_rejected_locally', self._policy_error, f'policy rejected locally: {e}')
            ctl = {'mode': 'dry', 'weights': {**{a: 0.0 for a in MARKETS}, **FALLBACK_WEIGHTS}, 'band_rel': 0.25}
        current, err = core_control.load(self.root)
        if err or {k: v for k, v in current.items() if k != 'updated_at'} != ctl:
            try:
                core_control.save(self.root, ctl, audit={'source': 'avid-cloud', 'mode': ctl['mode'], 'why': why,
                                                         'policy': (pol_shape or {}).get('version_id')})
            except (OSError, ValueError) as e:                # the cycle then fails closed on the stale file
                self._alert('control_write_failed', str(e)[:80], f'could not write control.json: {e}')
        cfg = types.SimpleNamespace(weights=ctl['weights'], band_rel=ctl['band_rel'], min_order_usdt=exchange_min_order(),
                                    max_order_usdt=ceil['max_order_usdt'],
                                    max_orders_per_day=ceil['max_orders_per_day'],
                                    max_daily_usdt=ceil['max_daily_usdt'], stale_minutes=30,
                                    min_cash_usdt=local_cash, max_spread=local_spread, fee_reserve=DEFAULT_FEE_RESERVE)
        if d is not None:
            self.state['has_policy'] = bool(pol)
        self.state['last_mode'] = ctl['mode']
        return cfg, why

    # ------------------------------------------------------------ AVID log -> report items (AVID-04)
    def _log_files(self):
        base = self.store.log_path
        paths = [base.with_suffix(f'.jsonl.{i}') for i in range(Store.LOG_GENERATIONS, 0, -1)] + [base]
        out = []
        for p in paths:
            try:
                out.append((p, os.stat(p)))
            except FileNotFoundError:
                continue
        return out

    def _collect_log(self):
        files = self._log_files()
        if not files:
            return
        cur = self.outbox.get_cursor()
        start_i, start_off = 0, 0
        if cur is None:
            legacy = self.state.pop('log_offset', None)       # v1.2: an offset of the current file only
            if isinstance(legacy, int) and legacy > 0 and files[-1][0] == self.store.log_path:
                if legacy <= files[-1][1].st_size:
                    start_i, start_off = len(files) - 1, legacy
                elif len(files) > 1 and files[-2][1].st_size >= legacy:
                    start_i, start_off = len(files) - 2, legacy
        else:
            idx = next((i for i, (_, s) in enumerate(files) if s.st_ino == cur.get('ino') and s.st_dev == cur.get('dev')),
                       None)
            if idx is None:
                self._gap(f'the log file the reader was in (inode {cur.get("ino")}) is gone - more rotations than '
                          'kept generations while the agent was not reading; earlier records may be missing')
            elif files[idx][1].st_size < int(cur.get('off', 0)):
                self._gap(f'{files[idx][0].name} is shorter than the read position (truncated?)')
                start_i = idx
            else:
                start_i, start_off = idx, int(cur.get('off', 0))
        for i in range(start_i, len(files)):
            path, stat = files[i]
            if not self._read_file(path, stat, start_off if i == start_i else 0):
                break

    def _gap(self, message):
        self._log('log_gap', level='error', message=message)

    def _read_file(self, path, stat, off) -> bool:
        """Queue every complete record after `off`; the cursor moves in the same transaction. False when the
        outbox is full (the rest stays in the log and is read next time)."""
        ident = {'dev': stat.st_dev, 'ino': stat.st_ino}
        with open(path, 'rb') as f:
            f.seek(off)
            done = False
            while not done:
                with self.outbox.tx():
                    for _ in range(LOG_TX_LINES):
                        line_start = f.tell()
                        line = f.readline()
                        if not line or not line.endswith(b'\n'):
                            done = True
                            break
                        end = f.tell()
                        try:
                            with self.outbox.tx():
                                for kind, item, key in self._map_line(line, ident, line_start):
                                    self.outbox.put(kind, item, item_key=key)
                                self.outbox.set_cursor({**ident, 'off': end})
                        except OutboxFull:
                            return False
        return True

    def _map_line(self, line: bytes, ident: dict, offset: int):
        try:
            rec = json.loads(line)
        except ValueError:
            return []
        if not isinstance(rec, dict):
            return []
        eid = rec.get('eid')
        base = str(eid)[:120] if eid else 'L' + hashlib.sha256(f'{ident["dev"]}:{ident["ino"]}:{offset}:'.encode()
                                                               + line).hexdigest()[:32]
        try:
            return self._map(rec, base)
        except Exception as e:                                # a malformed record never stalls the cursor
            return [('events', {'key': self._key(base), 'at': int(time.time() * 1000), 'kind': 'record_unmapped',
                                'level': 'warning', 'message': f'log record could not be mapped: {type(e).__name__}',
                                'data': {}}, f'{base}:event')]

    def _key(self, base: str) -> str:
        return hashlib.sha256((self.cloud.agent_id or '').encode() + b'|' + base.encode()).hexdigest()[:32]

    def _order_item(self, rec, status, **extra):
        oid = str(rec.get('id'))
        return {'exchange_order_id': oid, 'intent_key': f'oid:{oid}', 'client_order_id': rec.get('cid'),
                'asset': rec.get('asset') or '', 'side': rec.get('side') or rec.get('action') or '',
                'amount': rec.get('amount') or 0, 'price': rec.get('price') or 0, 'status': status, **extra}

    def _map(self, rec: dict, base: str) -> list:
        items = []
        ev = str(rec.get('event', ''))
        at = _ms(rec.get('at'), 0)
        key = self._key(base)
        net = getattr(self.exchange, 'network', None)
        has_id = rec.get('id') not in (None, '', 'None')
        if isinstance(rec.get('balances'), dict) and isinstance(rec.get('books'), dict):
            books = {a: list(v) for a, v in rec['books'].items() if isinstance(v, (list, tuple)) and len(v) == 2}
            items.append(('snapshots', {'key': key, 'at': at, 'balances': rec['balances'], 'books': books,
                                        'available': rec.get('available') if isinstance(rec.get('available'), dict)
                                        else None, 'value_usdt': rec.get('value_usdt') or 0, 'network': net,
                                        'mode': rec.get('mode')}, f'{base}:snap'))
        if ev in ('order_placed', 'dry_run_would_place') and self.state.get('has_policy', True):
            live = ev == 'order_placed' and has_id
            ikey = f"oid:{rec['id']}" if live else key
            items.append(('intents', {'key': ikey, 'at': at, 'mode': 'live' if live else 'simulation',
                                      'asset': rec.get('asset'), 'side': rec.get('action'), 'amount': rec.get('amount'),
                                      'price': rec.get('price'), 'notional_usdt': rec.get('notional_usdt'),
                                      'reason': rec.get('reason', ''), 'status': 'open' if live else 'simulated',
                                      'network': net}, f'{base}:intent'))
            if live:
                items.append(('orders', self._order_item({**rec, 'side': rec.get('action')}, 'open', at=at, state='OPEN'),
                              f'{base}:order'))
        elif ev == 'adopted_after_error' and has_id:
            ikey = f"oid:{rec['id']}"
            items.append(('intents', {'key': ikey, 'at': at, 'mode': 'live', 'asset': rec.get('asset'),
                                      'side': rec.get('side'), 'amount': rec.get('amount'), 'price': rec.get('price'),
                                      'notional_usdt': (rec.get('amount') or 0) * (rec.get('price') or 0),
                                      'reason': 'adopted after an unanswered placement', 'status': 'open',
                                      'network': net}, f'{base}:intent'))
            items.append(('orders', self._order_item(rec, 'open', at=at), f'{base}:order'))
        elif ev == 'order_progress' and has_id:
            items.append(('orders', self._order_item(rec, 'partially_filled', filled_amount=rec.get('filled_amount'),
                                                     at=at, state='PARTIALLY_FILLED'), f'{base}:order'))
        elif ev == 'order_state' and has_id and rec.get('to_state') in STATE_TO_STATUS:
            items.append(('orders', self._order_item(rec, STATE_TO_STATUS[rec['to_state']], at=at,
                                                     state=rec.get('to_state')), f'{base}:order'))
        elif ev == 'order_final' and has_id:
            items.append(('orders', self._order_item(rec, rec.get('status') or 'unknown', at=at, state=rec.get('state'),
                                                     filled_amount=rec.get('filled_amount'),
                                                     unfilled_amount=rec.get('unfilled_amount'),
                                                     avg_price=rec.get('avg_price'), fee=rec.get('fee'),
                                                     fee_asset=rec.get('fee_asset'),
                                                     accounting=rec.get('accounting') or 'none'), f'{base}:order'))
        elif ev in ('fills_reconciled', 'fill_aggregate', 'reconciliation', 'reconciliation_incomplete') and has_id:
            recon = {k: rec.get(k) for k in ('reconciliation_status', 'expected_filled_amount', 'observed_filled_amount',
                                             'missing_filled_amount', 'executions_count', 'pagination_complete',
                                             'last_reconciliation_at') if k in rec}
            items.append(('orders', {'exchange_order_id': str(rec['id']), 'intent_key': f"oid:{rec['id']}",
                                     'accounting': rec.get('accounting'), 'reconciliation': recon, 'at': at},
                          f'{base}:order'))
            if ev == 'fill_aggregate':                    # order totals stand in for missing trade records
                items.append(('fills', {'trade_id': f"agg-{rec['id']}", 'exchange_order_id': str(rec['id']),
                                        'asset': rec.get('asset'), 'side': rec.get('side'),
                                        'amount': rec.get('filled_amount'), 'price': rec.get('avg_price'),
                                        'fee': rec.get('fee') or 0, 'at': at, 'aggregate': True,
                                        'fee_asset': rec.get('asset') if rec.get('side') == 'buy' else 'usdt'},
                              f'{base}:aggfill'))
        elif ev in ('order_status_unknown', 'order_resolved_manually') and has_id:
            items.append(('orders', {'exchange_order_id': str(rec['id']), 'intent_key': f"oid:{rec['id']}",
                                     'status': 'unknown', 'state': 'UNKNOWN' if ev == 'order_status_unknown'
                                     else 'MANUAL_RECONCILIATION', 'at': at}, f'{base}:order'))
        elif ev == 'trade' and rec.get('trade_id') is not None:
            items.append(('fills', {'trade_id': str(rec.get('trade_id')), 'exchange_order_id': str(rec.get('id')),
                                    'asset': rec.get('asset'), 'side': rec.get('side'), 'amount': rec.get('amount'),
                                    'price': rec.get('price'), 'fee': rec.get('fee') or 0, 'fee_asset': rec.get('fee_asset'),
                                    'at': _ms(rec.get('exchange_at'), at) if rec.get('exchange_at') else at,
                                    'network': net}, f'fill:{rec.get("trade_id")}'))
        level = level_of(ev, rec.get('level'))
        if ev == 'waiting' and 'real bot order' in str(rec.get('reason', '')):
            level = 'warning'
        if level not in ('info', 'warning', 'error'):
            level = 'info'
        msg = rec.get('reason') or rec.get('cap') or rec.get('message') or ''
        data = {k: rec[k] for k in ('action', 'asset', 'amount', 'price', 'notional_usdt', 'id', 'cid', 'weights', 'mode',
                                    'cap_refund_usdt', 'status', 'state', 'from_state', 'to_state', 'filled_amount',
                                    'final_status', 'reconciliation_status', 'missing_filled_amount', 'assets')
                if k in rec}
        if ev not in QUIET_EVENTS:
            items.append(('events', {'key': key, 'at': at, 'kind': ev or 'event', 'level': level,
                                     'message': str(msg)[:500], 'data': data, 'network': net}, f'{base}:event'))
        return items
