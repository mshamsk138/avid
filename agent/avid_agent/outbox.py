"""Durable, bounded queue of reports waiting for the cloud (AVID-05, AVID-06).

SQLite on the customer's machine (stdlib). Every item has a stable item_key: the cloud acknowledges
items BY KEY and only acknowledged items are deleted (a resend after a lost reply is harmless because
the cloud de-duplicates by the same key). Inserting a key twice is a no-op.

Three classes of items:
  DROPPABLE  snapshots and informational events - may be evicted when the queue is over its cap
  DURABLE    trading decisions (simulated intents) and warnings - evicted only when older than the
             retention period (30 days) and the queue is still over its cap
  CRITICAL   orders, fills, live intents, errors, security and state-change events - NEVER evicted

Eviction removes exactly as many items as needed, oldest first, lowest class first. When the queue is
over its cap and nothing evictable is left, the outbox is under STORAGE PRESSURE: the agent places no
new orders, new low-class items are counted and refused (the count itself is reported), critical items
are still stored up to twice the cap; beyond that put() raises OutboxFull and the caller keeps the
record in the engine log (its read cursor does not advance), so nothing critical is lost. Pressure
clears by itself once the cloud has acknowledged enough items. SQLite / disk errors raise OutboxError;
they are never turned into success.

The log read cursor (file identity + offset, AVID-04) is stored in the same database so that queueing
the items of a log record and advancing the cursor past it happen in ONE transaction.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from avid_core.validation import sanitize

KINDS = ('snapshots', 'intents', 'orders', 'fills', 'events')
DROPPABLE, DURABLE, CRITICAL = 0, 1, 2
CLASS_NAMES = {DROPPABLE: 'droppable', DURABLE: 'durable', CRITICAL: 'critical'}
CRITICAL_EVENT_KINDS = {
    'order_state', 'order_placed', 'order_final', 'adopted_after_error', 'pending_unresolved',
    'pending_resolved_manually', 'order_resolved_manually', 'order_status_unresolved', 'kill_switch_cancel',
    'stopped', 'cancel_failed', 'fills_reconciled', 'fill_aggregate', 'reconciliation', 'reconciliation_incomplete',
    'agent_revoked', 'state_corrupt', 'state_recovered_from_ledger', 'control_invalid', 'policy_rejected_locally',
    'directive_rejected', 'cloud_bad_ack', 'log_gap', 'storage_pressure', 'outbox_dropped',
    'client_order_id_unavailable', 'client_order_id_mismatch', 'clock_backwards', 'clock_mismatch', 'ceilings_invalid',
    'order_submit_unconfirmed', 'pending_cancel_by_client_order_id', 'order_state_anomaly', 'pending_mismatch',
    'cloud_items_rejected', 'config_invalid', 'markets_invalid', 'valuation_incomplete', 'other_avid_instance'}
ESSENTIAL_KEYS = ('item_key', 'key', 'at', 'kind', 'level', 'exchange_order_id', 'intent_key', 'client_order_id', 'asset',
                  'side', 'amount', 'price', 'status', 'state', 'filled_amount', 'unfilled_amount', 'avg_price', 'fee',
                  'fee_asset', 'accounting', 'trade_id', 'mode', 'notional_usdt', 'aggregate')
MAX_ITEM_BYTES = 16_000
CRITICAL_MAX_ITEM_BYTES = 256_000
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_ITEMS = 200_000
HARD_FACTOR = 2
DURABLE_RETENTION_S = 30 * 86400


class OutboxError(RuntimeError):
    """The queue could not be read or written (disk full, I/O error, corrupt database)."""


class OutboxFull(OutboxError):
    """A critical item does not fit even in the reserve above the cap."""


def classify(kind: str, item: dict) -> int:
    if kind in ('orders', 'fills'):
        return CRITICAL
    if kind == 'intents':
        return CRITICAL if item.get('mode') == 'live' else DURABLE
    if kind == 'snapshots':
        return DROPPABLE
    if item.get('level') == 'error' or item.get('kind') in CRITICAL_EVENT_KINDS:
        return CRITICAL
    if item.get('level') == 'warning':
        return DURABLE
    return DROPPABLE


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'), default=str, allow_nan=False)


def derive_key(kind: str, item: dict) -> str:
    if kind == 'fills' and item.get('trade_id'):
        return f'fill:{item["trade_id"]}'
    if item.get('key'):
        return f'{kind}:{item["key"]}'
    return f'{kind}:' + hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:40]


class Outbox:
    def __init__(self, path: Path, *, max_bytes=DEFAULT_MAX_BYTES, max_items=DEFAULT_MAX_ITEMS, clock=time.time):
        self.path = Path(path)
        self.max_bytes, self.max_items = int(max_bytes), int(max_items)
        self.clock = clock
        try:
            self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self._schema()
        except sqlite3.Error as e:
            raise OutboxError(f'outbox database unusable: {e}') from None
        self._depth = 0

    # ------------------------------------------------------------ schema
    def _schema(self):
        db = self.db
        db.execute('CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, '
                   'prio INTEGER NOT NULL DEFAULT 0, level TEXT NOT NULL DEFAULT "", size INTEGER NOT NULL, '
                   'payload TEXT NOT NULL)')
        cols = {r[1] for r in db.execute('PRAGMA table_info(items)')}
        if 'cls' not in cols:
            db.execute('ALTER TABLE items ADD COLUMN cls INTEGER NOT NULL DEFAULT 1')
        if 'item_key' not in cols:
            db.execute('ALTER TABLE items ADD COLUMN item_key TEXT')
        if 'created_at' not in cols:
            db.execute('ALTER TABLE items ADD COLUMN created_at REAL NOT NULL DEFAULT 0')
        if not cols or 'cls' not in cols:                 # v1.2 queue: classify what is there
            for rid, kind, payload in db.execute('SELECT id, kind, payload FROM items').fetchall():
                try:
                    item = json.loads(payload)
                except ValueError:
                    item = {}
                db.execute('UPDATE items SET cls=?, created_at=? WHERE id=?',
                           (classify(kind, item if isinstance(item, dict) else {}), self.clock(), rid))
        db.execute("UPDATE items SET item_key='legacy-' || id WHERE item_key IS NULL")
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS items_key ON items (item_key)')
        db.execute('CREATE INDEX IF NOT EXISTS items_cls ON items (cls, id)')
        db.execute('CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS rejected (id INTEGER PRIMARY KEY AUTOINCREMENT, item_key TEXT, kind TEXT, '
                   'reason TEXT, payload TEXT, at REAL)')

    # ------------------------------------------------------------ transactions
    @contextlib.contextmanager
    def tx(self):
        """One atomic unit; nested calls become savepoints."""
        name = f'sp{self._depth}'
        try:
            self.db.execute('BEGIN IMMEDIATE' if self._depth == 0 else f'SAVEPOINT {name}')
        except sqlite3.Error as e:
            raise OutboxError(f'outbox: cannot start a transaction: {e}') from None
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            try:
                self.db.execute('ROLLBACK' if self._depth == 0 else f'ROLLBACK TO {name}')
                if self._depth:
                    self.db.execute(f'RELEASE {name}')
            except sqlite3.Error:
                pass
            raise
        self._depth -= 1
        try:
            self.db.execute('COMMIT' if self._depth == 0 else f'RELEASE {name}')
        except sqlite3.Error as e:
            raise OutboxError(f'outbox: commit failed: {e}') from None

    def _x(self, sql, args=()):
        try:
            return self.db.execute(sql, args)
        except sqlite3.Error as e:
            raise OutboxError(f'outbox: {e}') from None

    # ------------------------------------------------------------ meta
    def _meta(self, k, default=None):
        row = self._x('SELECT v FROM meta WHERE k=?', (k,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def _set_meta(self, k, v):
        self._x('INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, json.dumps(v)))

    def get_cursor(self):
        c = self._meta('log_cursor')
        return c if isinstance(c, dict) else None

    def set_cursor(self, cursor: dict):
        self._set_meta('log_cursor', cursor)

    @property
    def pressure(self) -> bool:
        return bool(self._meta('pressure', False))

    @property
    def dropped(self) -> int:
        d = self._meta('dropped', {}) or {}
        return int(sum(d.values())) if isinstance(d, dict) else 0

    @dropped.setter
    def dropped(self, value):                    # v1.2 compatibility: `outbox.dropped = 0` resets the counter
        if not value:
            self._set_meta('dropped', {})

    def take_dropped(self) -> dict:
        d = self._meta('dropped', {}) or {}
        if d:
            self._set_meta('dropped', {})
        return d if isinstance(d, dict) else {}

    def _count_drop(self, cls, n=1):
        if n <= 0:
            return
        d = self._meta('dropped', {}) or {}
        name = CLASS_NAMES[cls]
        d[name] = int(d.get(name, 0)) + n
        self._set_meta('dropped', d)

    # ------------------------------------------------------------ put / evict
    def _shrink(self, kind, item, cls, text):
        if len(text.encode()) <= MAX_ITEM_BYTES:
            return item, text
        if kind == 'events':
            item = {**item, 'message': str(item.get('message', ''))[:300], 'data': {}, 'truncated': True}
            text = _dumps(item)
            if len(text.encode()) <= MAX_ITEM_BYTES:
                return item, text
        if cls < CRITICAL:
            return None, None
        if len(text.encode()) <= CRITICAL_MAX_ITEM_BYTES:
            return item, text
        small = {k: item[k] for k in ESSENTIAL_KEYS if k in item}
        small['truncated'] = True
        text = _dumps(small)
        while len(text.encode()) > CRITICAL_MAX_ITEM_BYTES:          # pathological values: cut strings
            small = {k: (v[:200] if isinstance(v, str) else v) for k, v in small.items()}
            text = _dumps(small)
            if len(text.encode()) > CRITICAL_MAX_ITEM_BYTES:
                small = {'item_key': small.get('item_key'), 'truncated': True}
                text = _dumps(small)
        return small, text

    def put(self, kind: str, item: dict, item_key: str | None = None) -> str:
        """'stored' | 'duplicate' | 'dropped'. Raises OutboxFull / OutboxError (never silent)."""
        if kind not in KINDS:
            raise ValueError(f'unknown outbox kind {kind!r}')
        item = sanitize(dict(item))
        key = str(item_key or item.get('item_key') or derive_key(kind, item))[:200]
        item['item_key'] = key
        cls = classify(kind, item)
        item, text = self._shrink(kind, item, cls, _dumps(item))
        full = None
        with self.tx():
            if item is None:                                  # too big and not critical: counted, reported
                self._count_drop(cls)
                return 'dropped'
            if self._x('SELECT 1 FROM items WHERE item_key=?', (key,)).fetchone():
                return 'duplicate'
            size = len(text.encode())
            n, total = self._x('SELECT COUNT(*), COALESCE(SUM(size), 0) FROM items').fetchone()
            if cls < CRITICAL and self._meta('pressure', False) and (n + 1 > self.max_items or total + size > self.max_bytes):
                self._evict_for(1, size)
                n2, total2 = self._x('SELECT COUNT(*), COALESCE(SUM(size), 0) FROM items').fetchone()
                if n2 + 1 > self.max_items or total2 + size > self.max_bytes:
                    self._count_drop(cls)
                    return 'dropped'
            if cls == CRITICAL and (n + 1 > self.max_items * HARD_FACTOR or total + size > self.max_bytes * HARD_FACTOR):
                full = f'outbox full ({n} items, {total} bytes): critical item kept in the engine log'
            else:
                self._x('INSERT INTO items (kind, prio, level, size, payload, cls, item_key, created_at) '
                        'VALUES (?,?,?,?,?,?,?,?)', (kind, cls, str(item.get('level', '')), size, text, cls, key,
                                                     self.clock()))
                self._enforce_cap()
        if full:
            with self.tx():
                self._set_meta('pressure', True)
            raise OutboxFull(full)
        return 'stored'

    def _evict_for(self, need_items, need_bytes) -> tuple[int, int]:
        """Delete just enough DROPPABLE (then DURABLE past retention) items, oldest first."""
        removed = freed = 0
        for cls, older in ((DROPPABLE, None), (DURABLE, self.clock() - DURABLE_RETENTION_S)):
            if removed >= need_items and freed >= need_bytes:
                break
            q = 'SELECT id, size FROM items WHERE cls=?' + (' AND created_at < ?' if older else '') + ' ORDER BY id'
            ids = []
            for rid, size in self._x(q, (cls, older) if older else (cls,)).fetchall():
                if removed >= need_items and freed >= need_bytes:
                    break
                ids.append(rid)
                removed += 1
                freed += size
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                self._x(f'DELETE FROM items WHERE id IN ({",".join("?" * len(chunk))})', chunk)
            self._count_drop(cls, len(ids))
        return removed, freed

    def _enforce_cap(self):
        n, size = self._x('SELECT COUNT(*), COALESCE(SUM(size), 0) FROM items').fetchone()
        if n > self.max_items or size > self.max_bytes:
            self._evict_for(max(0, n - self.max_items), max(0, size - self.max_bytes))
            n, size = self._x('SELECT COUNT(*), COALESCE(SUM(size), 0) FROM items').fetchone()
        over = n > self.max_items or size > self.max_bytes
        if over:
            self._set_meta('pressure', True)
        elif self._meta('pressure', False) and n <= 0.9 * self.max_items and size <= 0.9 * self.max_bytes:
            self._set_meta('pressure', False)

    # ------------------------------------------------------------ send / ack
    def batch(self, max_items=400, max_bytes=1_500_000) -> tuple[dict, list]:
        """Oldest items first, within the count and byte budget: ({kind: [items]}, [item_keys])."""
        out = {k: [] for k in KINDS}
        keys, used = [], 0
        for kind, size, payload, key in self._x('SELECT kind, size, payload, item_key FROM items ORDER BY id LIMIT ?',
                                                (max_items,)).fetchall():
            if keys and used + size > max_bytes:
                break
            try:
                item = json.loads(payload)
            except ValueError:
                item = {'item_key': key, 'unreadable': True}
            item['item_key'] = key
            out[kind].append(item)
            keys.append(key)
            used += size
        return out, keys

    def ack(self, keys):
        """Delete the items the cloud ACKNOWLEDGED (by item_key)."""
        keys = [str(k) for k in keys]
        with self.tx():
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                self._x(f'DELETE FROM items WHERE item_key IN ({",".join("?" * len(chunk))})', chunk)
            self._enforce_cap()

    def reject(self, rejected: dict):
        """Items the cloud refused for good: moved to the local `rejected` table (kept for audit)."""
        if not rejected:
            return
        with self.tx():
            for key, reason in rejected.items():
                row = self._x('SELECT kind, payload FROM items WHERE item_key=?', (str(key),)).fetchone()
                if row is None:
                    continue
                self._x('INSERT INTO rejected (item_key, kind, reason, payload, at) VALUES (?,?,?,?,?)',
                        (str(key), row[0], str(reason)[:300], row[1], self.clock()))
                self._x('DELETE FROM items WHERE item_key=?', (str(key),))
            self._enforce_cap()

    def counts(self) -> dict:
        rows = dict(self._x('SELECT kind, COUNT(*) FROM items GROUP BY kind').fetchall())
        return {k: rows.get(k, 0) for k in KINDS}

    def stats(self) -> dict:
        n, size = self._x('SELECT COUNT(*), COALESCE(SUM(size), 0) FROM items').fetchone()
        by = dict(self._x('SELECT cls, COUNT(*) FROM items GROUP BY cls').fetchall())
        return {'items': n, 'bytes': size, 'pressure': self.pressure,
                'by_class': {CLASS_NAMES[c]: by.get(c, 0) for c in CLASS_NAMES},
                'rejected': self._x('SELECT COUNT(*) FROM rejected').fetchone()[0]}

    def __len__(self):
        return self._x('SELECT COUNT(*) FROM items').fetchone()[0]

    def close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass
