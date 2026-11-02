"""Signed client for the AVID Cloud agent protocol, version 2 (stdlib only).

Transport: redirects are never followed (a 3xx is an error) and the cloud URL is validated again here,
so a tampered agent.json cannot point the agent at an arbitrary host (audit P0-10). The request body is
strict JSON (no NaN); the response must be at most 4 MB of strict JSON (no NaN/Infinity, no duplicate
keys, nesting depth <= 64), otherwise it is a CloudError - never an unhandled exception (audit 18).

Signature (unchanged, v1-compatible): Ed25519 over  ts \\n nonce \\n METHOD \\n path \\n sha256(body).
The body carries `protocol`, `agent_id`, `batch_id` and `audience` (the cloud origin the agent talks
to), so the signed hash binds the request to this deployment, this agent and this batch.

Sync acknowledgement (AVID-06). A sync answer is accepted only when it is
  {"protocol": 2, "agent_id": <this agent>, "batch_id": <the batch sent>,
   "ack": {"accepted": [item_key, ...], "rejected": [{"item_key": ..., "reason": ...}, ...]},
   "directive": {...}}
Anything else - HTTP 200 with an error object, another agent or batch, keys that were not sent, a key
both accepted and rejected - is an invalid ACK: NOTHING is deleted from the outbox and the sync is not
fresh. Only accepted keys are deleted; a resend after a lost reply is safe because the cloud stores by
item_key idempotently. See docs/AGENT_PROTOCOL.md.
"""
from __future__ import annotations
import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

from avid_core import ed25519
from avid_core.client import no_redirect_opener
from avid_core.validation import ValidationError, loads_strict, sanitize

from . import __version__
from .directive import DirectiveError, validate as validate_directive

UA = f'AVID-Agent/{__version__}'
PROTOCOL = 2
MAX_RESPONSE_BYTES = 4_000_000
MAX_ACK_KEYS = 20_000


class CloudError(RuntimeError):
    def __init__(self, msg, status=None, code=None, retry_after=None):
        super().__init__(msg)
        self.status, self.code, self.retry_after = status, code, retry_after

    @property
    def revoked(self) -> bool:
        """The cloud says this agent is revoked or unknown: a permanent stop, not a network blip."""
        return self.status in (401, 403) and self.code in ('agent_revoked', 'agent_unknown')

    @property
    def unauthorized(self) -> bool:
        return self.status in (401, 403)


def _message(ts, nonce, method, path, body: bytes) -> bytes:
    return '\n'.join([ts, nonce, method.upper(), path, hashlib.sha256(body).hexdigest()]).encode()


def _retry_after(headers):
    try:
        v = float(headers.get('Retry-After'))
    except (TypeError, ValueError, AttributeError):
        return None
    return max(0.0, min(3600.0, v)) if v == v else None


