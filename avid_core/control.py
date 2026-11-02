"""AVID control file: settings the dashboard / agent may change while the bot runs.

data/bot/control.json is written by the dashboard (or the agent) and re-read by the bot every cycle.
The bot's command-line flags / the agent's local ceilings are HARD CEILINGS: the control file can
tighten the caps but never raise them, and it can switch to LIVE only if the bot was started with --live.

  {"mode": "dry" | "live",
   "weights": {"btc": 0.34, "paxg": 0.33, "usdt": 0.33},
   "band_rel": 0.25,
   "max_order_usdt": 20, "max_orders_per_day": 6, "max_daily_usdt": 100,
   "max_spread": 0.01, "min_cash_usdt": 0}

Every key is optional. Every number goes through avid_core.validation (finite, typed, in range).
Fail-closed rules (report 10.2) are applied by cycle.py: an INVALID file, or a MISSING file after a
valid one was in use, never falls back to the wider CLI settings - the last valid control file stays in
force and no new order is placed until a valid file is back.
"""
from __future__ import annotations
import datetime as dt
import json
import math
import types
from pathlib import Path

from . import fsio
from .rebalance import MARKETS
from .validation import CAP_KEYS, ValidationError, loads_strict, number

ASSETS = tuple(MARKETS) + ('usdt',)
BAND_RANGE = (0.05, 0.90)
RISK_KEYS = ('max_spread', 'min_cash_usdt')


class ControlError(ValueError):
    pass


def control_path(root) -> Path:
    return Path(root) / 'data/bot/control.json'


def _num(name, v):
    try:
        return number(name, v)
    except ValidationError as e:
        raise ControlError(str(e)) from None


def validate(obj, ceilings: dict | None = None) -> dict:
    """Return a clean control dict or raise ControlError (message in plain words)."""
    if not isinstance(obj, dict):
        raise ControlError('control must be a JSON object')
    out = {}
    if 'mode' in obj:
        if obj['mode'] not in ('dry', 'live'):
            raise ControlError("mode must be 'dry' or 'live'")
        out['mode'] = obj['mode']
    if 'weights' in obj:
        w = obj['weights']
        if not isinstance(w, dict) or not w:
            raise ControlError('weights must be an object like {"btc": 0.5, "usdt": 0.5}')
        clean = {}
        for k, v in w.items():
            k = str(k).lower()
            if k not in ASSETS:
                raise ControlError(f'unsupported asset {k!r}; supported: {", ".join(ASSETS)}')
            if k in clean:
                raise ControlError(f'asset {k!r} listed twice')
            clean[k] = _num('weight', v)
        for k in ASSETS:                       # explicit: an asset left out means weight 0 (sell it)
            clean.setdefault(k, 0.0)
        if abs(sum(clean.values()) - 1) > 1e-6:
            raise ControlError(f'weights must sum to 1 (got {sum(clean.values()):.4f})')
        out['weights'] = clean
    if 'band_rel' in obj:
        out['band_rel'] = _num('band_rel', obj['band_rel'])
    for k in CAP_KEYS + RISK_KEYS:
        if k not in obj:
            continue
        v = _num(k, obj[k])
        if ceilings and ceilings.get(k) is not None:
            c = ceilings[k]
            if k == 'min_cash_usdt':
                if v < c - 1e-9:
                    raise ControlError(f'{k}={v} is below the floor {c} set when the bot was started')
            elif v > c + 1e-9:
                raise ControlError(f'{k}={v} is above the ceiling {c} set when the bot was started')
        out[k] = v
    if 'max_order_usdt' in out and 'max_daily_usdt' in out and out['max_daily_usdt'] < out['max_order_usdt']:
        raise ControlError('max_daily_usdt is below max_order_usdt')
    return out


def read(root):
    """(control dict, error message or None, file present?)."""
    p = control_path(root)
    if not p.exists():
        return {}, None, False
    try:
        return validate(loads_strict(p.read_text())), None, True
    except (ValueError, OSError, UnicodeDecodeError) as e:   # ControlError / ValidationError are ValueErrors
        return {}, f'control.json invalid: {str(e)[:200]}', True


def load(root):
    """(control dict, error message or None). Missing file -> ({}, None)."""
    ctl, err, _ = read(root)
    return ctl, err


def save(root, ctl: dict, *, audit: dict | None = None) -> dict:
    """Atomically and durably write a validated control file and append an audit line."""
    clean = validate({k: v for k, v in ctl.items() if k != 'updated_at'})
    clean['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    p = control_path(root)
    fsio.private_dir(p.parent)
    fsio.atomic_write(p, json.dumps(clean, indent=2, allow_nan=False))
    if audit is not None:
        fsio.append_line(p.parent / 'control_audit.jsonl',
                         json.dumps({'at': clean['updated_at'], **audit}, default=str), durable=True)
    return clean


def effective(cfg, ctl: dict, *, live_allowed: bool):
    """Merge CLI cfg (ceilings) with the control file -> (cfg copy, live: bool)."""
    eff = types.SimpleNamespace(**vars(cfg))
    if 'weights' in ctl:
        eff.weights = dict(ctl['weights'])
    if 'band_rel' in ctl:
        eff.band_rel = ctl['band_rel']
    for k in CAP_KEYS:
        if k in ctl:
            setattr(eff, k, min(getattr(cfg, k), ctl[k]))     # can tighten, never loosen
    if 'max_spread' in ctl:
        eff.max_spread = min(float(getattr(cfg, 'max_spread', 0.01)), ctl['max_spread'])
    if 'min_cash_usdt' in ctl:
        eff.min_cash_usdt = max(float(getattr(cfg, 'min_cash_usdt', 0) or 0), ctl['min_cash_usdt'])
    mode = ctl.get('mode')
    live = live_allowed and mode != 'dry'                      # 'live' cannot exceed --live
    return eff, live


def check_cfg(cfg) -> str | None:
    """None when every risk number of the effective config is valid, else the reason (Invariant C).
    A NaN cap from any source must stop new orders, not disable the cap."""
    try:
        for k in CAP_KEYS + ('min_order_usdt', 'band_rel', 'stale_minutes'):
            number(k, getattr(cfg, k))
        number('max_spread', float(getattr(cfg, 'max_spread', 0.01)))
        number('min_cash_usdt', float(getattr(cfg, 'min_cash_usdt', 0) or 0))
        number('fee_reserve', float(getattr(cfg, 'fee_reserve', 0.003)))
        w = getattr(cfg, 'weights')
        if not isinstance(w, dict) or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                              and math.isfinite(v) and v >= 0 for v in w.values()):
            return 'weights are not finite numbers >= 0'
        if abs(sum(w.values()) - 1) > 1e-6:
            return 'weights do not sum to 1'
    except (ValidationError, AttributeError, TypeError) as e:
        return str(e)
    return None
