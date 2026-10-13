# permissions/permission_reconciliation.py
#
# Task 4 — Live Permission Change Handling
#
# The PermissionReconciliationService is the core of the live
# permission-change workflow.  It is triggered by a mock webhook
# event (see backend.py POST /permissions/reconcile) and performs
# the following:
#
# 1. Reads the CURRENT source ACL metadata from the mock JSON files
#    (which were modified by mock_acl_store to simulate a real
#    platform permission change).
# 2. Reads the CURRENTLY indexed documents from the user's index.
# 3. Compares the two and reconciles:
#    - Permission revoked → delete from index immediately
#    - Permission granted → re-index the document (after verifying
#      the user now has access in the updated ACL)
#    - Document deleted from source → remove from index
#    - Unchanged ACL → skip (no unnecessary reindexing)
#
# The service reuses:
#   - Task 1 authentication (all endpoints calling this service
#     are behind require_admin or require_user_or_admin).
#   - Task 3 reliable vector deletion (remove_document_from_index)
#     and connection-level identity (stable_doc_id).
#   - The existing audit system (audit_logger.log).
#
# Stale-sync guard:
#   The service uses mock_acl_store.is_revoked() to check whether
#   a document has been explicitly revoked.  When the sync service
#   (or add_nodes_to_index) encounters a revoked document, it must
#   NOT re-index it.  The revocation registry is cleared only when
#   a GRANT event is explicitly processed here.

import json
from typing import Dict, List, Optional, Tuple

from llama_index.core import Document

from embeddings.embedding import get_embedding_model
from indexing.indexer import (
    load_existing_index,
    index_exists,
    remove_document_from_index,
    OldIndexFormatError,
)
from indexing.doc_identity import stable_doc_id
from retrieval.permission_filter import filter_by_permissions
from llama_index.core.schema import NodeWithScore

from audit.audit_logger import audit_logger
from permissions.mock_acl_store import (
    get_all_mock_documents,
    get_mock_document,
    register_revocation,
    clear_revocation,
    is_revoked,
)


# ── Per-source user identity mapping ─────────────────────────
# Maps a source to the field in the mock record that identifies
# the user (e.g., account_id for Confluence, email for Jira/Drive,
# slack_user_id for Slack).

def _get_user_identity(source: str, user_context: dict) -> str:
    """Get the user's identity token for a given source."""
    if source == "confluence":
        return user_context.get("confluence_account_id", "")
    elif source == "jira":
        return user_context.get("email", "")
    elif source == "slack":
        return user_context.get("slack", {}).get("user_id", "")
    elif source == "google_drive":
        return user_context.get("email", "")
    return ""


# ── ACL check: does the user have access to this mock record? ──

def _user_has_access(source: str, record: dict, user_context: dict) -> bool:
    """
    Check whether the user_context has access to a mock record
    using the same per-source ACL logic as the permission filter.

    This is intentionally a re-implementation of the permission
    filter's check, because the permission filter operates on
    indexed NodeWithScore objects, not raw mock records.  The
    logic must match to avoid false grants or false denials.
    """
    from retrieval.permission_filter import (
        _check_confluence,
        _check_jira,
        _check_slack,
        _check_google_drive,
    )

    checkers = {
        "confluence":   _check_confluence,
        "jira":         _check_jira,
        "slack":         _check_slack,
        "google_drive": _check_google_drive,
    }
    checker = checkers.get(source)
    if not checker:
        return False
    return checker(record, user_context)


# ── Read currently-indexed document metadata ─────────────────

def _get_indexed_doc_infos(
    user_id: str,
    source: Optional[str] = None,
    connection_id: Optional[str] = None,
) -> List[dict]:
    """
    Read the ref_doc_info entries from the user's index.
    Returns a list of dicts with:
      - doc_id
      - source
      - source_id
      - connection_id
      - user_id
    """
    embed_model = get_embedding_model()
    if not index_exists(user_id):
        return []

    try:
        index = load_existing_index(embed_model=embed_model, user_id=user_id)
    except OldIndexFormatError:
        return []

    infos = []
    for ref_doc_id, ref_info in index.ref_doc_info.items():
        meta = ref_info.metadata or {}
        if source and meta.get("source", "") != source:
            continue
        if connection_id and meta.get("connection_id", "") != connection_id:
            continue
        infos.append({
            "doc_id":       ref_doc_id,
            "source":       meta.get("source", ""),
            "source_id":    meta.get("source_id", ""),
            "connection_id": meta.get("connection_id", ""),
            "user_id":      meta.get("user_id", ""),
        })
    return infos


