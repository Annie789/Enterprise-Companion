# audit/audit_query.py

from datetime import datetime
from typing import Optional
from sqlalchemy import text
from audit.audit_logger import Session, engine, AuditEntry
import hashlib, json


def query_audit_log(
    user_id:     Optional[str] = None,
    event_type:  Optional[str] = None,
    outcome:     Optional[str] = None,
    source:      Optional[str] = None,
    resource_id: Optional[str] = None,
    query_id:    Optional[str] = None,
    since:       Optional[datetime] = None,
    until:       Optional[datetime] = None,
    limit:       int = 100,
) -> list[dict]:
    """Query the audit log with filters.

    Filters:
        user_id     — authenticated user
        event_type  — QUERY, PERMISSION_CHANGE, SYNC, REVOKE
        outcome     — ALLOWED, DENIED, ERROR, SUCCESS
        source      — confluence, jira, slack, google_drive
        resource_id — document/node ID
        query_id    — groups entries for one user query
        since       — start of date range (inclusive)
        until       — end of date range (inclusive)
    """

    with Session(engine) as session:
        q = session.query(AuditEntry)
        if user_id:     q = q.filter(AuditEntry.user_id     == user_id)
        if event_type:  q = q.filter(AuditEntry.event_type  == event_type)
        if outcome:     q = q.filter(AuditEntry.outcome      == outcome)
        if source:      q = q.filter(AuditEntry.source       == source)
        if resource_id: q = q.filter(AuditEntry.resource_id  == resource_id)
        if query_id:    q = q.filter(AuditEntry.query_id     == query_id)
        if since:       q = q.filter(AuditEntry.timestamp    >= since)
        if until:       q = q.filter(AuditEntry.timestamp    <= until)

        rows = q.order_by(AuditEntry.timestamp.desc()).limit(limit).all()

    return [
        {
            "id":          r.id,
            "query_id":    r.query_id,
            "timestamp":   r.timestamp.isoformat() if r.timestamp else None,
            "event_type":  r.event_type,
            "user_id":     r.user_id,
            "user_email":  r.user_email,
            "source":      r.source,
            "resource_id": r.resource_id,
            "query":       r.query,
            "outcome":     r.outcome,
            "detail":      json.loads(r.detail or "{}"),
            "entry_hash":  r.entry_hash,
            "prev_hash":   r.prev_hash,
        }
        for r in rows
    ]


def verify_chain_integrity() -> dict:
    """
    Walk every entry in order and verify the hash chain is unbroken.
    Returns { intact: bool, broken_at: entry_id or None }

    The hash now includes query_id so that tampering with the
    query grouping is also detected.
    """
    with Session(engine) as session:
        rows = session.query(AuditEntry).order_by(AuditEntry.timestamp.asc()).all()

    prev_hash = "GENESIS"
    for row in rows:
        if row.prev_hash != prev_hash:
            return {"intact": False, "broken_at": row.id}

        expected = hashlib.sha256(json.dumps({
            "query_id":    row.query_id,
            "event_type":  row.event_type,
            "user_id":     row.user_id,
            "user_email":  row.user_email,
            "source":      row.source,
            "resource_id": row.resource_id,
            "query":       row.query,
            "outcome":     row.outcome,
            "detail":      json.loads(row.detail or "{}"),
            "timestamp":   row.timestamp.isoformat() if row.timestamp else None,
            "prev_hash":   row.prev_hash,
        }, sort_keys=True, default=str).encode()).hexdigest()

        if row.entry_hash != expected:
            return {"intact": False, "broken_at": row.id}

        prev_hash = row.entry_hash

    return {"intact": True, "broken_at": None}