class CloudClient:
    def __init__(self, base_url: str, agent_id: str | None = None, seed: bytes | None = None, opener=None,
                 timeout=20.0, clock=time.time):
        from .config import check_agent_id, check_cloud_url
        self.base = check_cloud_url(base_url)
        self.agent_id = check_agent_id(agent_id) if agent_id is not None else None
        self._seed = seed
        self.opener = opener or no_redirect_opener()
        self.timeout = timeout
        self.clock = clock

    def __repr__(self):
        return f'<CloudClient {self.base} agent={self.agent_id} key=***>'

    def _post(self, path, payload, signed):
        try:
            body = json.dumps(sanitize(payload), separators=(',', ':'), ensure_ascii=False, allow_nan=False,
                              default=str).encode()
        except (TypeError, ValueError) as e:
            raise CloudError(f'report could not be encoded: {e}', code='encode') from None
        headers = {'Content-Type': 'application/json', 'User-Agent': UA}
        if signed:
            ts, nonce = str(int(self.clock())), secrets.token_hex(12)
            sig = ed25519.sign(self._seed, _message(ts, nonce, 'POST', path, body))
            headers.update({'X-AVID-Agent': self.agent_id, 'X-AVID-Timestamp': ts, 'X-AVID-Nonce': nonce,
                            'X-AVID-Signature': base64.urlsafe_b64encode(sig).decode()})
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method='POST')
        try:
            with self.opener(req, timeout=self.timeout) as r:
                raw = r.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                raise CloudError(f'cloud redirect refused (HTTP {e.code})', status=e.code, code='redirect') from None
            detail, code = '', None
            try:
                j = loads_strict(e.read(2000))
                if isinstance(j, dict):
                    detail, code = str(j.get('message', ''))[:200], j.get('error')
                    code = code if isinstance(code, str) else None
            except Exception:
                pass
            raise CloudError(f'cloud HTTP {e.code}: {detail}'[:300], status=e.code, code=code,
                             retry_after=_retry_after(getattr(e, 'headers', None))) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as e:
            raise CloudError(f'cloud unreachable: {type(e).__name__}: {str(e)[:150]}') from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CloudError(f'cloud response larger than {MAX_RESPONSE_BYTES} bytes', code='bad_response')
        try:
            return loads_strict(raw)
        except (ValidationError, UnicodeDecodeError) as e:
            raise CloudError(f'cloud response unusable: {str(e)[:150]}', code='bad_response') from None

    def pair(self, code: str, public_key: bytes, version: str, ceilings: dict) -> dict:
        res = self._post('/agent/v1/pair', {'pairing_code': code, 'public_key': base64.b64encode(public_key).decode(),
                                             'version': version, 'local_ceilings': ceilings, 'protocol': PROTOCOL,
                                             'audience': self.base}, signed=False)
        if not isinstance(res, dict) or not isinstance(res.get('agent_id'), str):
            raise CloudError('pairing answer has no agent_id', code='bad_response')
        return res

    def sync(self, report: dict) -> dict:
        payload = dict(report)
        payload.setdefault('protocol', PROTOCOL)
        payload.setdefault('agent_id', self.agent_id)
        payload.setdefault('batch_id', uuid.uuid4().hex)
        payload['audience'] = self.base
        return self._post('/agent/v1/sync', payload, signed=True)


@dataclass
class SyncResult:
    accepted: list
    rejected: dict
    directive: dict | None
    directive_error: str | None = None
    extra: dict = field(default_factory=dict)


def parse_sync_response(resp, *, agent_id, batch_id, sent_keys) -> SyncResult:
    """Validate a v2 sync answer. Raises CloudError(code='bad_ack') when the acknowledgement cannot be
    trusted (then nothing may be deleted). A valid ACK with an invalid directive returns
    directive=None and directive_error (the items are acknowledged, the directive is not applied)."""
    def bad(msg):
        raise CloudError(f'invalid sync acknowledgement: {msg}', code='bad_ack')
    if not isinstance(resp, dict):
        bad('the answer is not an object')
    if resp.get('protocol') != PROTOCOL:
        bad(f'protocol {str(resp.get("protocol"))[:20]!r} (expected {PROTOCOL})')
    if resp.get('agent_id') != agent_id:
        bad('the answer is addressed to another agent')
    if resp.get('batch_id') != batch_id:
        bad('the answer is for another batch')
    ack = resp.get('ack')
    if not isinstance(ack, dict):
        bad('ack missing')
    acc, rej = ack.get('accepted', []), ack.get('rejected', [])
    if not isinstance(acc, list) or not isinstance(rej, list) or len(acc) + len(rej) > MAX_ACK_KEYS:
        bad('ack lists malformed')
    if not all(isinstance(k, str) for k in acc):
        bad('accepted keys must be strings')
    rejected = {}
    for r in rej:
        if not isinstance(r, dict) or not isinstance(r.get('item_key'), str):
            bad('rejected entries must be {"item_key", "reason"}')
        rejected[r['item_key']] = str(r.get('reason', ''))[:300]
    sent = set(sent_keys)
    accepted = list(dict.fromkeys(acc))
    unknown = (set(accepted) | set(rejected)) - sent
    if unknown:
        bad(f'{len(unknown)} acknowledged key(s) were never sent in this batch')
    if set(accepted) & set(rejected):
        bad('a key is both accepted and rejected')
    try:
        directive, derr = validate_directive(resp.get('directive')), None
    except DirectiveError as e:
        directive, derr = None, str(e)
    return SyncResult(accepted, rejected, directive, derr)
