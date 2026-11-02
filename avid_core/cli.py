"""avid-engine: the standalone engine command line.

    avid-engine check      read the account (balances, books, weights) - sends nothing
    avid-engine plan       what the engine would do NOW against the real account - read-only, never cancels
    avid-engine simulate   offline replay of a stored scenario through the real engine - no network at all
    avid-engine run        the rebalancing loop: dry run unless --live is given
    avid-engine status     local state (orders, caps, STOP, alerts) from data/bot - no network
    avid-engine help [cmd]

Output is human-readable; --json prints the machine-readable record instead. Exit codes: 0 ok,
1 needs attention (status), 2 usage / configuration / exchange error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, ed25519, fsio
from .validation import ValidationError, loads_strict, number

DEFAULT_SECRETS_DIR = '/run/secrets/nobitex'
DEFAULT_WEIGHTS = 'btc=0.34,paxg=0.33,usdt=0.33'
COMMANDS = ('check', 'plan', 'simulate', 'run', 'status', 'help')


def secrets_dir() -> str:
    return os.environ.get('NOBITEX_SECRETS_DIR', DEFAULT_SECRETS_DIR)


# ------------------------------------------------------------------------------------------ parser
def _risk_options(p):
    g = p.add_argument_group('strategy and risk ceilings (validated; control.json may only tighten them)')
    g.add_argument('--weights', default=DEFAULT_WEIGHTS, help=f'target weights (default {DEFAULT_WEIGHTS})')
    g.add_argument('--band-rel', type=float, default=0.25,
                   help='trade when a weight is off target by more than this fraction of it (default 0.25)')
    g.add_argument('--min-order-usdt', type=float, default=11.5,
                   help='Nobitex requires more than 11 USDT per order on USDT markets; never below that')
    g.add_argument('--max-order-usdt', type=float, default=20)
    g.add_argument('--max-orders-per-day', type=int, default=6)
    g.add_argument('--max-daily-usdt', type=float, default=100)
    g.add_argument('--stale-minutes', type=float, default=30)
    g.add_argument('--max-spread', type=float, default=0.01)
    g.add_argument('--min-cash-usdt', type=float, default=0.0)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='avid-engine', description='AVID rebalancing engine (Nobitex). '
                                 'Nothing is traded unless `run --live` is given.',
                                 epilog='Run `avid-engine help <command>` for the options of one command.')
    ap.add_argument('--version', action='version', version=f'avid-engine {__version__}')
    sub = ap.add_subparsers(dest='cmd', metavar='command')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', help='machine-readable JSON output')
    rooted = argparse.ArgumentParser(add_help=False)
    rooted.add_argument('--root', default='.', help='engine root; state lives in <root>/data/bot (default .)')

    p = sub.add_parser('check', parents=[common], help='read balances, books and weights (sends nothing)')
    p.add_argument('--weights', default=DEFAULT_WEIGHTS)
    p = sub.add_parser('plan', parents=[common, rooted], help='read-only preview against the real account')
    _risk_options(p)
    p = sub.add_parser('simulate', parents=[common],
                       help='offline replay of a stored scenario (no network, no credentials, no real order)')
    src = p.add_mutually_exclusive_group()
    src.add_argument('--example', default=None, help='a bundled example: btc-usdt (default), btc-paxg-usdt')
    src.add_argument('--scenario', default=None, help='path of a scenario JSON file (format avid-sim-scenario/1)')
    p.add_argument('--steps', type=int, default=None, help='replay only the first N steps')
    p.add_argument('--root', default=None,
                   help='keep the simulated engine data here (an empty folder, or one from an earlier simulation); '
                        'default: a temporary folder that is deleted afterwards')
    p.add_argument('--list', action='store_true', help='list the bundled examples and exit')
    p = sub.add_parser('run', parents=[common, rooted], help='the rebalancing loop (dry run unless --live)')
    p.add_argument('--live', action='store_true', help='actually send orders (default: dry run)')
    p.add_argument('--once', action='store_true', help='one cycle, then exit')
    p.add_argument('--interval', type=float, default=600, help='seconds between cycles (60..86400, default 600)')
    _risk_options(p)
    sub.add_parser('status', parents=[common, rooted], help='local engine state; reads files only, no network')
    p = sub.add_parser('help', help='show help for a command')
    p.add_argument('topic', nargs='?', choices=COMMANDS[:-1])
    return ap


def _cli_number(ap, name, v):
    try:
        return number(name, v)
    except ValidationError as e:
        ap.error(str(e))


def _validate_risk(ap, cfg):
    from .rebalance import parse_weights
    try:
        cfg.weights = parse_weights(cfg.weights)
    except ValueError as e:
        ap.error(str(e))
    for name, attr in (('band_rel', 'band_rel'), ('min_order_usdt', 'min_order_usdt'),
                       ('max_order_usdt', 'max_order_usdt'), ('max_orders_per_day', 'max_orders_per_day'),
                       ('max_daily_usdt', 'max_daily_usdt'), ('stale_minutes', 'stale_minutes'),
                       ('max_spread', 'max_spread'), ('min_cash_usdt', 'min_cash_usdt')):
        setattr(cfg, attr, _cli_number(ap, name, getattr(cfg, attr)))
    if hasattr(cfg, 'interval'):
        cfg.interval = _cli_number(ap, 'interval_s', cfg.interval)
    if cfg.max_daily_usdt < cfg.max_order_usdt:
        ap.error('--max-daily-usdt must be at least --max-order-usdt')


# ------------------------------------------------------------------------------------------ output
def _out(obj, as_json, human):
    if as_json:
        print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))
    else:
        print(human(obj))


def _w(weights) -> str:
    return '  '.join(f'{a} {float(w):.4f}' for a, w in sorted((weights or {}).items(), key=lambda x: x[0]))


def _decision_lines(d) -> list[str]:
    o = d.get('order')
    band = ', '.join(f'{a} {lo:.3f}-{hi:.3f}' for a, (lo, hi) in sorted(d.get('band', {}).items()))
    lines = [f'  Portfolio Value  {d["portfolio_value_usdt"]:.2f} USDT (estimated at mid prices)'
             if d.get('portfolio_value_usdt') is not None else '  Portfolio Value  unknown',
             f'  Current Weights  {_w(d.get("current_weights"))}',
             f'  Target Weights   {_w({a: w for a, w in d.get("target_weights", {}).items() if w > 0})}',
             f'  Band             ±{d["band_rel"]:.0%} of each target ({band})',
             f'  Decision         {d.get("decision")}']
    if o:
        lines += [f'  Order Amount     {o["amount"]} {o["asset"].upper()} ({o["side"].upper()} on {o["market"]})',
                  f'  Order Price      {o["price"]} USDT, {o["type"]}; notional {o["notional_usdt"]:.2f} USDT']
    else:
        lines += ['  Order Amount     none', '  Order Price      none']
    checks = d.get('risk_checks') or []
    lines.append('  Risk Checks      ' + ('; '.join(f'{c["check"]} {"OK" if c["ok"] else "FAILED"} ({c["detail"]})'
                                                for c in checks) if checks else 'not reached (no order proposed)'))
    lines.append(f'  Reason           {d.get("reason")}')
    return lines


def human_simulation(r) -> str:
    out = [f'AVID offline SIMULATION - {r["scenario"]}: {r["title"]}',
           '  ' + r['notice'],
           f'  {r["steps"]} steps of {r["interval_s"]:.0f} s, {r["start"]} .. {r["end"]}; fill rule: '
           f'{r["assumptions"]["fill_rule"]}; assumed fee {r["assumptions"]["fee_rate"]:.2%}', '']
    d = r.get('first_decision')
    if d:
        out.append(f'First decision (step {d["step"]}, {d["at"]})')
        out += _decision_lines(d)
        out.append('')
    out.append('Timeline (steps where something happened)')
    for t in r['timeline']:
        for f in t['fills']:
            out.append(f'  {t["at"]}  FILL   {f["side"]} {f["amount"]:g} {f["asset"]} @ {f["price"]:g} '
                       f'(fee {f["fee"]:.8g} {f["fee_asset"]})')
        if t['event'] not in ('no_trade', 'waiting'):
            out.append(f'  {t["at"]}  {t["event"]:<20} {t["summary"]}')
    o = r['orders']
    out += ['', 'Result',
            f'  Orders           placed {o["placed"]}, filled {o["filled"]}, cancelled {o["canceled"]}, '
            f'errors {r["errors"]}',
            f'  Fees (realized)  {r["fees_usdt_realized"]:.4f} USDT in the simulation',
            f'  Start            {r["start_value_usdt_estimated"]:.2f} USDT estimated; weights {_w(r["start_weights"])}',
            f'  End              {r["end_value_usdt_estimated"]:.2f} USDT estimated; weights {_w(r["end_weights"])}',
            f'  Holding instead  {r["hold_value_usdt_estimated"]:.2f} USDT estimated (start balances at the end prices)',
            '  Estimated = valued at mid prices; realized = executed fills of the simulated exchange.',
            '  Rebalancing manages weights; it does not guarantee a profit.']
    if r.get('engine_root'):
        out.append(f'  Engine data kept in {r["engine_root"]}/data/bot')
    return '\n'.join(out)


def human_record(rec) -> str:
    """One engine cycle record (plan / run) for a person."""
    ev = rec.get('event')
    head = f'{rec.get("at", "")} {rec.get("mode", "")} {ev}'.strip()
    if 'value_usdt' in rec:
        from .sim import decision_view
        cfg = {'band_rel': rec.get('band_rel') or 0.25}
        return '\n'.join([head] + _decision_lines(decision_view(rec, cfg)))
    detail = rec.get('reason') or rec.get('message') or rec.get('cap') or ''
    return f'{head}: {detail}' if detail else head


# ------------------------------------------------------------------------------------------ commands
def _client():
    from .client import NobitexClient, read_credentials
    cred = read_credentials(secrets_dir())
    if cred['mode'] == 'api_key':
        return NobitexClient(api_key=cred['api_key'], seed=cred['seed'])
    return NobitexClient(cred['token'])


def cmd_check(ap, cfg):
    from .rebalance import MARKETS, parse_weights
    from .records import _wallets
    try:
        targets = parse_weights(cfg.weights)
    except ValueError as e:
        ap.error(str(e))
    client = _client()
    managed = sorted(MARKETS)
    total, avail = _wallets(client, tuple(managed) + ('usdt',))
    books = {a: client.best_bid_ask(MARKETS[a]['symbol']) for a in managed
             if total.get(a, 0) > 0 or targets.get(a, 0) > 0}
    vals = {a: total[a] * sum(books[a]) / 2 for a in books}
    vals['usdt'] = total['usdt']
    value = sum(vals.values())
    res = {'auth_mode': client.auth_mode, 'network': client.network, 'signer': client.signer, 'total': total,
           'available': avail, 'books': books, 'portfolio_value_usdt': round(value, 2),
           'weights': {a: round(v / value, 4) for a, v in vals.items()} if value else None,
           'targets': targets, 'orders_sent': 0}
    _out(res, cfg.json, lambda r: '\n'.join([
        f'Account check ({r["network"]}, {r["auth_mode"]} auth, signer {r["signer"]}) - read-only, 0 orders sent',
        f'  Balances (total)      {_w(r["total"])}', f'  Balances (available)  {_w(r["available"])}',
        '  Books                 ' + '  '.join(f'{a} {b[0]:g}/{b[1]:g}' for a, b in r['books'].items()),
        f'  Portfolio Value       {r["portfolio_value_usdt"]:.2f} USDT (estimated at mid prices)',
        f'  Current Weights       {_w(r["weights"])}',
        f'  Target Weights        {_w({a: w for a, w in r["targets"].items() if w > 0})}']))
    return 0


def cmd_plan(ap, cfg):
    from .cycle import cycle
    from .store import PreviewStore
    _validate_risk(ap, cfg)
    client = _client()
    rec = cycle(client, PreviewStore(Path(cfg.root).resolve()), cfg, live=False, may_cancel=False)
    rec.setdefault('band_rel', cfg.band_rel)
    _out(rec, cfg.json, lambda r: 'PLAN (read-only preview: nothing is sent or cancelled)\n' + human_record(r))
    return 0


def cmd_simulate(ap, cfg):
    from . import sim
    if cfg.list:
        for name in sim.EXAMPLES:
            s = sim.load_example(name)
            print(f'{name:<16} {s.title} ({s.steps} steps)')
        return 0
    try:
        scn = sim.load_file(cfg.scenario) if cfg.scenario else sim.load_example(cfg.example or 'btc-usdt')
        if cfg.steps is not None and cfg.steps < 1:
            ap.error('--steps must be at least 1')
        report = sim.run(scn, root=cfg.root, steps=cfg.steps, keep=cfg.root is not None)
    except sim.ScenarioError as e:
        print(f'avid-engine simulate: {e}', file=sys.stderr)
        return 2
    _out(report, cfg.json, human_simulation)
    return 0


def cmd_run(ap, cfg):
    from .client import NobitexError
    from .cycle import cycle
    from .store import Store
    _validate_risk(ap, cfg)
    client = _client()
    store = Store(Path(cfg.root).resolve())
    runtime = {'mode': 'LIVE' if cfg.live else 'DRY_RUN', 'live_allowed': bool(cfg.live),
               'version': f'AVID {__version__}', 'network': client.network, 'targets': cfg.weights,
               'band_rel': cfg.band_rel, 'max_order_usdt': cfg.max_order_usdt, 'min_order_usdt': cfg.min_order_usdt,
               'max_orders_per_day': cfg.max_orders_per_day, 'max_daily_usdt': cfg.max_daily_usdt,
               'interval_s': cfg.interval, 'stale_minutes': cfg.stale_minutes, 'auth_mode': client.auth_mode,
               'signer': client.signer, 'started_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}
    fsio.atomic_write(store.dir / 'runtime.json', json.dumps(runtime, indent=2), durable=False)
    print(json.dumps(runtime) if cfg.json else
          f'avid-engine {runtime["mode"]} on {runtime["network"]} ({runtime["auth_mode"]}, signer {runtime["signer"]}); '
          f'targets {_w(cfg.weights)}; every {cfg.interval:.0f} s', flush=True)
    while True:
        try:
            rec = cycle(client, store, cfg, live=cfg.live)
        except NobitexError as e:
            rec = store.log('error', level='error', message=str(e))
        except Exception as e:                    # never let one bad cycle kill the service
            rec = store.log('error', level='error', message=f'UNEXPECTED {type(e).__name__}: {str(e)[:300]}')
        rec.setdefault('band_rel', cfg.band_rel)
        print(json.dumps(rec, default=str) if cfg.json else human_record(rec), flush=True)
        if cfg.once:
            return 0
        time.sleep(cfg.interval)


def _read_json(path: Path):
    try:
        return loads_strict(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return {'_unreadable': f'{type(e).__name__}: {str(e)[:120]}'}


def _last_log(path: Path, n=5) -> list:
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode('utf-8', 'replace').splitlines()[1:] or []
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            r = loads_strict(line)
            out.append({k: r.get(k) for k in ('ts', 'event', 'level', 'reason', 'message') if r.get(k) is not None})
        except ValueError:
            out.append({'event': 'unreadable log line'})
    return out


def engine_status(root) -> dict:
    """Everything `status` shows, read from files only (no lock, no write, no network)."""
    from .store import check_state
    bot = Path(root).resolve() / 'data' / 'bot'
    res = {'root': str(Path(root).resolve()), 'engine_data': bot.is_dir(), 'attention': []}
    if not bot.is_dir():
        res['attention'].append(f'no engine data in {bot} (the engine has not run here)')
        return res
    raw = _read_json(bot / 'state.json')
    if isinstance(raw, dict) and '_unreadable' not in raw:
        try:
            st = check_state(raw)
        except ValueError as e:
            st, res['state_error'] = None, str(e)[:200]
    else:
        st = None
        res['state_error'] = (raw or {}).get('_unreadable', 'state.json is missing')
    stop = [n for n in ('STOP', 'STOP.cloud') if (bot / n).exists()]
    eff = _read_json(bot / 'effective.json') or {}
    res.update(stop_files=stop, mode=eff.get('mode'), holds=eff.get('holds'), signer=eff.get('signer'),
               simulation=(bot / 'SIMULATION').is_file(), last_cycles=_last_log(bot / 'log.jsonl'))
    if st:
        res.update(day=st.get('day'), orders_today=st.get('orders_today'),
                   notional_today=round(float(st.get('notional_today') or 0), 2),
                   open_bot_orders={oid: {k: o.get(k) for k in ('asset', 'side', 'amount', 'price', 'state', 'cid')}
                                    for oid, o in (st.get('bot_orders') or {}).items()},
                   closing=sorted(st.get('closing') or {}), pending=st.get('pending'),
                   fills_pending=len(st.get('fills_pending') or {}), cid_supported=st.get('cid_supported'),
                   alerts=sorted((st.get('alerts') or {}).keys()) if isinstance(st.get('alerts'), dict) else [],
                   locked_day=st.get('locked_day'))
        if st.get('pending'):
            res['attention'].append('an order whose placement reply was lost is not settled (no new orders)')
        if st.get('closing'):
            res['attention'].append('final state of bot order(s) ' + ','.join(sorted(st['closing'])) + ' not known yet')
        if res['alerts']:
            res['attention'].append('alerts: ' + ', '.join(res['alerts']))
    if res.get('state_error'):
        res['attention'].append('state: ' + res['state_error'])
    if stop:
        res['attention'].append('kill switch present: ' + ', '.join(stop))
    return res


def cmd_status(ap, cfg):
    res = engine_status(cfg.root)

    def human(r):
        lines = [f'AVID engine status ({r["root"]}) - local files only, nothing was sent']
        if not r['engine_data']:
            return '\n'.join(lines + ['  ' + a for a in r['attention']])
        if r.get('simulation'):
            lines.append('  This folder holds SIMULATED engine data (avid-engine simulate), not a real account.')
        lines += [f'  Mode            {r.get("mode") or "unknown"}; signer {r.get("signer") or "unknown"}',
                  f'  Kill switch     {", ".join(r["stop_files"]) or "none"}',
                  f'  Day (UTC)       {r.get("day")}: {r.get("orders_today")} orders, {r.get("notional_today")} USDT '
                  'committed']
        for oid, o in (r.get('open_bot_orders') or {}).items():
            lines.append(f'  Open bot order  {oid}: {o["side"]} {o["amount"]} {o["asset"]} @ {o["price"]} [{o["state"]}]')
        if r.get('fills_pending'):
            lines.append(f'  Reconciliation  {r["fills_pending"]} order(s) not yet matched to executions')
        for c in r.get('last_cycles') or []:
            lines.append(f'  Log             {c.get("ts", "")} {c.get("event", "")} {c.get("reason") or c.get("message") or ""}'
                         .rstrip()[:160])
        lines += ['  ATTENTION       ' + a for a in r['attention']] or ['  Attention       nothing']
        return '\n'.join(lines)
    _out(res, cfg.json, human)
    return 1 if res['attention'] and res['engine_data'] else 0


def main(argv=None):
    ap = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    cfg = ap.parse_args(argv)
    if cfg.cmd in (None, 'help'):
        topic = getattr(cfg, 'topic', None)
        if topic:
            ap.parse_args([topic, '--help'])          # prints the sub-command help and exits 0
        ap.print_help()
        return 0
    from .client import NobitexError
    try:
        return {'check': cmd_check, 'plan': cmd_plan, 'simulate': cmd_simulate, 'run': cmd_run,
                'status': cmd_status}[cfg.cmd](ap, cfg)
    except NobitexError as e:
        print(json.dumps({'error': str(e)}) if getattr(cfg, 'json', False) else f'avid-engine: {e}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
