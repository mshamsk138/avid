"""Agent local configuration. Everything lives on the customer's machine:

  ~/.avid-agent/agent.json      cloud URL + agent id (after pairing)
  ~/.avid-agent/agent_key       Ed25519 private key of this agent (chmod 600) - signs cloud requests
  ~/.avid-agent/ceilings.json   LOCAL HARD LIMITS set by the customer; the cloud can only tighten them
                                and can never enable live trading unless live_allowed is true here
  ~/.avid-agent/data/bot/       engine state + log (same format as AVID)
  ~/.nobitex/                   the Nobitex API key (never sent anywhere except Nobitex)

~/.avid-agent is kept at mode 0700 and every file in it at 0600, so other users of the machine can
neither read the key nor plant files or symlinks there (audit 38). Every number in ceilings.json goes
through avid_core.validation (AVID-02): NaN, Infinity, 1e309, true, "20", -1, 0 and 6.7 orders are
refused, unknown keys are refused (a typo must not silently fall back to a default), and the daily cap
must allow at least one full order.
"""
from __future__ import annotations
import base64
import binascii
import json
import os
import re
import time
from pathlib import Path

from avid_core import fsio
from avid_core.validation import CAP_KEYS, ValidationError, caps, loads_strict, number

HOME = Path(os.environ.get('AVID_AGENT_HOME', Path.home() / '.avid-agent'))
DEFAULT_CEILINGS = {'live_allowed': False, 'max_order_usdt': 20, 'max_orders_per_day': 6, 'max_daily_usdt': 100,
                    'max_spread': 0.01, 'min_cash_usdt': 0}
AGENT_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}')
KEEP_FLAG = 'keep-orders-on-next-stop'
KEEP_FLAG_MAX_AGE_S = 600


class ConfigError(RuntimeError):
    pass


def home() -> Path:
    return fsio.private_dir(HOME)


def _write_private(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    fsio.atomic_write(p, text, mode=0o600)


def _read_private(p: Path, what: str) -> str:
    """One O_NOFOLLOW descriptor for check and read (no TOCTOU); 0600 and owned by this user."""
    try:
        return fsio.read_private(p, what)
    except FileNotFoundError:
        raise ConfigError(f'{what} file {p} not found') from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(str(e)[:200]) from None


def check_agent_id(agent_id) -> str:
    """An agent id is sent as an HTTP header: letters, digits and _ . : - only (audit 16)."""
    if not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id):
        raise ConfigError('agent id from the cloud is not valid (expected up to 80 letters, digits, _ . : -)')
    return agent_id


def _seed(text: str) -> bytes:
    try:
        seed = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ConfigError('agent_key is not valid base64') from None
    if len(seed) != 32:
        raise ConfigError('agent_key must hold a 32-byte Ed25519 seed')
    return seed


def load_agent() -> dict:
    p = home() / 'agent.json'
    if not p.exists():
        raise ConfigError('agent is not paired yet: run  avid-agent pair --cloud <url> --code <code>')
    try:
        cfg = loads_strict(p.read_text())
    except ValidationError as e:
        raise ConfigError(f'agent.json is not valid: {e}') from None
    if not isinstance(cfg, dict):
        raise ConfigError('agent.json is not an object')
    cfg['cloud_url'] = check_cloud_url(cfg.get('cloud_url', ''))     # re-checked on every load
    cfg['agent_id'] = check_agent_id(cfg.get('agent_id'))
    key = home() / 'agent_key'
    cfg['seed'] = _seed(_read_private(key, 'agent key'))
    return cfg


def save_agent(cloud_url: str, agent_id: str, seed: bytes):
    url = check_cloud_url(cloud_url)
    agent_id = check_agent_id(agent_id)
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
        raise ConfigError('agent seed must be 32 bytes')
    _write_private(home() / 'agent_key', base64.b64encode(seed).decode())
    _write_private(home() / 'agent.json', json.dumps({'cloud_url': url, 'agent_id': agent_id}, indent=2))
    st = home() / 'agent_state.json'                    # a new pairing clears an old revocation
    if st.exists():
        try:
            d = loads_strict(st.read_text())
            if not isinstance(d, dict):
                raise ValidationError('not an object')
            d.pop('revoked', None)
            d['directive'] = None
            _write_private(st, json.dumps(d))
        except ValidationError:
            st.unlink()


