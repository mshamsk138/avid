"""avid-agent: the AVID Private Agent command line.

  avid-agent install                                  background service (systemd --user), no root
  avid-agent uninstall [--keep-orders] [--force]      controlled stop + remove the service; keeps your data
  avid-agent pair --cloud https://avid.example.ir --code K7QX-M2PR-9WDA
  avid-agent ceilings                                 show local hard limits
  avid-agent ceilings --live yes --max-order 20 --max-orders-day 6 --max-daily 100 [--max-spread 0.01]
                      [--min-cash 0]
  avid-agent start                                    start the service (and at login / boot)
  avid-agent restart                                  restart it; keeps the bot's open orders
  avid-agent stop [--keep-orders]                     controlled stop: no new orders, cancel the bot's own
                                                      open orders, report; exit 3/4 when not confirmed
  avid-agent once                                     one step in the foreground, print the result
  avid-agent run [--interval 600]                     the loop in the foreground (what the service runs)
  avid-agent safe-stop                                no new orders, cancel the bot's open orders, report
  avid-agent resolve --not-placed | --closed ID       settle after checking the Nobitex panel yourself
  avid-agent reset-cid                                try clientOrderId again (after Nobitex enabled it)
  avid-agent status [--json]                          service, cloud link, ceilings, engine state
  avid-agent logs [-f] [-n 50]                        the service output (journald)
  avid-agent help

Stopping the service (avid-agent stop, systemctl stop, docker stop, SIGTERM/SIGINT) is a CONTROLLED stop:
the current step finishes (its cloud sync is cut short and its exchange calls get a deadline), no new
order is placed, the bot's open orders are cancelled and the result is written to
~/.avid-agent/last_stop.json, which `avid-agent stop` reports. To stop without cancelling (restart,
--keep-orders) a one-shot flag ~/.avid-agent/keep-orders-on-next-stop is written just before the stop; it
is honoured only within 10 minutes of being written, so an old flag never keeps orders open (audit 44).

`resolve --not-placed` first looks the order up on Nobitex by its clientOrderId (open list, status and
full list); if it exists the resolution is refused - nothing a message on a dashboard says can talk the
customer into a duplicate order (audit 45).
"""
from __future__ import annotations
import argparse
import json
import os
import signal
import sys
import time

from avid_core import ed25519
from avid_core.client import NobitexClient, NobitexError, read_credentials
from avid_core.rebalance import exchange_min_order
from avid_core.validation import loads_strict

from . import config, service
from .cloud import CloudClient, CloudError
from .engine import VERSION, Agent

NOBITEX_DIR = os.environ.get('NOBITEX_SECRETS_DIR', os.path.expanduser('~/.nobitex'))
KEEP_FLAG = config.KEEP_FLAG


def make_exchange():
    cred = read_credentials(NOBITEX_DIR)
    return (NobitexClient(api_key=cred['api_key'], seed=cred['seed']) if cred['mode'] == 'api_key'
            else NobitexClient(cred['token']))


def make_agent(offline_grace_s=1800, should_stop=None) -> Agent:
    a = config.load_agent()
    cloud = CloudClient(a['cloud_url'], a['agent_id'], a['seed'])
    return Agent(cloud, make_exchange(), config.home(), config.load_ceilings, offline_grace_s=offline_grace_s,
                 should_stop=should_stop)


def on_stop(agent: Agent, home, now: float | None = None) -> dict:
    """What a stop signal does: cancel the bot's open orders, unless a FRESH one-shot keep flag exists."""
    flag = home / KEEP_FLAG
    if flag.exists():
        fresh = config.keep_flag_fresh(flag, now)
        try:
            flag.unlink()
        except OSError:
            pass
        if fresh:
            return {'cancelled': False, 'message': 'stopped; open bot orders kept (restart / --keep-orders)'}
    res = agent.safe_shutdown()
    return {'cancelled': True, **res}


def run_loop(agent: Agent, interval: float, home, *, stopping: list, sleep=time.sleep, out=print) -> dict:
    """The service loop. `stopping` becomes non-empty on SIGTERM/SIGINT; the step in progress finishes,
    then on_stop() runs (controlled shutdown)."""
    while not stopping:
        try:
            res = agent.step()
            out(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                            'event': res['record'].get('event'), 'mode': res['mode_reason'],
                            'offline': res['offline'], 'fresh': res.get('fresh'), 'holds': res.get('holds')},
                           ensure_ascii=False))
        except (NobitexError, CloudError) as e:
            out(json.dumps({'error': str(e)}))
        except Exception as e:                       # one bad step never kills the service
            out(json.dumps({'error': f'UNEXPECTED {type(e).__name__}: {str(e)[:200]}'}))
        for _ in range(int(interval)):
            if stopping:
                break
            sleep(1)
    try:
        res = on_stop(agent, home)
    except Exception as e:                           # e.g. no network: say so, the orders may be open
        res = {'cancelled': False, 'ok': False,
               'message': f'controlled stop failed ({type(e).__name__}: {str(e)[:150]}); check open orders in the '
                          'Nobitex panel'}
    out(json.dumps({'stopped': True, **res}, ensure_ascii=False))
    return res


