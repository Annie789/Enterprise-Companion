# revocation/revocation_service.py

from typing import Optional
from embeddings.embedding import get_embedding_model
from indexing.indexer import (
    load_existing_index,
    index_exists,
    remove_document_from_index,
    remove_source_from_index,
    remove_connection_from_index,
    remove_all_user_documents,
    OldIndexFormatError,
)
from audit.audit_logger import audit_logger


class RevocationService:
    """
    Handles live permission revocation by removing
    specific documents from a user's index at query time.

    All deletion methods use the reliable indexer that removes
    from both docstore AND vector store, ensuring revoked
    content is non-retrievable.

    All operations require authorized caller (Task 1 RBAC).
    """

    def revoke_resource(
        self,
        user_id:     str,
        source:      str,
        resource_id: str,
        revoked_by:  str = "system",
    ) -> dict:
        """
        Remove all nodes for a specific document from a user's index.
        Uses delete_ref_doc to remove from docstore AND vector store.
        """
        embed_model = get_embedding_model()

        if not index_exists(user_id):
            audit_logger.log(
                event_type  = "REVOKE",
                user_id     = user_id,
                source      = source,
                resource_id = resource_id,
                outcome     = "DENIED",
                detail      = {
                    "revoked_by": revoked_by,
                    "reason":     "no_index",
                    "mode":       "resource",
                },
            )
            return {"status": "no_index", "removed": 0}

        try:
            removed = remove_document_from_index(
                user_id=user_id,
                embed_model=embed_model,
                source=source,
                source_id=resource_id,
            )
        except OldIndexFormatError:
            removed = self._revoke_old_format(
                user_id, embed_model, source=source, resource_id=resource_id
            )
        except Exception as e:
            audit_logger.log(
                event_type  = "REVOKE",
                user_id     = user_id,
                source      = source,
                resource_id = resource_id,
                outcome     = "DENIED",
                detail      = {
                    "revoked_by": revoked_by,
                    "error":      str(e),
                    "mode":       "resource",
                },
            )
            return {
                "status": "error",
                "error": str(e),
                "removed": 0,
            }

        audit_logger.log(
            event_type  = "REVOKE",
            user_id     = user_id,
            source      = source,
            resource_id = resource_id,
            outcome     = "ALLOWED",
            detail      = {
                "revoked_by":   revoked_by,
                "nodes_removed": removed,
                "mode":         "resource",
            },
        )

        return {
            "status":  "revoked",
            "user_id": user_id,
            "source":  source,
            "resource_id": resource_id,
            "nodes_removed": removed,
        }

    def revoke_source(
        self,
        user_id:    str,
        source:     str,
        revoked_by: str = "system",
    ) -> dict:
        """
        Remove ALL nodes from a source for a user.
        Uses reliable deletion from docstore AND vector store.
        """
        embed_model = get_embedding_model()

        if not index_exists(user_id):
            audit_logger.log(
                event_type = "REVOKE",
                user_id    = user_id,
                source     = source,
                outcome    = "ALLOWED",
                detail     = {
                    "revoked_by":    revoked_by,
                    "nodes_removed": 0,
                    "mode":          "source",
                    "reason":        "no_index",
                },
            )
            return {
                "status":        "revoked",
                "user_id":       user_id,
                "source":        source,
                "nodes_removed": 0,
            }

        try:
            removed = remove_source_from_index(
                user_id=user_id,
                embed_model=embed_model,
                source=source,
            )
        except OldIndexFormatError:
            removed = self._revoke_old_format(
                user_id, embed_model, source=source
            )
        except Exception as e:
            audit_logger.log(
                event_type = "REVOKE",
                user_id    = user_id,
                source     = source,
                outcome    = "DENIED",
                detail     = {
                    "revoked_by":    revoked_by,
                    "error":         str(e),
                    "mode":          "source",
                },
            )
            return {
                "status":        "error",
                "error":         str(e),
                "user_id":       user_id,
                "source":        source,
                "nodes_removed": 0,
            }

        audit_logger.log(
            event_type = "REVOKE",
            user_id    = user_id,
            source     = source,
            outcome    = "ALLOWED",
            detail     = {
                "revoked_by":    revoked_by,
                "nodes_removed": removed,
                "mode":          "source",
            },
        )

        return {
            "status":        "revoked",
            "user_id":       user_id,
            "source":        source,
            "nodes_removed": removed,
        }

    def revoke_connection(
        self,
        user_id:       str,
        connection_id: str,
        revoked_by:    str = "system",
    ) -> dict:
        """
        Remove ALL nodes belonging to a specific connection.
        Prevents disconnecting one connection from deleting
        another connection to the same source.
        """
        embed_model = get_embedding_model()

        if not index_exists(user_id):
            return {"status": "no_index", "removed": 0}

        try:
            removed = remove_connection_from_index(
                user_id=user_id,
                embed_model=embed_model,
                connection_id=connection_id,
            )
        except Exception as e:
            audit_logger.log(
                event_type = "REVOKE",
                user_id    = user_id,
                outcome    = "DENIED",
                detail     = {
                    "revoked_by":    revoked_by,
                    "connection_id": connection_id,
                    "error":         str(e),
                    "mode":          "connection",
                },
            )
            return {
                "status":        "error",
                "error":         str(e),
                "removed":       0,
            }

        audit_logger.log(
            event_type = "REVOKE",
            user_id    = user_id,
            outcome    = "ALLOWED",
            detail     = {
                "revoked_by":    revoked_by,
                "connection_id": connection_id,
                "nodes_removed": removed,
                "mode":          "connection",
            },
        )

        return {
            "status":        "revoked",
            "user_id":       user_id,
            "connection_id": connection_id,
            "nodes_removed": removed,
        }

    def revoke_all_user_documents(
        self,
        user_id:    str,
        revoked_by: str = "system",
    ) -> dict:
        """
        Remove ALL documents belonging to a user.
        """
        embed_model = get_embedding_model()

        if not index_exists(user_id):
            return {"status": "no_index", "removed": 0}

        try:
            removed = remove_all_user_documents(
                user_id=user_id,
                embed_model=embed_model,
            )
        except Exception as e:
            audit_logger.log(
                event_type = "REVOKE",
                user_id    = user_id,
                outcome    = "DENIED",
                detail     = {
                    "revoked_by":    revoked_by,
                    "error":         str(e),
                    "mode":          "all_user",
                },
            )
            return {
                "status":        "error",
                "error":         str(e),
                "removed":       0,
            }

        audit_logger.log(
            event_type = "REVOKE",
            user_id    = user_id,
            outcome    = "ALLOWED",
            detail     = {
                "revoked_by":    revoked_by,
                "nodes_removed": removed,
                "mode":          "all_user",
            },
        )

        return {
            "status":        "revoked",
            "user_id":       user_id,
            "nodes_removed": removed,
        }

    def _revoke_old_format(
        self,
        user_id: str,
        embed_model,
        source: str = None,
        resource_id: str = None,
    ) -> int:
        """
        Fallback for old-format indexes.
        Uses node-level metadata matching + delete_nodes.
        """
        from indexing.vector_store import load_storage_context
        from llama_index.core import load_index_from_storage

        storage_context = load_storage_context(user_id)
        index = load_index_from_storage(
            storage_context=storage_context,
            embed_model=embed_model,
        )

        node_ids_to_remove = []
        for node_id, node in index.docstore.docs.items():
            meta = node.metadata or {}
            if source and meta.get("source", "") != source:
                continue
            if resource_id and meta.get("source_id", "") != resource_id:
                continue
            node_ids_to_remove.append(node_id)

        if not node_ids_to_remove:
            return 0

        try:
            index.delete_nodes(node_ids_to_remove, delete_from_docstore=True)
        except Exception:
            for nid in node_ids_to_remove:
                try:
                    index.docstore.delete_document(nid)
                except Exception:
                    pass

        from indexing.vector_store import get_index_directory
        index_dir = get_index_directory(user_id)
        index.storage_context.persist(persist_dir=str(index_dir))
        return len(node_ids_to_remove)


revocation_service = RevocationService()