class PermissionReconciliationService:
    """
    Reconciles the indexed content with the source ACL metadata
    after a permission change event.

    All methods assume the caller is already authenticated and
    authorized (Task 1).  The user_context is built from the
    authenticated user's server-defined attributes — never from
    the request body.
    """

    def reconcile(
        self,
        user_id: str,
        source: str,
        source_id: str,
        user_context: dict,
        action: str = "revoke",
        connection_id: str = "",
        triggered_by: str = "webhook",
    ) -> dict:
        """
        Reconcile a single document after a permission change.

        Parameters
        ----------
        user_id      : the user whose index to reconcile
        source       : the data source (confluence, jira, etc.)
        source_id    : the document ID in that source
        user_context : the user's permission context (from AuthUser)
        action       : "revoke" | "grant" | "delete" | "check"
        connection_id: the connection that owns the indexed content
        triggered_by : who/what triggered this reconciliation

        Returns a summary dict with actions taken.
        """
        embed_model = get_embedding_model()
        result = {
            "user_id":      user_id,
            "source":       source,
            "source_id":    source_id,
            "action":       action,
            "connection_id": connection_id,
            "indexed_before": False,
            "indexed_after":  False,
            "nodes_removed":  0,
            "nodes_added":    0,
            "skipped":        False,
            "reason":         "",
        }

        # ── Check if currently indexed ────────────────────────────
        indexed_infos = _get_indexed_doc_infos(
            user_id, source=source, connection_id=connection_id or None,
        )
        was_indexed = any(
            info["source_id"] == source_id for info in indexed_infos
        )
        result["indexed_before"] = was_indexed

        # ── Read the current source ACL metadata ──────────────────
        mock_record = get_mock_document(source, source_id)

        # ── Action: REVOKE ─────────────────────────────────────────
        if action == "revoke":
            # Register in the revocation registry so stale syncs
            # cannot restore this document.
            register_revocation(user_id, source, source_id)

            if was_indexed:
                # Delete from index immediately
                removed = self._remove_document(
                    user_id, source, source_id, connection_id,
                )
                result["nodes_removed"] = removed

                audit_logger.log(
                    event_type="REVOKE",
                    user_id=user_id,
                    source=source,
                    resource_id=source_id,
                    outcome="ALLOWED",
                    detail={
                        "triggered_by":  triggered_by,
                        "nodes_removed": removed,
                        "mode":          "reconciliation_revoke",
                    },
                )
            else:
                result["skipped"] = True
                result["reason"] = "not_indexed"

            audit_logger.log(
                event_type="PERMISSION_CHANGE",
                user_id=user_id,
                source=source,
                resource_id=source_id,
                outcome="ALLOWED",
                detail={
                    "action":       "revoke",
                    "triggered_by": triggered_by,
                    "was_indexed":  was_indexed,
                },
            )

        # ── Action: GRANT ───────────────────────────────────────────
        elif action == "grant":
            # Clear the revocation registry entry — access is
            # being re-granted.  But only after verifying the
            # user actually has access in the updated ACL.
            if mock_record is None:
                result["skipped"] = True
                result["reason"] = "document_not_found_in_source"
                return result

            has_access = _user_has_access(source, mock_record, user_context)
            if not has_access:
                result["skipped"] = True
                result["reason"] = "access_not_verified_in_updated_acl"
                # Do NOT clear the revocation — access was not verified
                return result

            # Access verified — clear revocation and re-index
            clear_revocation(user_id, source, source_id)

            if not was_indexed:
                added = self._reindex_document(
                    user_id, source, source_id, connection_id,
                    mock_record, user_context,
                )
                result["nodes_added"] = added

                audit_logger.log(
                    event_type="PERMISSION_CHANGE",
                    user_id=user_id,
                    source=source,
                    resource_id=source_id,
                    outcome="ALLOWED",
                    detail={
                        "action":       "grant",
                        "triggered_by": triggered_by,
                        "nodes_added":  added,
                        "access_verified": True,
                    },
                )
            else:
                result["skipped"] = True
                result["reason"] = "already_indexed"

        # ── Action: DELETE ─────────────────────────────────────────
        elif action == "delete":
            # The document was deleted from the source. Remove
            # from index regardless of the revocation registry.
            if was_indexed:
                removed = self._remove_document(
                    user_id, source, source_id, connection_id,
                )
                result["nodes_removed"] = removed

                audit_logger.log(
                    event_type="PERMISSION_CHANGE",
                    user_id=user_id,
                    source=source,
                    resource_id=source_id,
                    outcome="ALLOWED",
                    detail={
                        "action":       "delete",
                        "triggered_by": triggered_by,
                        "nodes_removed": removed,
                    },
                )
            else:
                result["skipped"] = True
                result["reason"] = "not_indexed"

            # Also register revocation so stale sync doesn't restore
            register_revocation(user_id, source, source_id)

        # ── Action: CHECK (unchanged ACL reconciliation) ────────────
        elif action == "check":
            # Used by full reconciliation to detect drift without
            # a specific action.  If the user no longer has access
            # in the source ACL but the doc is indexed, revoke it.
            if mock_record is None:
                # Document deleted from source but still indexed
                if was_indexed:
                    removed = self._remove_document(
                        user_id, source, source_id, connection_id,
                    )
                    result["nodes_removed"] = removed
                    register_revocation(user_id, source, source_id)
                    result["reason"] = "deleted_from_source"
                else:
                    result["skipped"] = True
                    result["reason"] = "not_indexed_and_not_in_source"
            else:
                has_access = _user_has_access(source, mock_record, user_context)
                if has_access and not was_indexed and not is_revoked(user_id, source, source_id):
                    # Access granted but not indexed — re-index
                    added = self._reindex_document(
                        user_id, source, source_id, connection_id,
                        mock_record, user_context,
                    )
                    result["nodes_added"] = added
                    result["reason"] = "access_granted_not_indexed"
                elif not has_access and was_indexed:
                    # Access revoked but still indexed — remove
                    register_revocation(user_id, source, source_id)
                    removed = self._remove_document(
                        user_id, source, source_id, connection_id,
                    )
                    result["nodes_removed"] = removed
                    result["reason"] = "access_revoked_still_indexed"
                else:
                    result["skipped"] = True
                    result["reason"] = "no_change"

        # ── Final state check ──────────────────────────────────────
        indexed_after = self._is_document_indexed(
            user_id, source, source_id, connection_id,
        )
        result["indexed_after"] = indexed_after

        return result

    def reconcile_all(
        self,
        user_id: str,
        source: str,
        user_context: dict,
        connection_id: str = "",
        triggered_by: str = "webhook",
    ) -> List[dict]:
        """
        Reconcile ALL documents for a given source.

        This iterates over:
          - All mock documents in the source JSON
          - All indexed documents for this user+source

        And reconciles each one using action="check".
        """
        results = []

        # ── Get all source mock records ───────────────────────────
        mock_records = get_all_mock_documents(source)
        mock_ids = {r["id"] for r in mock_records}

        # ── Get all indexed documents ──────────────────────────────
        indexed_infos = _get_indexed_doc_infos(
            user_id, source=source, connection_id=connection_id or None,
        )
        indexed_ids = {info["source_id"] for info in indexed_infos}

        # ── Check each mock record ─────────────────────────────────
        for record in mock_records:
            sid = record["id"]
            r = self.reconcile(
                user_id=user_id,
                source=source,
                source_id=sid,
                user_context=user_context,
                action="check",
                connection_id=connection_id,
                triggered_by=triggered_by,
            )
            results.append(r)

        # ── Check for indexed docs no longer in source (deleted) ──
        deleted_ids = indexed_ids - mock_ids
        for sid in deleted_ids:
            r = self.reconcile(
                user_id=user_id,
                source=source,
                source_id=sid,
                user_context=user_context,
                action="delete",
                connection_id=connection_id,
                triggered_by=triggered_by,
            )
            results.append(r)

        return results

    # ── Internal helpers ────────────────────────────────────────

    def _remove_document(
        self,
        user_id: str,
        source: str,
        source_id: str,
        connection_id: str = "",
    ) -> int:
        """Remove a document from the user's index."""
        embed_model = get_embedding_model()
        try:
            return remove_document_from_index(
                user_id=user_id,
                embed_model=embed_model,
                source=source,
                source_id=source_id,
                connection_id=connection_id,
            )
        except OldIndexFormatError:
            # Fallback for old format indexes
            from revocation.revocation_service import revocation_service
            result = revocation_service.revoke_resource(
                user_id=user_id,
                source=source,
                resource_id=source_id,
                revoked_by="reconciliation_service",
            )
            return result.get("nodes_removed", 0)
        except Exception as e:
            print(f"  [RECONCILE] Failed to remove {source_id}: {e}")
            return 0

    def _reindex_document(
        self,
        user_id: str,
        source: str,
        source_id: str,
        connection_id: str,
        mock_record: dict,
        user_context: dict,
    ) -> int:
        """
        Re-index a document after access is granted.

        Uses the mock loader to create a Document, then transforms
        and indexes it via the standard sync path.
        """
        from transformation.transformation import TransformationService
        from indexing.indexer import add_nodes_to_index, create_index

        embed_model = get_embedding_model()

        # Build a Document from the mock record
        # We use the loader's metadata format so permission_filter
        # works correctly at query time.
        doc = self._build_document(source, mock_record)
        doc.metadata["user_id"] = user_id
        doc.metadata["connection_id"] = connection_id
        doc.metadata["source"] = source

        transformer = TransformationService(embed_model=embed_model)
        nodes = transformer.transform([doc])

        for node in nodes:
            node.metadata["user_id"] = user_id
            node.metadata["connection_id"] = connection_id
            node.metadata["source"] = source

        add_nodes_to_index(
            nodes=nodes,
            embed_model=embed_model,
            user_id=user_id,
        )
        return len(nodes)

    def _build_document(self, source: str, record: dict) -> Document:
        """
        Build a LlamaIndex Document from a mock record, including
        the source-specific ACL metadata that permission_filter
        expects.
        """
        meta = {"source_id": record["id"]}

        if source == "confluence":
            meta.update({
                "source":             "confluence",
                "document_type":      "confluence_page",
                "title":              record.get("title", ""),
                "url":                record.get("url", ""),
                "updated_at":         record.get("updated_at", ""),
                "space_key":          record.get("space_key", ""),
                "allowed_groups":      record.get("allowed_groups", []),
                "restricted_to_users": record.get("restricted_to_users", []),
                "restriction_type":    record.get("restriction_type", "inherited"),
                "has_restrictions":    record.get("has_restrictions", False),
            })
        elif source == "jira":
            meta.update({
                "source":             "jira",
                "document_type":      "jira_issue",
                "title":              record.get("title", ""),
                "url":                record.get("url", ""),
                "updated_at":         record.get("updated_at", ""),
                "project_key":        record.get("project_key", ""),
                "issue_key":          record.get("issue_key", record["id"]),
                "issue_type":         record.get("issue_type", "unknown"),
                "status":             record.get("status", "unknown"),
                "security_level_id":   str(record.get("security_level_id", "")),
                "security_level_name": record.get("security_level_name", "none"),
                "allowed_users":      record.get("allowed_users", []),
            })
        elif source == "slack":
            meta.update({
                "source":             "slack",
                "document_type":      "slack_message",
                "title":              record.get("title", ""),
                "url":                record.get("url", ""),
                "updated_at":         record.get("updated_at", ""),
                "channel_id":         record.get("channel_id", ""),
                "channel_name":       record.get("channel_name", ""),
                "is_private":         record.get("is_private", True),
                "member_user_ids":    record.get("member_user_ids", []),
            })
        elif source == "google_drive":
            meta.update({
                "source":             "google_drive",
                "document_type":      "drive_file",
                "title":              record.get("title", ""),
                "url":                record.get("url", ""),
                "updated_at":         record.get("updated_at", ""),
                "folder_id":          record.get("folder_id", ""),
                "accessible_by_users":   record.get("accessible_by_users", []),
                "accessible_by_domains": record.get("accessible_by_domains", []),
                "shared_drive_id":       record.get("shared_drive_id", ""),
                "parent_folder_ids":     record.get("parent_folder_ids", []),
                "is_public":             record.get("is_public", False),
            })
        else:
            meta["source"] = source

        return Document(text=record.get("content", ""), metadata=meta)

    def _is_document_indexed(
        self,
        user_id: str,
        source: str,
        source_id: str,
        connection_id: str = "",
    ) -> bool:
        """Check if a document is currently in the user's index."""
        indexed_infos = _get_indexed_doc_infos(
            user_id, source=source, connection_id=connection_id or None,
        )
        return any(info["source_id"] == source_id for info in indexed_infos)


# ── Singleton ───────────────────────────────────────────────
reconciliation_service = PermissionReconciliationService()
