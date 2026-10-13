# sync/sync_status.py
#
# Task 5 — Automatic Synchronization and Data Freshness
#
# Lightweight, thread-safe status tracker for the background
# sync scheduler.  Records:
#   - Last successful sync time
#   - Last detected change
#   - Documents added / updated / deleted per cycle
#   - Errors
#   - Freshness latency (time from change detection to index update)
#
# This is a single-process in-memory store — no Redis, no DB.
# It is intentionally simple for the hackathon demo.

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_float() -> float:
    return datetime.now(timezone.utc).timestamp()


@dataclass
class SyncCycleResult:
    """Summary of a single sync cycle."""
    timestamp: str = ""
    added: int = 0
    updated: int = 0
    deleted: int = 0
    skipped_unchanged: int = 0
    skipped_revoked: int = 0
    errors: List[str] = field(default_factory=list)
    latency_ms: Optional[float] = None
    sources_checked: int = 0
    changes_detected: bool = False


class SyncStatusTracker:
    """
    Thread-safe tracker for synchronization status.

    All methods are safe to call from the scheduler thread and
    from FastAPI request handlers simultaneously.
    """

    def __init__(self, max_history: int = 50):
        self._lock = threading.Lock()
        self._max_history = max_history

        self._last_successful_sync: Optional[str] = None
        self._last_successful_ts: Optional[float] = None
        self._last_change_detected: Optional[str] = None
        self._last_error: Optional[str] = None
        self._scheduler_running: bool = False
        self._total_syncs: int = 0
        self._total_errors: int = 0
        self._history: List[SyncCycleResult] = []

        # Per-source last-seen state
        self._source_hashes: Dict[str, str] = {}

    # ── Scheduler lifecycle ──────────────────────────────────

    def mark_scheduler_started(self):
        with self._lock:
            self._scheduler_running = True

    def mark_scheduler_stopped(self):
        with self._lock:
            self._scheduler_running = False

    @property
    def scheduler_running(self) -> bool:
        with self._lock:
            return self._scheduler_running

    # ── Cycle recording ──────────────────────────────────────

    def record_cycle(self, result: SyncCycleResult):
        with self._lock:
            self._total_syncs += 1
            self._history.append(result)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

            if result.errors:
                self._total_errors += len(result.errors)
                self._last_error = result.errors[-1]
            else:
                # Clear last error on a clean cycle
                self._last_error = None

            if not result.errors:
                self._last_successful_sync = result.timestamp
                self._last_successful_ts = _now_float()

            if result.changes_detected:
                self._last_change_detected = result.timestamp

    # ── Source hash tracking ─────────────────────────────────

    def get_source_hash(self, source: str) -> Optional[str]:
        with self._lock:
            return self._source_hashes.get(source)

    def set_source_hash(self, source: str, h: str):
        with self._lock:
            self._source_hashes[source] = h

    # ── Freshness ─────────────────────────────────────────────

    def freshness_latency_seconds(self) -> Optional[float]:
        """
        Returns seconds since last successful sync, or None
        if no sync has occurred yet.
        """
        with self._lock:
            if self._last_successful_ts is None:
                return None
            return _now_float() - self._last_successful_ts

    # ── Snapshot for API ──────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "scheduler_running": self._scheduler_running,
                "last_successful_sync": self._last_successful_sync,
                "last_change_detected": self._last_change_detected,
                "last_error": self._last_error,
                "total_syncs": self._total_syncs,
                "total_errors": self._total_errors,
                "freshness_latency_seconds": (
                    _now_float() - self._last_successful_ts
                    if self._last_successful_ts is not None
                    else None
                ),
                "source_hashes": dict(self._source_hashes),
                "recent_cycles": [
                    {
                        "timestamp": c.timestamp,
                        "added": c.added,
                        "updated": c.updated,
                        "deleted": c.deleted,
                        "skipped_unchanged": c.skipped_unchanged,
                        "skipped_revoked": c.skipped_revoked,
                        "errors": c.errors,
                        "latency_ms": c.latency_ms,
                        "sources_checked": c.sources_checked,
                        "changes_detected": c.changes_detected,
                    }
                    for c in self._history[-10:]
                ],
            }

    def reset(self):
        """Clear all state — used in tests."""
        with self._lock:
            self._last_successful_sync = None
            self._last_successful_ts = None
            self._last_change_detected = None
            self._last_error = None
            self._scheduler_running = False
            self._total_syncs = 0
            self._total_errors = 0
            self._history.clear()
            self._source_hashes.clear()


# Singleton
sync_status_tracker = SyncStatusTracker()