def serve(agent: Agent, interval: float, home, *, out=print, sleep=time.sleep) -> int:
    """`avid-agent run`: the loop with signal handling. SIGTERM/SIGINT -> the current step finishes
    (its exchange calls get a STEP_DRAIN_S deadline, its cloud sync is cut short) -> controlled stop ->
    the verdict goes to last_stop.json for `avid-agent stop`. Exit 0 when nothing is left open (or the
    orders were kept on purpose), 3 when a cancellation was not confirmed."""
    stopping: list = []

    def on_signal(*_):
        stopping.append(1)
        ex = agent.exchange
        if hasattr(ex, 'deadline') and getattr(ex, 'deadline', None) is None:
            ex.deadline = time.monotonic() + service.STEP_DRAIN_S

    agent.should_stop = lambda: bool(stopping)
    old = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        res = run_loop(agent, interval, home, stopping=stopping, sleep=sleep, out=out)
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
    try:
        service.write_last_stop(res)
    except OSError as e:                             # the verdict is still printed (journal)
        out(json.dumps({'warning': f'could not write {service.LAST_STOP}: {e}'}))
    return 0 if res.get('ok', True) else 3


def _human_status(d: dict) -> str:
    sv, eng, c = d.get('service') or {}, d.get('engine') or {}, d.get('ceilings') or {}
    ls = sv.get('last_stop') or {}
    lines = ['AVID agent status',
             f'  Service          {"active" if sv.get("active") else "not running"}'
             f'{", enabled" if sv.get("enabled") else ""}{"" if sv.get("installed") else " (not installed)"}',
             f'  Cloud            {d.get("cloud") or "not paired"}  agent {d.get("agent_id") or "-"}',
             f'  Network          {d.get("network")}',
             f'  Last sync ok     {d.get("last_sync_ok")}   mode {d.get("last_mode")}   portfolio {d.get("portfolio")}',
             f'  Ceilings         live {"YES" if c.get("live_allowed") else "no"}, order <= {c.get("max_order_usdt")} '
             f'USDT, {c.get("max_orders_per_day")}/day, <= {c.get("max_daily_usdt")} USDT/day',
             f'  Open bot orders  {", ".join(eng.get("open_bot_orders") or []) or "none"}',
             f'  clientOrderId    {eng.get("client_order_id")}   signer {eng.get("signer")}']
    if eng.get('lost_reply_unresolved'):
        lines.append(f'  ATTENTION        lost placement reply not settled (clientOrderId {eng.get("lost_reply_client_order_id")})')
    if eng.get('final_state_unknown'):
        lines.append('  ATTENTION        final state unknown: ' + ', '.join(eng['final_state_unknown']))
    if d.get('global_halt') or d.get('revoked'):
        lines.append(f'  ATTENTION        global halt {d.get("global_halt")}, revoked {d.get("revoked")}')
    if ls:
        lines.append(f'  Last stop        {ls.get("at")}: {"ok" if ls.get("ok", True) else "NOT CONFIRMED"} - '
                     f'{ls.get("message")}')
    return '\n'.join(lines)


def _read_json(p):
    try:
        return loads_strict(p.read_text())
    except (OSError, ValueError):
        return {}


def _engine_summary():
    st = _read_json(config.home() / 'data/bot/state.json')
    if not isinstance(st, dict):
        return {}
    pend = st.get('pending') or {}
    return {'open_bot_orders': sorted(st.get('bot_orders') or {}), 'lost_reply_unresolved': bool(pend),
            'lost_reply_client_order_id': pend.get('cid'),
            'final_state_unknown': sorted(st.get('closing') or {}),
            'accounting_pending': sorted(st.get('fills_pending') or {}),
            'client_order_id': {None: 'not verified yet', True: 'verified', False: 'NOT AVAILABLE - live off'}.get(
                st.get('cid_supported'), 'unknown'),
            'signer': ed25519.backend()}


