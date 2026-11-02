"""Validation of what the cloud tells the agent to do (trust boundary B4, audit 18, AVID-02).

The cloud is not trusted. Its directive is checked field by field before anything uses it; a directive
that fails is not applied at all (the sync counts as NOT fresh, so no new order is placed) and the
customer is told why. Valid values are still bounded by the LOCAL ceilings afterwards (engine._apply):
the cloud can tighten, never loosen.

  {"global_halt": false,
   "portfolio": null | {
      "name": "...", "mode": "live" | "simulation" | ..., "kill_switch": false,
      "policy": null | {"version_id": "...", "weights": {"btc": 0.5, "usdt": 0.5}, "band_rel": 0.25,
                        "max_order_usdt": 20, "max_orders_per_day": 6, "max_daily_usdt": 100,
                        "min_cash_usdt": 0, "max_spread": 0.01, "expires_at": <epoch ms, optional>}}}
"""
from __future__ import annotations

import re

from avid_core import control
from avid_core.validation import CAP_KEYS, ValidationError, caps, number

MODE_RE = re.compile(r'[a-z_]{1,32}')


class DirectiveError(ValueError):
    pass


def _bool(name, v, default=False):
    if v is None:
        return default
    if not isinstance(v, bool):
        raise DirectiveError(f'{name} must be true or false')
    return v


def _str(name, v, n, required=False):
    if v is None and not required:
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str) or not (0 < len(v) <= n) or any(ord(c) < 32 for c in v):
        raise DirectiveError(f'{name} must be a short text')
    return v


def validate_policy(pol) -> dict:
    if not isinstance(pol, dict):
        raise DirectiveError('policy must be an object')
    try:
        out = {'version_id': _str('policy.version_id', pol.get('version_id'), 64, required=True)}
        if 'weights' not in pol:
            raise DirectiveError('policy.weights is missing')
        out['weights'] = control.validate({'weights': pol['weights']})['weights']
        out['band_rel'] = number('band_rel', pol.get('band_rel'))
        out.update(caps({k: pol.get(k) for k in CAP_KEYS if k in pol}))
        out['min_cash_usdt'] = number('min_cash_usdt', pol.get('min_cash_usdt', 0))
        out['max_spread'] = number('max_spread', pol.get('max_spread', 0.01))
        exp = pol.get('expires_at')
        if exp is not None:
            if isinstance(exp, bool) or not isinstance(exp, int) or exp <= 0:
                raise DirectiveError('policy.expires_at must be epoch milliseconds')
            out['expires_at'] = exp
    except (ValidationError, control.ControlError) as e:
        raise DirectiveError(f'policy: {e}') from None
    return out


def validate(d) -> dict:
    """Clean directive or DirectiveError."""
    if not isinstance(d, dict):
        raise DirectiveError('directive must be an object')
    out = {'global_halt': _bool('global_halt', d.get('global_halt')), 'portfolio': None}
    port = d.get('portfolio')
    if port is None:
        return out
    if not isinstance(port, dict):
        raise DirectiveError('portfolio must be an object or null')
    mode = port.get('mode')
    if not isinstance(mode, str) or not MODE_RE.fullmatch(mode):
        raise DirectiveError('portfolio.mode must be a short lowercase word')
    pol = port.get('policy')
    out['portfolio'] = {'name': _str('portfolio.name', port.get('name'), 200), 'mode': mode,
                        'kill_switch': _bool('portfolio.kill_switch', port.get('kill_switch')),
                        'policy': None if pol is None else validate_policy(pol)}
    return out
