# connections/sync_service.py

from ingestion.loader.confluence import load_real_confluence
from ingestion.loader.jira import load_real_jira
from ingestion.loader.slack import load_real_slack
from ingestion.loader.drive import load_real_google_drive

from embeddings.embedding import get_embedding_model
from transformation.transformation import TransformationService
from indexing.indexer import add_nodes_to_index


class ConnectionSyncService:

    def _process_documents(
        self,
        documents,
        user_id: str,
        connection_id: str,
        source: str,
        kind: str = "real",
    ) -> dict:

        if not documents:
            return {
                "status":    "completed",
                "source":    source,
                "documents": 0,
                "nodes":     0,
            }

        # ── Stale-sync guard (Task 4) ────────────────────────────────
        # Filter out documents that have been explicitly revoked via
        # the reconciliation service.  A stale scheduled sync must NOT
        # restore access to content that was revoked by a live permission
        # change, even if the old ACL metadata still lists the user as
        # having access.
        from permissions.mock_acl_store import is_revoked

        filtered = []
        skipped_revoked = []
        for document in documents:
            sid = document.metadata.get("source_id", "")
            if is_revoked(user_id, source, sid):
                skipped_revoked.append(sid)
            else:
                filtered.append(document)

        if not filtered:
            return {
                "status":          "completed",
                "source":          source,
                "documents":       0,
                "nodes":           0,
                "skipped_revoked": skipped_revoked,
            }

        documents = filtered

        # ── Stamp ownership metadata onto every document ───────────────
        # source_id must already be set by the loader (from the
        # record's "id" field). We stamp user_id, connection_id,
        # and source here to bind content to the connection.
        for document in documents:
            document.metadata["user_id"]       = user_id
            document.metadata["connection_id"] = connection_id
            document.metadata["source"]        = source
            document.metadata["index_kind"] = kind

        embed_model = get_embedding_model()
        transformer = TransformationService(embed_model=embed_model)
        nodes       = transformer.transform(documents)

        # ── Stamp ownership metadata onto every node ───────────────────
        # Each chunk inherits the parent document's identity metadata.
        for node in nodes:
            node.metadata["user_id"]       = user_id
            node.metadata["connection_id"] = connection_id
            node.metadata["source"]        = source
            node.metadata["index_kind"] = kind

        add_nodes_to_index(
            nodes=nodes,
            embed_model=embed_model,
            user_id=user_id,
            index_kind=kind,
        )

        return {
            "status":          "completed",
            "source":          source,
            "documents":       len(documents),
            "nodes":           len(nodes),
            "skipped_revoked": skipped_revoked,
        }

    # ──────────────────────────────────────────────────────────────────
    # Per-source sync methods — all pass credentials as a dict
    # ──────────────────────────────────────────────────────────────────

    def sync_confluence(self, user_id, connection_id, credentials):
        documents = load_real_confluence(credentials={
            "base_url":   credentials["base_url"],
            "email":      credentials.get("email", ""),
            "api_token":  credentials["api_token"],
            "space_keys": credentials.get("space_keys", []),
        })
        return self._process_documents(
            documents=documents,
            user_id=user_id,
            connection_id=connection_id,
            source="confluence",
        )

    def sync_jira(self, user_id, connection_id, credentials):
        documents = load_real_jira(credentials={
            "server_url":   credentials["server_url"],
            "email":        credentials["email"],
            "api_token":    credentials["api_token"],
            "project_keys": credentials.get("project_keys", []),
        })
        return self._process_documents(
            documents=documents,
            user_id=user_id,
            connection_id=connection_id,
            source="jira",
        )

    def sync_slack(self, user_id, connection_id, credentials, verified_user_email=""):
        documents = load_real_slack(credentials={
            "bot_token":   credentials["bot_token"],
            "channel_ids": credentials.get("channel_ids", []),
            "email": verified_user_email,
        })
        return self._process_documents(
            documents=documents,
            user_id=user_id,
            connection_id=connection_id,
            source="slack",
        )

    def sync_google_drive(self, user_id, connection_id, credentials):
        documents = load_real_google_drive(credentials={
            "folder_ids":              credentials.get("folder_ids", []),
            "service_account_json_path": credentials.get("service_account_json_path", ""),
            "service_account_json":      credentials.get("service_account_json"),
        })
        return self._process_documents(
            documents=documents,
            user_id=user_id,
            connection_id=connection_id,
            source="google_drive",
        )


connection_sync_service = ConnectionSyncService()
