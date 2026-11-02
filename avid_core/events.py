"""Severity of engine and agent events: one table for the whole project.

Every log record carries `event`, `eid` (unique event id) and `level` ('info' | 'warning' | 'error').
A caller may set `level` explicitly; otherwise it comes from this table. Filtering a log by severity
therefore never needs to know event names (report section 8.4).
"""
from __future__ import annotations

ERROR_EVENTS = frozenset({
    'error', 'state_corrupt', 'control_invalid', 'cancel_failed', 'config_conflict', 'agent_revoked',
    'pending_unresolved', 'order_status_unresolved', 'fill_aggregate', 'client_order_id_unavailable',
    'order_state_anomaly', 'pending_mismatch', 'client_order_id_mismatch', 'reconciliation_incomplete',
    'state_recovered_from_ledger', 'clock_backwards', 'clock_mismatch', 'log_gap', 'storage_pressure',
    'cloud_bad_ack', 'directive_rejected', 'ceilings_invalid', 'markets_invalid', 'config_invalid',
    'plan_invalid', 'policy_rejected_locally', 'emergency_cancel'})
WARNING_EVENTS = frozenset({
    'cap_reached', 'stopped', 'pending_not_open', 'kill_switch_cancel', 'cancel_mode_dry', 'order_status_unknown',
    'trades_unavailable', 'hold', 'cloud_unreachable', 'outbox_dropped', 'order_submit_unconfirmed',
    'valuation_incomplete', 'engine_busy', 'other_avid_instance', 'cloud_items_rejected', 'trade_record_invalid',
    'order_record_inconsistent', 'pending_resolved_manually', 'order_resolved_manually'})
LEVELS = ('info', 'warning', 'error')


def level_of(event: str, explicit: str | None = None) -> str:
    """The severity of one record: the explicit level when it is valid, else the table."""
    if explicit in LEVELS:
        return explicit
    if event in ERROR_EVENTS:
        return 'error'
    if event in WARNING_EVENTS:
        return 'warning'
    return 'info'
