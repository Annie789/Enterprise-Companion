# audit/audit_logger.py

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, String, Text, DateTime,
    create_engine, text, inspect
)
from sqlalchemy.orm import DeclarativeBase, Session


# ── Database setup ─────────────────────────────────────────────────────
engine = create_engine(
    "sqlite:///data/audit.db",
    connect_args={"check_same_thread": False},
)


class Base(DeclarativeBase):
    pass


class AuditEntry(Base):
    __tablename__ = "audit_log"

    id            = Column(String,   primary_key=True, default=lambda: str(uuid.uuid4()))
    query_id      = Column(String,   nullable=True)    # groups entries for the same user query
    timestamp     = Column(DateTime, nullable=False)
    event_type    = Column(String,   nullable=False)   # QUERY, ACCESS_DENIED, SYNC, REVOKE
    user_id       = Column(String,   nullable=False)
    user_email    = Column(String,   nullable=True)
    source        = Column(String,   nullable=True)    # confluence, jira, slack, google_drive
    resource_id   = Column(String,   nullable=True)    # document/node ID accessed
    query         = Column(Text,     nullable=True)    # user query text
    outcome       = Column(String,   nullable=False)   # ALLOWED, DENIED, ERROR, SUCCESS
    detail        = Column(Text,     nullable=True)    # JSON blob for extra info
    prev_hash     = Column(String,   nullable=False)   # hash of previous entry
    entry_hash    = Column(String,   nullable=False)   # hash of THIS entry (tamper detection)


Base.metadata.create_all(engine)

# ── Lightweight schema migration ───────────────────────────────────────
# Add the query_id column if the table was created by a prior version.
def _ensure_column():
    insp = inspect(engine)
    if insp.has_table("audit_log"):
        cols = {c["name"] for c in insp.get_columns("audit_log")}
        if "query_id" not in cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE audit_log ADD COLUMN query_id TEXT"))
                conn.commit()

_ensure_column()


# ── Hash chain ─────────────────────────────────────────────────────────
def _get_last_hash() -> str:
    with Session(engine) as session:
        row = session.execute(
            text("SELECT entry_hash FROM audit_log ORDER BY timestamp DESC LIMIT 1")
        ).fetchone()
        return row[0] if row else "GENESIS"


def _compute_hash(entry_data: dict) -> str:
    canonical = json.dumps(entry_data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ── Public logger ──────────────────────────────────────────────────────
class AuditLogger:

    def log(
        self,
        event_type: str,
        user_id: str,
        outcome: str,
        user_email: str = "",
        source: str = "",
        resource_id: str = "",
        query: str = "",
        query_id: str = "",
        detail: Optional[dict] = None,
    ) -> str:
        """
        Write a tamper-evident audit entry.

        query_id groups entries belonging to the same user query
        (e.g. the authorization-decision entry and the final-answer
        entry).

        The detail dict may carry:
          - retrieved_doc_ids   list[str]
          - auth_decisions      list[{source_id, source, decision}]
          - final_answer        str
          - citations           list[dict]
          - query_outcome       str  (success, denied, error)

        All fields are included in the hash chain so tampering
        with any of them is detected by verify_chain_integrity().
        """
        prev_hash = _get_last_hash()
        timestamp = datetime.now(timezone.utc)

        entry_data = {
            "query_id":    query_id,
            "event_type":  event_type,
            "user_id":     user_id,
            "user_email":  user_email,
            "source":      source,
            "resource_id": resource_id,
            "query":       query,
            "outcome":     outcome,
            "detail":      detail or {},
            "timestamp":   timestamp.isoformat(),
            "prev_hash":   prev_hash,
        }

        entry_hash = _compute_hash(entry_data)
        entry_id   = str(uuid.uuid4())

        with Session(engine) as session:
            session.add(AuditEntry(
                id          = entry_id,
                query_id    = query_id,
                timestamp   = timestamp,
                event_type  = event_type,
                user_id     = user_id,
                user_email  = user_email,
                source      = source,
                resource_id = resource_id,
                query       = query,
                outcome     = outcome,
                detail      = json.dumps(detail or {}),
                prev_hash   = prev_hash,
                entry_hash  = entry_hash,
            ))
            session.commit()

        return entry_id


# ── Singleton ──────────────────────────────────────────────────────────
audit_logger = AuditLogger()
