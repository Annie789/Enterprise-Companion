# sync/auto_sync_scheduler.py
#
# Task 5 — Automatic Synchronization and Data Freshness
#
# Lightweight background synchronization scheduler for a
# single-process FastAPI hackathon application.
#
# DESIGN — Mock-Source Freshness Simulation
#   This is NOT production real-time synchronization.  It is a
#   polling-based freshness simulation that checks the four mock
#   JSON data files (Confluence, Jira, Slack, Google Drive) every
#   N seconds (default 30, configurable via MOCK_SYNC_INTERVAL_SECONDS)
#   and reconciles the user's vector index accordingly.
#
# CHANGE DETECTION
#   A SHA-256 hash is computed over the serialized JSON content of
#   each source file.  If the hash is unchanged since the last
#   cycle, the source is skipped entirely — no reindexing.
#
# REUSE OF EXISTING INFRASTRUCTURE
#   - Stable document IDs (indexing.doc_identity.stable_doc_id)
#   - Content hashes (indexing.doc_identity.content_hash)
#   - Indexer add_nodes_to_index / remove_document_from_index
#   - Task 4 ACL reconciliation (permissions.permission_reconciliation)
#   - Revocation registry (permissions.mock_acl_store.is_revoked)
#   - Per-user index locks (indexing.index_lock_manager)
#
# LOCK SAFETY
#   The scheduler never acquires a user's index lock while holding
#   another user's lock.  Each user is processed sequentially within
#   a cycle, and the per-user RLock is reentrant so nested calls
#   (add_nodes_to_index → create_index) do not deadlock.
#
# ACL CONSISTENCY
#   The mock JSON files and the ACL store are not necessarily the
#   same source of truth.  The scheduler resolves consistency as
#   follows:
#     1. Revocation registry takes precedence — a revoked document
#        is NEVER re-indexed, even if the mock JSON still lists it
#        with the user's access intact.
#     2. For non-revoked documents, the mock JSON ACL metadata is
#        the source of truth for content changes, and the
#        reconciliation service (Task 4) is the source of truth
#        for permission changes.
#     3. The scheduler does NOT overwrite a revocation record with
#        stale JSON data.  Only an explicit GRANT action (processed
#        through reconciliation_service.reconcile) can clear a
#        revocation.

import json
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from sync.sync_status import (
    sync_status_tracker,
    SyncCycleResult,
)


# ── Mock data source definitions ──────────────────────────
#
# Paths are resolved from environment variables (set by tests
# to point at temporary copies) with fallback to the default
# data/mock directory.

def _mock_file_path(source: str) -> Path:
    """Resolve the mock JSON path for a source from env or default."""
    env_map = {
        "confluence":   "CONFLUENCE_MOCK_FILE",
        "jira":          "JIRA_MOCK_FILE",
        "slack":         "SLACK_MOCK_FILE",
        "google_drive":  "GOOGLE_DRIVE_MOCK_FILE",
    }
    env_key = env_map.get(source, "")
    if env_key and os.environ.get(env_key):
        return Path(os.environ[env_key])
    return _MOCK_DIR / f"{source}_mock.json"


_MOCK_DIR = Path("data/mock")

# Lazily resolved at access time so tests can override via env vars
def _get_mock_sources():
    return {
        "confluence":   _mock_file_path("confluence"),
        "jira":         _mock_file_path("jira"),
        "slack":        _mock_file_path("slack"),
        "google_drive": _mock_file_path("google_drive"),
    }


# ── Source → loader mapping for building Documents ─────────

_SOURCE_LOADERS = {
    "confluence":   "ingestion.loader.confluence.load_mock_confluence",
    "jira":         "ingestion.loader.jira.load_mock_jira",
    "slack":        "ingestion.loader.slack.load_mock_slack",
    "google_drive": "ingestion.loader.drive.load_mock_google_drive",
}


def _get_sync_interval() -> float:
    """Read the configurable sync interval from env."""
    try:
        return float(os.environ.get("MOCK_SYNC_INTERVAL_SECONDS", "30"))
    except ValueError:
        return 30.0


# ── Hash computation ───────────────────────────────────────

def _compute_source_hash(source: str) -> Optional[str]:
    """
    Compute a SHA-256 hash over the entire JSON content of a
    mock data file.  Returns None if the file doesn't exist.
    """
    sources = _get_mock_sources()
    path = sources.get(source)
    if not path or not path.exists():
        return None
    try:
        raw = path.read_bytes()
        return hashlib.sha256(raw).hexdigest()
    except Exception:
        return None


