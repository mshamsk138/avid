"""Agent <-> cloud protocol v2: signed transport over a real local HTTP server, ACK validation (AVID-06)."""
import base64
import hashlib
import http.server
import json
import threading

import pytest

from avid_agent.cloud import CloudClient, CloudError, parse_sync_response
from avid_core import ed25519

SEED = bytes(range(32))


def ok(report, **over):
    keys = [it['item_key'] for k in ('snapshots', 'intents', 'orders', 'fills', 'events') for it in report.get(k) or []]
    r = {'protocol': 2, 'agent_id': report['agent_id'], 'batch_id': report['batch_id'],
         'ack': {'accepted': keys, 'rejected': []}, 'directive': {'global_halt': False, 'portfolio': None}}
    r.update(over)
    return r


class Server:
    def __init__(self, handler):
        outer = self
        self.requests = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                outer.requests.append((self.path, dict(self.headers), body))
                status, payload, headers = handler(self.path, dict(self.headers), body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        self.httpd = http.server.HTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.httpd.server_port}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    made = []

    def make(handler):
        s = Server(handler)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def verify(headers, path, body):
    headers = {k.lower(): v for k, v in headers.items()}
    pub = ed25519.public_key(SEED)
    msg = '\n'.join([headers['x-avid-timestamp'], headers['x-avid-nonce'], 'POST', path,
                     hashlib.sha256(body).hexdigest()]).encode()
    assert headers['x-avid-agent'] == 'agent-1'
    sig = base64.urlsafe_b64decode(headers['x-avid-signature'])
    # Ed25519 is deterministic: the RFC 8032 reference implementation must give the same signature
    assert sig == ed25519._pure_sign(SEED, msg)
    if ed25519.backend() == 'cryptography':                           # and the library must accept it
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)      # raises if invalid


def test_signed_sync_round_trip(server):
    s = server(lambda path, h, body: (200, ok(json.loads(body)), None))
    c = CloudClient(s.url, 'agent-1', SEED)
    resp = c.sync({'events': [{'item_key': 'k1', 'message': 'hi'}], 'batch_id': 'b1'})
    path, headers, body = s.requests[0]
    verify(headers, path, body)
    sent = json.loads(body)
    assert sent['protocol'] == 2 and sent['agent_id'] == 'agent-1' and sent['audience'] == s.url
    res = parse_sync_response(resp, agent_id='agent-1', batch_id='b1', sent_keys=['k1'])
    assert res.accepted == ['k1'] and res.directive == {'global_halt': False, 'portfolio': None}


def test_request_body_never_contains_nan(server):
    s = server(lambda path, h, body: (200, ok(json.loads(body)), None))
    CloudClient(s.url, 'agent-1', SEED).sync({'batch_id': 'b', 'snapshots': [{'item_key': 'x', 'v': float('nan')}]})
    assert b'NaN' not in s.requests[0][2]


@pytest.mark.parametrize('status,payload,code', [
    (200, b'{"a": NaN}', 'bad_response'), (200, b'not json', 'bad_response'),
    (401, {'error': 'agent_revoked', 'message': 'revoked'}, 'agent_revoked'),
])
def test_bad_answers_are_cloud_errors(server, status, payload, code):
    s = server(lambda path, h, body: (status, payload, None))
    with pytest.raises(CloudError) as ei:
        CloudClient(s.url, 'agent-1', SEED).sync({'batch_id': 'b'})
    assert ei.value.code == code
    if code == 'agent_revoked':
        assert ei.value.revoked


def test_retry_after_and_redirects(server):
    s = server(lambda path, h, body: (429, {'error': 'rate'}, {'Retry-After': '120'}))
    with pytest.raises(CloudError) as ei:
        CloudClient(s.url, 'agent-1', SEED).sync({'batch_id': 'b'})
    assert ei.value.status == 429 and ei.value.retry_after == 120
    s2 = server(lambda path, h, body: (302, b'', {'Location': 'https://evil.example/'}))
    with pytest.raises(CloudError) as ei:
        CloudClient(s2.url, 'agent-1', SEED).sync({'batch_id': 'b'})
    assert ei.value.code == 'redirect'


def test_oversized_answer(server):
    big = b'{"x": "' + b'a' * 4_100_000 + b'"}'
    s = server(lambda path, h, body: (200, big, None))
    with pytest.raises(CloudError):
        CloudClient(s.url, 'agent-1', SEED).sync({'batch_id': 'b'})


GOOD = {'protocol': 2, 'agent_id': 'a1', 'batch_id': 'b1', 'ack': {'accepted': ['k1'], 'rejected': []},
        'directive': {'global_halt': False, 'portfolio': None}}


@pytest.mark.parametrize('mutate', [
    lambda r: r.pop('protocol'), lambda r: r.update(protocol=1), lambda r: r.update(agent_id='other'),
    lambda r: r.update(batch_id='other'), lambda r: r.pop('ack'), lambda r: r['ack'].update(accepted='k1'),
    lambda r: r['ack'].update(accepted=['k1', 'k-not-sent']), lambda r: r['ack'].update(rejected=[{'item_key': 'k1'}]),
    lambda r: r['ack'].update(accepted=[1]), lambda r: r['ack'].update(rejected=['k1']),
])
def test_untrustworthy_acks_are_refused(mutate):
    import copy
    r = copy.deepcopy(GOOD)
    mutate(r)
    with pytest.raises(CloudError) as ei:
        parse_sync_response(r, agent_id='a1', batch_id='b1', sent_keys=['k1', 'k2'])
    assert ei.value.code == 'bad_ack'
    with pytest.raises(CloudError):
        parse_sync_response({'error': 'unexpected result'}, agent_id='a1', batch_id='b1', sent_keys=['k1'])


def test_partial_ack_and_rejections():
    r = {**GOOD, 'ack': {'accepted': ['k1'], 'rejected': [{'item_key': 'k2', 'reason': 'schema'}]}}
    res = parse_sync_response(r, agent_id='a1', batch_id='b1', sent_keys=['k1', 'k2', 'k3'])
    assert res.accepted == ['k1'] and res.rejected == {'k2': 'schema'}


@pytest.mark.parametrize('directive', [
    None, [], {'global_halt': 'yes'}, {'global_halt': False, 'portfolio': []},
    {'global_halt': False, 'portfolio': {'mode': 'LIVE!'}},
    {'global_halt': False, 'portfolio': {'mode': 'live', 'policy': {'version_id': 'v', 'weights': {'doge': 1}}}},
    {'global_halt': False, 'portfolio': {'mode': 'live', 'policy': {'version_id': 'v', 'weights': {'btc': 1},
                                                                    'band_rel': 0.25, 'max_order_usdt': float('nan'),
                                                                    'max_orders_per_day': 6, 'max_daily_usdt': 100}}},
    {'global_halt': False, 'portfolio': {'mode': 'live', 'kill_switch': 'true', 'policy': None}},
])
def test_invalid_directive_is_reported_but_ack_still_counts(directive):
    r = {**GOOD, 'directive': directive}
    res = parse_sync_response(r, agent_id='a1', batch_id='b1', sent_keys=['k1'])
    assert res.accepted == ['k1'] and res.directive is None and res.directive_error


def test_agent_id_header_injection_is_refused():
    from avid_agent.config import ConfigError
    with pytest.raises(ConfigError):
        CloudClient('https://cloud.example', 'a\r\nX: 1', SEED)
