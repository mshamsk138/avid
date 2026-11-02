"""Ed25519: RFC 8032 vectors, agreement with `cryptography`, key parsing (report 10.3, audit 33)."""
import base64
import sys

import pytest

from avid_core import ed25519

# RFC 8032, section 7.1, TEST 1 and TEST 2
VECTORS = [
    ('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60',
     'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a', '',
     'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b'),
    ('4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb',
     '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c', '72',
     '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00'),
]


@pytest.mark.parametrize('seed,pub,msg,sig', VECTORS)
def test_rfc8032_vectors_pure_python(seed, pub, msg, sig):
    s = bytes.fromhex(seed)
    assert ed25519.public_key(s).hex() == pub
    assert ed25519._pure_sign(s, bytes.fromhex(msg)).hex() == sig


def _require_usable_cryptography():
    """Skip (with the reason) when cryptography is missing OR installed but unusable here - e.g. a distro
    build for another Python that panics on import. The engine's own behaviour in that case is tested by
    test_broken_cryptography_install_fails_closed."""
    pytest.importorskip('cryptography', exc_type=ImportError)
    if ed25519.backend() != 'cryptography':
        pytest.skip(f'cryptography is installed but not usable here: {ed25519.backend_error()}')


@pytest.mark.parametrize('seed,pub,msg,sig', VECTORS)
def test_library_path_matches(seed, pub, msg, sig):
    _require_usable_cryptography()
    assert ed25519.backend() == 'cryptography'
    assert ed25519.sign(bytes.fromhex(seed), bytes.fromhex(msg)).hex() == sig


def test_pure_and_library_agree_on_random_messages():
    _require_usable_cryptography()
    import os
    for _ in range(20):
        seed, msg = os.urandom(32), os.urandom(50)
        assert ed25519._pure_sign(seed, msg) == ed25519.sign(seed, msg)


def test_backend_falls_back_and_pure_needs_opt_in(monkeypatch):
    monkeypatch.setitem(sys.modules, 'cryptography.hazmat.primitives.asymmetric.ed25519', None)
    assert ed25519.backend() == 'pure'
    monkeypatch.delenv('AVID_ALLOW_PURE_ED25519', raising=False)
    assert not ed25519.pure_allowed('testnet')
    monkeypatch.setenv('AVID_ALLOW_PURE_ED25519', '1')
    assert ed25519.pure_allowed('testnet')
    for net in ('mainnet', None, 'fake'):                     # no override outside the official testnet
        assert not ed25519.pure_allowed(net)
    seed = bytes.fromhex(VECTORS[0][0])
    assert ed25519.sign(seed, b'').hex() == VECTORS[0][3]


def test_parse_private_key_formats():
    seed = bytes.fromhex(VECTORS[0][0])
    pub = ed25519.public_key(seed)
    pem = ('-----BEGIN PRIVATE KEY-----\n' + base64.b64encode(ed25519._PKCS8_PREFIX + seed).decode()
           + '\n-----END PRIVATE KEY-----\n')
    for text in (seed.hex(), (seed + pub).hex(), base64.b64encode(seed).decode(),
                 base64.urlsafe_b64encode(seed + pub).decode().rstrip('='), pem, '0x' + seed.hex()):
        assert ed25519.parse_private_key(text) == seed
    for bad in ('', 'zz', (seed + b'\x00' * 32).hex(), base64.b64encode(b'\x01' * 16).decode(),
                '-----BEGIN PRIVATE KEY-----\n!!!\n-----END PRIVATE KEY-----'):
        with pytest.raises(ValueError):
            ed25519.parse_private_key(bad)


def test_sign_rejects_wrong_key_length():
    with pytest.raises(ValueError):
        ed25519.sign(b'\x00' * 31, b'x')


class _Panic(BaseException):
    """Stands in for pyo3_runtime.PanicException (a BaseException, NOT an Exception)."""


def test_broken_cryptography_install_fails_closed(monkeypatch):
    """PRA-03: a cryptography built for another Python raises PanicException on import. The signer
    must then fall back (live refused) instead of crashing every cycle - and with it STOP."""
    import builtins
    real_import = builtins.__import__

    def broken(name, *a, **kw):
        if name.startswith('cryptography'):
            raise _Panic('Python API call failed')
        return real_import(name, *a, **kw)
    monkeypatch.setattr(ed25519, '_BROKEN', None)
    monkeypatch.delitem(sys.modules, 'cryptography.hazmat.primitives.asymmetric.ed25519', raising=False)
    monkeypatch.setattr(builtins, '__import__', broken)
    assert ed25519.backend() == 'pure'
    assert 'Panic' in ed25519.backend_error()
    seed = bytes.fromhex(VECTORS[0][0])
    assert ed25519.sign(seed, b'').hex() == VECTORS[0][3]      # still signs (pure) for simulation
    monkeypatch.setattr(builtins, '__import__', real_import)
    monkeypatch.setattr(ed25519, '_BROKEN', None)