# ── Per-source document extraction ─────────────────────────

def _load_source_documents(source: str) -> List:
    """
    Load mock documents for a source using the existing mock
    loader.  Returns a list of LlamaIndex Document objects.
    """
    import importlib
    parts = _SOURCE_LOADERS[source].rsplit(".", 1)
    mod = importlib.import_module(parts[0])
    func = getattr(mod, parts[1])
    return func()


def _load_raw_records(source: str) -> List[dict]:
    """
    Load raw JSON records directly from the mock file.
    Used for change detection at the document level.
    """
    sources = _get_mock_sources()
    path = sources.get(source)
    if not path or not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _record_content_hash(record: dict) -> str:
    """
    Compute a content hash for a raw mock record, based on
    its content field.  This mirrors indexing.doc_identity.content_hash
    but works on raw dict data.
    """
    text = record.get("content", "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_acl_hash(record: dict) -> str:
    """
    Compute a hash over the ACL-relevant fields of a record.
    Detects permission changes separate from content changes.
    """
    acl_fields = {
        "allowed_groups": record.get("allowed_groups", []),
        "restricted_to_users": record.get("restricted_to_users", []),
        "has_restrictions": record.get("has_restrictions", False),
        "restriction_type": record.get("restriction_type", "inherited"),
        "allowed_users": record.get("allowed_users", []),
        "is_private": record.get("is_private", False),
        "member_user_ids": record.get("member_user_ids", []),
        "accessible_by_users": record.get("accessible_by_users", []),
        "accessible_by_domains": record.get("accessible_by_domains", []),
        "is_public": record.get("is_public", False),
        "security_level_id": record.get("security_level_id", ""),
    }
    raw = json.dumps(acl_fields, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── Get all connections grouped by user ───────────────────

def _get_connections_by_user() -> Dict[str, List[dict]]:
    """
    Return a mapping of user_id → list of connections that
    have been synced.  Only connections with source in the four
    mock sources are included.
    """
    from connections.manager import connection_manager

    result: Dict[str, List[dict]] = {}
    for conn in connection_manager._connections.values():
        source = conn.get("source", "")
        if source not in _get_mock_sources() or conn.get("kind", "real") != "mock":
            continue
        uid = conn["user_id"]
        if uid not in result:
            result[uid] = []
        result[uid].append(conn)
    return result


# ── Scheduler ──────────────────────────────────────────────

class AutoSyncScheduler:
    """
    Background thread that polls mock JSON data sources and
    reconciles user indexes.

    Single-instance guard: the _started flag prevents duplicate
    scheduler instances.
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = False
        self._lock = threading.Lock()

        # Per-source per-user last-seen document state
        # {user_id: {source: {source_id: (content_hash, acl_hash)}}}
        self._doc_state: Dict[str, Dict[str, Dict[str, Tuple[str, str]]]] = {}

    # ── Lifecycle ───────────────────────────────────────────

    def start(self):
        """Start the scheduler thread.  Safe to call multiple times."""
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            sync_status_tracker.mark_scheduler_started()
            self._thread = threading.Thread(
                target=self._run,
                name="auto-sync-scheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0):
        """Stop the scheduler and wait for the thread to exit."""
        with self._lock:
            if not self._started:
                return
            self._stop_event.set()
            self._started = False

        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

        sync_status_tracker.mark_scheduler_stopped()

    @property
    def is_running(self) -> bool:
        return self._started and self._thread is not None and self._thread.is_alive()

    # ── Main loop ────────────────────────────────────────────

    def _run(self):
        """
        Main scheduler loop.  Runs until stop() is called.
        """
        interval = _get_sync_interval()
        print(f"[auto-sync] Scheduler started (interval={interval}s)")

        while not self._stop_event.is_set():
            try:
                self._sync_cycle()
            except Exception as e:
                print(f"[auto-sync] Cycle error: {e}")
                cycle = SyncCycleResult(timestamp=_now_iso(), errors=[str(e)])
                sync_status_tracker.record_cycle(cycle)

            # Wait for the next interval, but check stop event
            self._stop_event.wait(timeout=interval)

        print("[auto-sync] Scheduler stopped.")

    # ── Single sync cycle ───────────────────────────────────

    def _sync_cycle(self):
        """
        Execute one synchronization cycle:
          1. For each mock source, check if the file hash changed.
          2. If changed, for each user with a connection to that
             source, detect per-document changes and reconcile.
          3. Record results in the status tracker.
        """
        from permissions.mock_acl_store import is_revoked
        from indexing.indexer import (
            index_exists,
            load_existing_index,
            remove_document_from_index,
            OldIndexFormatError,
        )
        from embeddings.embedding import get_embedding_model

        cycle = SyncCycleResult(timestamp=_now_iso())
        cycle_start = time.time()

        connections_by_user = _get_connections_by_user()

        if not connections_by_user:
            # No connections yet — nothing to sync
            cycle.sources_checked = len(_get_mock_sources())
            sync_status_tracker.record_cycle(cycle)
            return

        for source in _get_mock_sources():
            cycle.sources_checked += 1

            # ── Source-level hash check ────────────────────────
            current_hash = _compute_source_hash(source)
            if current_hash is None:
                continue

            prev_hash = sync_status_tracker.get_source_hash(source)
            sync_status_tracker.set_source_hash(source, current_hash)

            if prev_hash == current_hash:
                # Source unchanged — skip entirely
                continue

            # ── Source changed — do per-user, per-document diff ──
            raw_records = _load_raw_records(source)
            current_docs: Dict[str, dict] = {
                r["id"]: r for r in raw_records
            }

            for user_id, conns in connections_by_user.items():
                # Find connections for THIS source
                source_conns = [c for c in conns if c["source"] == source]
                if not source_conns:
                    continue

                for conn in source_conns:
                    connection_id = conn["connection_id"]

                    # ── Ensure doc state tracking exists ─────────
                    if user_id not in self._doc_state:
                        self._doc_state[user_id] = {}
                    if source not in self._doc_state[user_id]:
                        self._doc_state[user_id][source] = {}

                    prev_docs = self._doc_state[user_id][source]

                    # ── Detect added / updated / deleted docs ────
                    added_ids = []
                    updated_ids = []
                    deleted_ids = []
                    unchanged_count = 0

                    for sid, record in current_docs.items():
                        content_h = _record_content_hash(record)
                        acl_h = _record_acl_hash(record)

                        # Check revocation registry — revoked docs
                        # must NOT be re-indexed even if the JSON
                        # lists them with access.
                        if is_revoked(user_id, source, sid):
                            continue

                        if sid not in prev_docs:
                            added_ids.append(sid)
                        else:
                            prev_content_h, prev_acl_h = prev_docs[sid]
                            if content_h != prev_content_h or acl_h != prev_acl_h:
                                updated_ids.append(sid)
                            else:
                                unchanged_count += 1

                    for sid in list(prev_docs.keys()):
                        if sid not in current_docs:
                            deleted_ids.append(sid)

                    # ── If no changes for this user+source, skip ──
                    if not added_ids and not updated_ids and not deleted_ids:
                        continue

                    cycle.changes_detected = True

                    # ── Apply changes ───────────────────────────────
                    embed_model = get_embedding_model()

                    # Process deletions first (safest)
                    for sid in deleted_ids:
                        if is_revoked(user_id, source, sid):
                            # Already revoked — just clear from state
                            prev_docs.pop(sid, None)
                            continue
                        try:
                            if index_exists(user_id, index_kind="mock"):
                                remove_document_from_index(
                                    user_id=user_id,
                                    embed_model=embed_model,
                                    source=source,
                                    source_id=sid,
                                    connection_id=connection_id,
                                    index_kind="mock",
                                )
                            cycle.deleted += 1
                        except OldIndexFormatError:
                            cycle.errors.append(
                                f"Old index format for {user_id} — skip delete {sid}"
                            )
                        except Exception as e:
                            cycle.errors.append(
                                f"Delete error: {user_id}/{source}/{sid}: {e}"
                            )
                        prev_docs.pop(sid, None)

                    # Process added + updated together via the sync service
                    # which handles revocation checks internally.
                    if added_ids or updated_ids:
                        try:
                            self._reindex_documents(
                                user_id=user_id,
                                connection_id=connection_id,
                                source=source,
                                raw_records=current_docs,
                                sids=added_ids + updated_ids,
                                embed_model=embed_model,
                            )
                            cycle.added += len(added_ids)
                            cycle.updated += len(updated_ids)
                        except Exception as e:
                            cycle.errors.append(
                                f"Reindex error: {user_id}/{source}: {e}"
                            )

                    # Track skipped revoked
                    for sid in current_docs:
                        if is_revoked(user_id, source, sid):
                            cycle.skipped_revoked += 1

                    cycle.skipped_unchanged += unchanged_count

                    # ── Update doc state ───────────────────────────
                    new_state: Dict[str, Tuple[str, str]] = {}
                    for sid, record in current_docs.items():
                        if is_revoked(user_id, source, sid):
                            continue
                        new_state[sid] = (
                            _record_content_hash(record),
                            _record_acl_hash(record),
                        )
                    self._doc_state[user_id][source] = new_state

        # ── Record latency ────────────────────────────────────
        cycle.latency_ms = round((time.time() - cycle_start) * 1000, 2)

        sync_status_tracker.record_cycle(cycle)

    # ── Re-index helper ──────────────────────────────────────

    def _reindex_documents(
        self,
        user_id: str,
        connection_id: str,
        source: str,
        raw_records: Dict[str, dict],
        sids: List[str],
        embed_model,
    ):
        """
        Re-index specific documents by building LlamaIndex
        Documents from raw mock records and calling the existing
        sync service's _process_documents (which handles
        revocation checks, transformation, and indexing).
        """
        from connections.sync_service import ConnectionSyncService
        from llama_index.core import Document as LIDocument

        sync_service = ConnectionSyncService()
        documents = []

        for sid in sids:
            record = raw_records.get(sid)
            if not record:
                continue

            # Build Document with the same metadata format as
            # the mock loaders, so permission_filter works.
            doc = self._build_document(source, record)
            doc.metadata["user_id"] = user_id
            doc.metadata["connection_id"] = connection_id
            doc.metadata["source"] = source
            documents.append(doc)

        if documents:
            sync_service._process_documents(
                documents=documents,
                user_id=user_id,
                connection_id=connection_id,
                source=source,
                kind="mock",
            )

    @staticmethod
    def _build_document(source: str, record: dict):
        """
        Build a LlamaIndex Document from a raw mock record,
        including source-specific ACL metadata.

        This mirrors the _build_document method in
        PermissionReconciliationService to ensure consistency.
        """
        from llama_index.core import Document

        meta = {"source_id": record["id"]}

        if source == "confluence":
            meta.update({
                "source": "confluence",
                "document_type": "confluence_page",
                "title": record.get("title", ""),
                "url": record.get("url", ""),
                "updated_at": record.get("updated_at", ""),
                "space_key": record.get("space_key", ""),
                "allowed_groups": record.get("allowed_groups", []),
                "restricted_to_users": record.get("restricted_to_users", []),
                "restriction_type": record.get("restriction_type", "inherited"),
                "has_restrictions": record.get("has_restrictions", False),
            })
        elif source == "jira":
            meta.update({
                "source": "jira",
                "document_type": "jira_issue",
                "title": record.get("title", ""),
                "url": record.get("url", ""),
                "updated_at": record.get("updated_at", ""),
                "project_key": record.get("project_key", ""),
                "issue_key": record.get("issue_key", record["id"]),
                "issue_type": record.get("issue_type", "unknown"),
                "status": record.get("status", "unknown"),
                "security_level_id": str(record.get("security_level_id", "")),
                "security_level_name": record.get("security_level_name", "none"),
                "allowed_users": record.get("allowed_users", []),
            })
        elif source == "slack":
            meta.update({
                "source": "slack",
                "document_type": "slack_message",
                "title": record.get("title", ""),
                "url": record.get("url", ""),
                "updated_at": record.get("updated_at", ""),
                "channel_id": record.get("channel_id", ""),
                "channel_name": record.get("channel_name", ""),
                "is_private": record.get("is_private", True),
                "member_user_ids": record.get("member_user_ids", []),
            })
        elif source == "google_drive":
            meta.update({
                "source": "google_drive",
                "document_type": "drive_file",
                "title": record.get("title", ""),
                "url": record.get("url", ""),
                "updated_at": record.get("updated_at", ""),
                "folder_id": record.get("folder_id", ""),
                "accessible_by_users": record.get("accessible_by_users", []),
                "accessible_by_domains": record.get("accessible_by_domains", []),
                "shared_drive_id": record.get("shared_drive_id", ""),
                "parent_folder_ids": record.get("parent_folder_ids", []),
                "is_public": record.get("is_public", False),
            })
        else:
            meta["source"] = source

        return Document(text=record.get("content", ""), metadata=meta)

    # ── Manual trigger (for tests) ────────────────────────────

    def sync_once(self):
        """
        Execute a single sync cycle synchronously.  Used by tests
        and the /sync/trigger endpoint.
        """
        self._sync_cycle()


# ── Helper ──────────────────────────────────────────────────

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── Singleton ──────────────────────────────────────────────

auto_sync_scheduler = AutoSyncScheduler()
