"""Central validation of every number that limits risk (AVID-02, Invariant C).

One rule set for the CLI flags, the local ceilings file, the control file, the cloud policy and the
engine state: the value must be an int or float (never bool, never a string), finite
(math.isfinite: NaN, Infinity and 1e309 are refused), inside its documented range, and a whole number
for counts. Caps are also checked against each other. A failure raises ValidationError and callers
fail CLOSED (no new orders), never open.

JSON read from disk or from the network goes through loads_strict(): NaN/Infinity literals, numbers
that overflow to infinity, duplicate keys and very deep nesting are refused.
"""
from __future__ import annotations

import json
import math


class ValidationError(ValueError):
    pass


# name: (kind, low, high, low_inclusive, unit)
SPECS = {
    'max_order_usdt': ('num', 0.0, 1_000_000.0, False, 'USDT per order'),
    'max_daily_usdt': ('num', 0.0, 10_000_000.0, False, 'USDT per UTC day (filled + still open)'),
    'max_orders_per_day': ('int', 0, 1000, False, 'orders submitted per UTC day'),
    'min_order_usdt': ('num', 0.0, 1_000_000.0, False, 'USDT per order'),
    'min_cash_usdt': ('num', 0.0, 1e9, True, 'USDT kept free'),
    'max_spread': ('num', 0.0, 0.05, False, 'fraction of the mid price'),
    'fee_reserve': ('num', 0.0, 0.01, True, 'fraction of the order value'),
    'band_rel': ('num', 0.05, 0.90, True, 'fraction of the target weight'),
    'stale_minutes': ('num', 1.0, 1440.0, True, 'minutes'),
    'interval_s': ('num', 60.0, 86400.0, True, 'seconds'),
    'weight': ('num', 0.0, 1.0, True, 'fraction of the portfolio'),
}
CAP_KEYS = ('max_order_usdt', 'max_orders_per_day', 'max_daily_usdt')
MAX_JSON_DEPTH = 64
_HUGE_INT = 10 ** 15


def number(name: str, v, *, spec: tuple | None = None):
    """The validated value (float, or int for counts) or ValidationError."""
    kind, lo, hi, lo_incl, unit = spec or SPECS[name]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValidationError(f'{name} must be a number ({unit}), got {type(v).__name__}')
    if isinstance(v, int) and abs(v) > _HUGE_INT:
        raise ValidationError(f'{name} is out of range')
    if not math.isfinite(float(v)):
        raise ValidationError(f'{name} must be a finite number, got {v!r}')
    if kind == 'int':
        if isinstance(v, float) and not v.is_integer():
            raise ValidationError(f'{name} must be a whole number, got {v!r}')
        v = int(v)
    else:
        v = float(v)
    if v < lo or (v == lo and not lo_incl) or v > hi:
        rng = f'{"[" if lo_incl else "("}{lo:g}, {hi:g}]'
        raise ValidationError(f'{name}={v!r} outside the allowed range {rng} ({unit})')
    return v


def caps(d: dict, *, keys=CAP_KEYS, required=True) -> dict:
    """Validate the order caps of one source and check them against each other."""
    if not isinstance(d, dict):
        raise ValidationError('caps must be an object')
    out = {}
    for k in keys:
        if k not in d:
            if required:
                raise ValidationError(f'{k} is missing')
            continue
        out[k] = number(k, d[k])
    if 'max_order_usdt' in out and 'max_daily_usdt' in out and out['max_daily_usdt'] < out['max_order_usdt']:
        raise ValidationError(f'max_daily_usdt={out["max_daily_usdt"]:g} is below max_order_usdt='
                              f'{out["max_order_usdt"]:g}; the daily cap must allow at least one full order')
    return out


def sanitize(obj, _depth=0):
    """Copy of a JSON-like value with every non-finite float replaced by None (what we SEND must be
    valid JSON: a NaN must never reach the cloud's database)."""
    if _depth > MAX_JSON_DEPTH:
        return None
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): sanitize(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v, _depth + 1) for v in obj]
    return obj


def finite_nonneg(name, v):
    """A finite number >= 0 coming from the exchange or the state file (no upper bound)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or v < 0:
        raise ValidationError(f'{name} must be a finite number >= 0, got {v!r}')
    return float(v)


# ------------------------------------------------------------------ strict JSON
def _no_constants(c):
    raise ValidationError(f'non-finite number {c} in JSON')


def _finite_float(s):
    f = float(s)
    if not math.isfinite(f):
        raise ValidationError(f'number {s[:40]} overflows')
    return f


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValidationError(f'duplicate key {k[:40]!r} in JSON')
        out[k] = v
    return out


def _depth_ok(obj, limit):
    stack = [(obj, 1)]
    while stack:
        o, d = stack.pop()
        if d > limit:
            return False
        if isinstance(o, dict):
            stack.extend((v, d + 1) for v in o.values())
        elif isinstance(o, list):
            stack.extend((v, d + 1) for v in o)
    return True


def loads_strict(text, *, max_depth=MAX_JSON_DEPTH):
    """json.loads that refuses NaN/Infinity, overflowing numbers, duplicate keys and deep nesting."""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode('utf-8')
    if text.count('[') + text.count('{') > 200_000:
        raise ValidationError('JSON has too many nested values')
    try:
        obj = json.loads(text, parse_constant=_no_constants, parse_float=_finite_float,
                         object_pairs_hook=_no_dupes)
    except RecursionError:
        raise ValidationError('JSON nested too deeply') from None
    except ValidationError:
        raise
    except ValueError as e:
        raise ValidationError(f'invalid JSON: {str(e)[:120]}') from None
    if not _depth_ok(obj, max_depth):
        raise ValidationError(f'JSON nested deeper than {max_depth}')
    return obj
