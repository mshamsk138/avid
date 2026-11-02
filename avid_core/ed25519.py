"""Ed25519 signing (RFC 8032). Uses `cryptography` (OpenSSL, constant time) when installed, else a
pure-Python fallback.

Only signing is needed (Nobitex API-key auth, agent -> cloud requests). The pure-Python path follows the
RFC 8032 reference algorithm and is checked against the RFC test vectors and against `cryptography` in
tests - but Python big integers are NOT constant time, so its timing can depend on the private key
(report 10.3, audit 33). Therefore the engine refuses LIVE orders signed by the pure-Python code on
mainnet, without any override; on the official testnet AVID_ALLOW_PURE_ED25519=1 allows it for
testing. Install `python3-cryptography` (Debian/Ubuntu/Kali) or `pip install cryptography`.
"""
from __future__ import annotations
import base64
import hashlib
import os

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y, sign):
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if x & 1 != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _P
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _P
    C = 2 * P[3] * Q[3] * _D % _P
    D = 2 * P[2] * Q[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _compress(P):
    zi = _inv(P[2])
    x, y = P[0] * zi % _P, P[1] * zi % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, 'little')


def _h(m):
    return int.from_bytes(hashlib.sha512(m).digest(), 'little') % _Q


def _pure_sign(seed: bytes, msg: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], 'little')
    a &= (1 << 254) - 8
    a |= 1 << 254
    A = _compress(_mul(a, _G))
    r = _h(h[32:] + msg)
    R = _compress(_mul(r, _G))
    s = (r + _h(R + A + msg) * a) % _Q
    return R + int.to_bytes(s, 32, 'little')


def public_key(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], 'little')
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _compress(_mul(a, _G))


_BROKEN: str | None = None


def _lib():
    """The `cryptography` Ed25519 class, or None. A library that is missing is simply absent; a library
    that is installed but BROKEN (e.g. built for another Python: pyo3 raises PanicException, which is
    not even an Exception subclass) is remembered as broken - signing then uses the pure fallback and
    the engine refuses live orders - instead of crashing every cycle (and with it STOP)."""
    global _BROKEN
    if _BROKEN is not None:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey
    except ImportError:
        return None
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:                     # noqa: BLE001 - see docstring
        _BROKEN = f'{type(e).__name__}: {str(e)[:120]}'
        return None


def backend() -> str:
    """'cryptography' (constant time) or 'pure' (timing may depend on the key)."""
    return 'cryptography' if _lib() is not None else 'pure'


def backend_error() -> str | None:
    """Why an installed `cryptography` could not be used, or None."""
    _lib()
    return _BROKEN


def pure_allowed(network: str | None = None) -> bool:
    """The pure-Python signer may place LIVE orders only on the official testnet and only when the
    operator says so explicitly (AVID_ALLOW_PURE_ED25519=1). On mainnet there is no override: an
    environment variable copied from a test setup can never weaken a real account (report 5.2)."""
    return network == 'testnet' and os.environ.get('AVID_ALLOW_PURE_ED25519') == '1'


def sign(seed: bytes, msg: bytes) -> bytes:
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
        raise ValueError('Ed25519 private key must be 32 bytes')
    lib = _lib()
    if lib is not None:
        return lib.from_private_bytes(bytes(seed)).sign(msg)
    return _pure_sign(bytes(seed), msg)


_PKCS8_PREFIX = bytes.fromhex('302e020100300506032b657004220420')


def _seed_of(raw: bytes) -> bytes:
    """32-byte seed; a 64-byte seed||public key must carry the public key of that seed (a wrong or
    swapped half means a wrong key format, not a usable key - audit 33)."""
    if len(raw) == 32:
        return raw
    if len(raw) == 64:
        if public_key(raw[:32]) != raw[32:]:
            raise ValueError('64-byte key: the second half is not the public key of the first half')
        return raw[:32]
    raise ValueError('Ed25519 private key must be 32 bytes (or 64 bytes seed||public key)')


def parse_private_key(text: str) -> bytes:
    """Accept a PEM (PKCS#8) Ed25519 key, 64/128 hex chars, or base64 of 32/64 bytes."""
    s = (text or '').strip()
    if not s:
        raise ValueError('empty private key')
    if 'BEGIN' in s and 'PRIVATE KEY' in s:
        body = ''.join(l.strip() for l in s.splitlines() if l.strip() and not l.strip().startswith('-----'))
        try:
            der = base64.b64decode(body, validate=True)
        except Exception:
            raise ValueError('PEM body is not valid base64') from None
        if len(der) == 48 and der.startswith(_PKCS8_PREFIX):
            return der[16:]
        raise ValueError('PEM is not an Ed25519 PKCS#8 private key')
    hx = s.lower().removeprefix('0x')
    if len(hx) in (64, 128) and all(c in '0123456789abcdef' for c in hx):
        return _seed_of(bytes.fromhex(hx))
    pad = '=' * (-len(s) % 4)
    for dec in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = dec(s + pad)
        except Exception:
            continue
        if len(raw) in (32, 64):
            return _seed_of(raw)
    raise ValueError('could not parse private key (expected base64, hex or PEM Ed25519)')