def validate_ceilings(raw) -> dict:
    """Clean local ceilings or ConfigError. Every risk number is validated centrally (AVID-02)."""
    if not isinstance(raw, dict):
        raise ConfigError('ceilings.json must be a JSON object')
    unknown = set(raw) - set(DEFAULT_CEILINGS)
    if unknown:
        raise ConfigError(f'ceilings.json: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULT_CEILINGS)}')
    live = raw.get('live_allowed', False)
    if not isinstance(live, bool):
        raise ConfigError('ceilings.json: live_allowed must be true or false')
    merged = {**DEFAULT_CEILINGS, **raw}
    try:
        out = {'live_allowed': live, **caps({k: merged[k] for k in CAP_KEYS})}
        out['max_spread'] = number('max_spread', merged['max_spread'])
        out['min_cash_usdt'] = number('min_cash_usdt', merged['min_cash_usdt'])
    except ValidationError as e:
        raise ConfigError(f'ceilings.json: {e}') from None
    return out


def load_ceilings() -> dict:
    p = home() / 'ceilings.json'
    if not p.exists():
        _write_private(p, json.dumps(DEFAULT_CEILINGS, indent=2))
    try:
        raw = loads_strict(p.read_text())
    except (ValidationError, UnicodeDecodeError) as e:
        raise ConfigError(f'ceilings.json is not valid JSON: {e}') from None
    return validate_ceilings(raw)


def save_ceilings(c: dict):
    clean = validate_ceilings(c)
    _write_private(home() / 'ceilings.json', json.dumps(clean, indent=2, allow_nan=False))


def write_keep_flag(now: float | None = None):
    _write_private(home() / KEEP_FLAG, str(int(time.time() if now is None else now)))


def keep_flag_fresh(path: Path, now: float | None = None) -> bool:
    """A keep-orders flag counts only for KEEP_FLAG_MAX_AGE_S after it was written (audit 44): a flag left
    over from an old 'restart' must not make a later shutdown keep the bot's orders open."""
    now = time.time() if now is None else now
    try:
        st = path.stat()
        text = path.read_text().strip()
    except OSError:
        return False
    if now - st.st_mtime > KEEP_FLAG_MAX_AGE_S or st.st_mtime - now > 60:
        return False
    if text:
        try:
            ts = float(text)
        except ValueError:
            return False
        if abs(now - ts) > KEEP_FLAG_MAX_AGE_S:
            return False
    return True


LOOPBACK = ('localhost', '127.0.0.1', '::1')


def check_cloud_url(url: str) -> str:
    """https://host[:port] only; plain http only when the host IS a loopback address.
    No credentials, path, query or fragment (audit P0-10: 'http://localhost.attacker.example'
    used to pass a prefix check)."""
    from urllib.parse import urlsplit
    if not isinstance(url, str) or not url.strip():
        raise ConfigError('cloud URL is empty')
    raw = url.strip()
    if not raw.isascii() or any(ord(c) < 33 for c in raw):
        raise ConfigError('cloud URL must be plain ASCII without spaces or control characters')
    try:
        p = urlsplit(raw)
        host = p.hostname
        p.port                                            # raises ValueError on a bad port
    except ValueError:
        raise ConfigError('cloud URL is not valid') from None
    if p.username or p.password or '@' in p.netloc:
        raise ConfigError('cloud URL must not contain credentials')
    if p.query or p.fragment or raw.endswith(('?', '#')):
        raise ConfigError('cloud URL must not contain a query or fragment')
    if p.path not in ('', '/'):
        raise ConfigError('cloud URL must be just https://host[:port]')
    if not host or any(c in host for c in ' \\'):
        raise ConfigError('cloud URL has no valid host')
    if p.scheme == 'https':
        pass
    elif p.scheme == 'http' and host in LOOPBACK:
        pass
    else:
        raise ConfigError('cloud URL must use https:// (plain http only for localhost testing)')
    return f'{p.scheme}://{p.netloc}'.rstrip('/')