def _queued():
    p = config.home() / 'outbox.sqlite3'
    if not p.exists():
        return {}
    from .outbox import Outbox
    ob = Outbox(p)
    try:
        return {**ob.counts(), **ob.stats()}
    finally:
        ob.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog='avid-agent', description='AVID Private Agent (Nobitex). See `avid-agent help`.')
    ap.add_argument('--version', action='version', version=VERSION)
    sub = ap.add_subparsers(dest='cmd', metavar='command')
    i = sub.add_parser('install', help='install the background service (systemd --user)')
    i.add_argument('--no-linger', action='store_true', help='do not enable lingering (runs only while logged in)')
    i.add_argument('--unit-only', action='store_true', help='write the unit even if no systemd user manager answers')
    i.add_argument('--allow-root', action='store_true', help=argparse.SUPPRESS)
    u = sub.add_parser('uninstall', help='controlled stop and remove the service (your data is kept)')
    u.add_argument('--keep-orders', action='store_true', help='do not cancel the bot\'s open orders')
    u.add_argument('--force', action='store_true', help='remove the service even if the stop was not confirmed')
    sub.add_parser('start', help='start the service (and at login/boot)')
    s_ = sub.add_parser('stop', help='controlled stop: cancel the bot\'s open orders, report')
    s_.add_argument('--keep-orders', action='store_true', help='stop without cancelling the bot\'s open orders')
    sub.add_parser('restart', help='restart the service, keeping the bot\'s open orders')
    lg = sub.add_parser('logs', help='service output (journald)')
    lg.add_argument('-f', '--follow', action='store_true')
    lg.add_argument('-n', '--lines', type=int, default=50)
    sub.add_parser('help', help='this help')
    p = sub.add_parser('pair')
    p.add_argument('--cloud', required=True)
    p.add_argument('--code', required=True)
    c = sub.add_parser('ceilings')
    c.add_argument('--live', choices=('yes', 'no'))
    c.add_argument('--max-order', type=float)
    c.add_argument('--max-orders-day', type=int)
    c.add_argument('--max-daily', type=float)
    c.add_argument('--max-spread', type=float)
    c.add_argument('--min-cash', type=float)
    sub.add_parser('once')
    r = sub.add_parser('run')
    r.add_argument('--interval', type=float, default=600)
    r.add_argument('--offline-grace', type=float, default=1800)
    st_ = sub.add_parser('status')
    st_.add_argument('--json', action='store_true')
    sub.add_parser('safe-stop')
    rv = sub.add_parser('resolve')
    rv.add_argument('--not-placed', action='store_true', help='the order with the lost reply does not exist')
    rv.add_argument('--closed', nargs='*', default=[], metavar='ORDER_ID',
                    help='these bot orders are no longer open on Nobitex')
    sub.add_parser('reset-cid')
    o = ap.parse_args(argv)
    if o.cmd in (None, 'help'):
        print(__doc__.strip())
        return 0
    try:
        if o.cmd == 'install':
            return service.install(linger=not o.no_linger, unit_only=o.unit_only, allow_root=o.allow_root,
                                   version=VERSION)
        if o.cmd == 'uninstall':
            return service.uninstall(keep_orders=o.keep_orders, force=o.force, safe_stop=_safe_stop_if_paired)
        if o.cmd == 'start':
            return service.start()
        if o.cmd == 'stop':
            return service.stop(keep_orders=o.keep_orders, safe_stop=_safe_stop_if_paired)
        if o.cmd == 'restart':
            return service.restart()
        if o.cmd == 'logs':
            return service.logs(follow=o.follow, lines=o.lines)
        if o.cmd == 'pair':
            url = config.check_cloud_url(o.cloud)
            seed = os.urandom(32)
            res = CloudClient(url).pair(o.code.strip().upper(), ed25519.public_key(seed), VERSION,
                                        config.load_ceilings())
            config.save_agent(url, res['agent_id'], seed)
            print(f'paired: agent {res["agent_id"]} -> {url}\nnext: avid-agent run')
        elif o.cmd == 'ceilings':
            cur = config.load_ceilings()
            if o.live:
                if o.live == 'yes':
                    ans = input('Allow this agent to trade REAL money (within these ceilings)? type LIVE: ')
                    if ans.strip() != 'LIVE':
                        print('not changed')
                        return 1
                    if ed25519.backend() == 'pure' and not ed25519.pure_allowed():
                        print('note: live orders need python3-cryptography (sudo apt install -y python3-cryptography)',
                              file=sys.stderr)
                cur['live_allowed'] = o.live == 'yes'
            for k, v in (('max_order_usdt', o.max_order), ('max_orders_per_day', o.max_orders_day),
                         ('max_daily_usdt', o.max_daily), ('max_spread', o.max_spread), ('min_cash_usdt', o.min_cash)):
                if v is not None:
                    if k == 'max_order_usdt' and v < exchange_min_order():
                        raise config.ConfigError(f'max order must be at least {exchange_min_order():g} USDT: Nobitex '
                                                 'refuses USDT-market orders of 11 USDT or less')
                    cur[k] = v
            config.save_ceilings(cur)                  # validated (finite, typed, consistent) before writing
            print(json.dumps(config.load_ceilings(), indent=2))
        elif o.cmd == 'once':
            res = make_agent().step()
            print(json.dumps({'event': res['record'].get('event'), 'mode_reason': res['mode_reason'],
                              'offline': res['offline'], 'fresh': res.get('fresh'), 'holds': res.get('holds'),
                              'reason': res['record'].get('reason') or res['record'].get('message')}, ensure_ascii=False))
        elif o.cmd == 'run':
            if o.interval < 60:
                raise config.ConfigError('--interval below 60 s is not allowed')
            flag = config.home() / KEEP_FLAG
            if flag.exists() and not config.keep_flag_fresh(flag):
                flag.unlink()                          # a stale flag from an old restart never counts
            agent = make_agent(o.offline_grace)
            print(f'{VERSION} running; local ceilings: {json.dumps(config.load_ceilings())}; signer: '
                  f'{agent.exchange.signer}', flush=True)
            return serve(agent, o.interval, config.home(), out=lambda t: print(t, flush=True))
        elif o.cmd == 'safe-stop':
            res = make_agent().safe_shutdown()
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res['ok'] else 3
        elif o.cmd == 'resolve':
            from avid_core.cycle import ManualResolution, Store, resolve_manually
            if not o.not_placed and not o.closed:
                raise config.ConfigError('nothing to resolve: use --not-placed and/or --closed ORDER_ID')
            store = Store(config.home())
            pend = store.peek().get('pending') or {}
            confirm = None
            if o.not_placed:
                if not pend:
                    raise config.ConfigError('there is no unresolved placement')
                print(json.dumps({'unresolved_placement': {k: pend.get(k) for k in ('cid', 'asset', 'side', 'amount',
                                                                                     'price', 'day')}}, indent=2))
            ans = input('Did you check the Nobitex panel yourself (open AND recent orders)? type CHECKED: ')
            if ans.strip() != 'CHECKED':
                print('not changed')
                return 1
            if o.not_placed:
                confirm = input('type the clientOrderId shown above to confirm it does NOT exist: ')
            try:
                done = resolve_manually(store, pending_not_placed=o.not_placed, closing_ids=o.closed,
                                        client=make_exchange() if o.not_placed else None, confirm_cid=confirm)
            except ManualResolution as e:
                raise config.ConfigError(str(e)) from None
            print(json.dumps({'resolved': done}))
        elif o.cmd == 'reset-cid':
            from avid_core.cycle import Store, reset_client_order_id
            reset_client_order_id(Store(config.home()))
            print('clientOrderId will be tried (and verified) again on the next live order')
        elif o.cmd == 'status':
            try:
                a = config.load_agent()
            except config.ConfigError:
                a = {'cloud_url': None, 'agent_id': None}       # not paired yet: show the rest anyway
            st = _read_json(config.home() / 'agent_state.json')
            st = st if isinstance(st, dict) else {}
            d = st.get('directive') or {}
            res = {'cloud': a['cloud_url'], 'agent_id': a['agent_id'], 'ceilings': config.load_ceilings(),
                   'last_sync_ok': st.get('last_sync_ok'), 'last_mode': st.get('last_mode'),
                   'portfolio': (d.get('portfolio') or {}).get('name'),
                   'global_halt': d.get('global_halt'), 'revoked': st.get('revoked'),
                   'network': os.environ.get('NOBITEX_NETWORK') or 'mainnet',
                   'service': service.service_state(), 'engine': _engine_summary(), 'queued': _queued()}
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str) if o.json else _human_status(res))
    except (config.ConfigError, CloudError, NobitexError, service.ServiceError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


def _safe_stop_if_paired() -> dict:
    """`stop` / `uninstall` while the service is not running: cancel what the bot may have left open,
    if this machine is paired (an unpaired agent never had orders)."""
    try:
        agent = make_agent()
    except config.ConfigError as e:
        return {'ok': True, 'cancelled': False, 'message': f'not paired ({e}); nothing to cancel'}
    except NobitexError as e:                        # no usable credentials: say what may be open
        eng = _engine_summary()
        left = (eng.get('open_bot_orders') or []) + (eng.get('final_state_unknown') or []) \
            + ([f'lost-reply:{eng.get("lost_reply_client_order_id")}'] if eng.get('lost_reply_unresolved') else [])
        return {'ok': not left, 'cancelled': False, 'open_bot_orders': left,
                'message': f'cannot reach Nobitex ({str(e)[:150]})' + ('' if left else '; the bot has no open order')}
    return agent.safe_shutdown()


if __name__ == '__main__':
    sys.exit(main())
