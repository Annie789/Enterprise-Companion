# backend.py

import os
from contextlib import asynccontextmanager
from typing import List, Optional
import traceback

from fastapi import Depends, FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from audit.audit_query import query_audit_log, verify_chain_integrity
from audit.audit_logger import audit_logger
from revocation.revocation_service import revocation_service
from permissions.permission_reconciliation import reconciliation_service

from sync.auto_sync_scheduler import auto_sync_scheduler
from sync.sync_status import sync_status_tracker


from embeddings.embedding import (
    get_embedding_model,
)

from indexing.indexer import (
    load_existing_index,
)

from retrieval.retriever import (
    RetrievalService,
)

from generation.adp_client import (
    ADPClient,
)

from pipeline.rag_pipeline import (
    RAGPipeline,
)

from connections import (
    connection_manager,

    confluence_connection_service,
    jira_connection_service,
    slack_connection_service,
    google_drive_connection_service,

    connection_sync_service,

    ConfluenceConnectionRequest,
    JiraConnectionRequest,
    SlackConnectionRequest,
    GoogleDriveConnectionRequest,
)

from auth import (
    AuthUser,
    DEMO_USERS,
    get_current_user,
    reject_identity_mismatch,
    require_admin,
    require_user_or_admin,
)

def _get_user_slack_channels(user_id: str) -> list:
    """
    Returns the channel_ids stored when user connected Slack.
    Used to populate user_context for permission filtering.
    """
    connections = connection_manager.get_user_connections(user_id)
    for conn in connections:
        if conn.get("source") == "slack":
            return conn.get("credentials", {}).get("channel_ids", [])
    return []

# =========================================================
# FastAPI application with lifespan
# =========================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan: starts and stops the auto-sync scheduler.

    Using lifespan (instead of @app.on_event) ensures clean
    startup/shutdown and avoids duplicate scheduler instances
    through the auto_sync_scheduler singleton guard.
    """
    global embed_model
    global adp_client

    print("\nInitializing Enterprise RAG...")

    embed_model = get_embedding_model()
    print("Embedding model initialized.")

    adp_client = ADPClient()
    print("ADP client initialized.")

    # Start auto-sync scheduler
    auto_sync_scheduler.start()
    print("Auto-sync scheduler started.")

    print("Enterprise RAG initialized successfully.\n")

    yield

    # ── Shutdown: stop the scheduler cleanly ─────────────────
    auto_sync_scheduler.stop()
    print("Auto-sync scheduler stopped.")


app = FastAPI(
    title="Enterprise Companion",
    description="Permission-aware Agentic RAG for Enterprise Knowledge",
    version="1.0.0",
    lifespan=lifespan,
)


# =========================================================
# CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",
        "http://127.0.0.1:8501",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# Request Models
# =========================================================

class UserRequest(BaseModel):
    """
    Query request. Identity fields are intentionally
    absent — effective identity and all permission
    metadata are derived from the authenticated user,
    never from the request body.
    """
    query: str


# =========================================================
# Shared Application Services
# =========================================================

embed_model = None
adp_client = None


# =========================================================
# User-specific RAG pipeline
# =========================================================

def get_user_rag_pipeline(user_id: str) -> RAGPipeline:

    indexes = []
    for kind in ("mock", "real"):
        try:
            indexes.append(load_existing_index(
                embed_model=embed_model, user_id=user_id, index_kind=kind
            ))
        except FileNotFoundError:
            pass
    if not indexes:
        raise FileNotFoundError(f"No mock or real index for {user_id}")

    retrieval_service = RetrievalService(
        index=indexes,
        initial_top_k=20,
        final_top_k=5,
    )

    return RAGPipeline(
        retrieval_service=retrieval_service,
        adp_client=ADPClient(visitor_id=user_id),   # ← per-user visitor_id
    )


# =========================================================
# Root
# =========================================================

@app.get("/")
def root():

    return {
        "status": "running",
        "message": (
            "Enterprise RAG backend is ready"
        ),
    }


# =========================================================
# Health
# =========================================================

@app.get("/health")
def health():

    return {
        "status": "healthy",
        "embedding_initialized": (
            embed_model is not None
        ),
        "adp_initialized": (
            adp_client is not None
        ),
    }


# =========================================================
# Auth — demo user info
# =========================================================

@app.get("/auth/whoami")
def whoami(
    user: AuthUser = Depends(get_current_user),
):
    """
    Returns the authenticated user's identity and role.
    Does NOT expose the API key or sensitive source IDs.
    """
    return {
        "user_id": user.user_id,
        "email": user.email,
        "role": user.role,
        "groups": user.groups,
        "is_admin": user.is_admin,
    }


@app.get("/auth/users")
def list_demo_users(
    user: AuthUser = Depends(get_current_user),
):
    """
    Lists available demo personas (user IDs and roles only).
    Available to any authenticated user so they can pick
    a persona in the Streamlit login screen.
    """
    require_admin(user)
    return {
        "users": [
            {
                "user_id": u.user_id,
                "email": u.email,
                "role": u.role,
                "groups": u.groups,
            }
            for u in DEMO_USERS.values()
        ]
    }


# =========================================================
# Confluence
# =========================================================

@app.post(
    "/connections/confluence/test"
)
def test_confluence_connection(
    request: ConfluenceConnectionRequest,
    user: AuthUser = Depends(get_current_user),
):

    reject_identity_mismatch(request.user_id, user)

    try:

        result = (
            confluence_connection_service
            .test_connection(
                base_url=request.base_url,
                api_token=request.api_token,
                email=request.email,
                space_keys=request.space_keys,
            )
        )

        return {
            "source": "confluence",
            "status": "success",
            "details": result,
        }

    except Exception as exc:

        return {
            "source": "confluence",
            "status": "error",
            "error": str(exc),
        }


@app.post(
    "/connections/confluence"
)
def connect_confluence(
    request: ConfluenceConnectionRequest,
    user: AuthUser = Depends(get_current_user),
):

    reject_identity_mismatch(request.user_id, user)

    try:

        # Validate real provider credentials before storing the connection
        confluence_connection_service\
            .test_connection(
                base_url=request.base_url,
                api_token=request.api_token,
                email=request.email,
                space_keys=request.space_keys,
            )

        # Store connection
        connection_id = (
            connection_manager
            .create_connection(
                user_id=user.user_id,
                source="confluence",
                credentials={
                    "base_url": (
                        request.base_url
                    ),
                    "api_token": (
                        request.api_token
                    ),
                    "space_keys": (
                        request.space_keys
                    ),
                    "email": (
                        request.email
                    ),
                },
            )
        )

        # Do not return API token
        return {
            "connection_id": (
                connection_id
            ),
            "user_id": (
                user.user_id
            ),
            "source": "confluence",
            "status": "connected",
            "base_url": (
                request.base_url
            ),
            "space_keys": (
                request.space_keys
            ),
        }

    except Exception as exc:

        return {
            "source": "confluence",
            "status": "error",
            "error": str(exc),
        }


# =========================================================
# Jira
# =========================================================

@app.post(
    "/connections/jira/test"
)
def test_jira_connection(
    request: JiraConnectionRequest,
    user: AuthUser = Depends(get_current_user),
):

    reject_identity_mismatch(request.user_id, user)

    try:

        result = (
            jira_connection_service
            .test_connection(
                server_url=request.server_url,
                email=request.email,
                api_token=request.api_token,
                project_keys=request.project_keys,
            )
        )

        return {
            "source": "jira",
            "status": "success",
            "details": result,
        }

    except Exception as exc:

        return {
            "source": "jira",
            "status": "error",
            "error": str(exc),
        }


@app.post(
    "/connections/jira"
)
def connect_jira(
    request: JiraConnectionRequest,
    user: AuthUser = Depends(get_current_user),
):

    reject_identity_mismatch(request.user_id, user)

    try:

        jira_connection_service\
            .test_connection(
                server_url=request.server_url,
                email=request.email,
                api_token=request.api_token,
                project_keys=request.project_keys,
            )

        connection_id = (
            connection_manager
            .create_connection(
                user_id=user.user_id,
                source="jira",
                credentials={
                    "server_url": (
                        request.server_url
                    ),
                    "email": (
                        request.email
                    ),
                    "api_token": (
                        request.api_token
                    ),
                    "project_keys": (
                        request.project_keys
                    ),
                },
            )
        )

        return {
            "connection_id": (
                connection_id
            ),
            "user_id": (
                user.user_id
            ),
            "source": "jira",
            "status": "connected",
            "server_url": (
                request.server_url
            ),
            "project_keys": (
                request.project_keys
            ),
        }

    except Exception as exc:

        return {
            "source": "jira",
            "status": "error",
            "error": str(exc),
        }


# =========================================================
# Slack
# =========================================================

@app.post(
    "/connections/slack/test"
)
def test_slack_connection(
    request: SlackConnectionRequest,
    user: AuthUser = Depends(get_current_user),
):

    reject_identity_mismatch(request.user_id, user)

    try:

        result = (
            slack_connection_service
            .test_connection(
                bot_token=request.bot_token,
                channel_ids=request.channel_ids,
            )
        )

        return {
            "source": "slack",
            "status": "success",
            "details": result,
        }

    except Exception as exc:

        return {
            "source": "slack",
            "status": "error",
            "error": str(exc),
        }


@app.post(
    "/connections/slack"
)
def connect_slack(
    request: SlackConnectionRequest,
    user: AuthUser = Depends(get_current_user),
):

    reject_identity_mismatch(request.user_id, user)

    try:

        slack_connection_service\
            .test_connection(
                bot_token=request.bot_token,
                channel_ids=request.channel_ids,
            )

        connection_id = (
            connection_manager
            .create_connection(
                user_id=user.user_id,
                source="slack",
                credentials={
                    "bot_token": (
                        request.bot_token
                    ),
                    "channel_ids": (
                        request.channel_ids
                    ),
                },
            )
        )

        return {
            "connection_id": (
                connection_id
            ),
            "user_id": (
                user.user_id
            ),
            "source": "slack",
            "status": "connected",
            "channel_ids": (
                request.channel_ids
            ),
        }

    except Exception as exc:

        return {
            "source": "slack",
            "status": "error",
            "error": str(exc),
        }


# =========================================================
# Google Drive
# =========================================================

@app.post(
    "/connections/google-drive/test"
)
def test_google_drive_connection(
    request: GoogleDriveConnectionRequest,
    user: AuthUser = Depends(get_current_user),
):

    reject_identity_mismatch(request.user_id, user)

    try:

        result = (
            google_drive_connection_service
            .test_connection(
                folder_ids=request.folder_ids,
            )
        )

        return {
            "source": "google_drive",
            "status": "success",
            "details": result,
        }

    except Exception as exc:

        return {
            "source": "google_drive",
            "status": "error",
            "error": str(exc),
        }


@app.post(
    "/connections/google-drive"
)
def connect_google_drive(
    request: GoogleDriveConnectionRequest,
    user: AuthUser = Depends(get_current_user),
):

    reject_identity_mismatch(request.user_id, user)

    try:

        google_drive_connection_service\
            .test_connection(
                folder_ids=request.folder_ids,
            )

        connection_id = (
            connection_manager
            .create_connection(
                user_id=user.user_id,
                source="google_drive",
                credentials={
                    "folder_ids": (
                        request.folder_ids
                    ),
                },
            )
        )

        return {
            "connection_id": (
                connection_id
            ),
            "user_id": (
                user.user_id
            ),
            "source": "google_drive",
            "status": "connected",
            "folder_ids": (
                request.folder_ids
            ),
        }

    except Exception as exc:

        return {
            "source": "google_drive",
            "status": "error",
            "error": str(exc),
        }


# =========================================================
# List User Connections
# =========================================================

@app.get(
    "/connections/{user_id}"
)
def get_connections(
    user_id: str,
    user: AuthUser = Depends(get_current_user),
):

    require_user_or_admin(user_id, user)

    connections = (
        connection_manager
        .get_user_connections(
            user_id
        )
    )

    safe_connections = []

    for connection in connections:

        credentials = (
            connection.get(
                "credentials",
                {},
            )
        )

        safe_connection = {
            "connection_id": (
                connection[
                    "connection_id"
                ]
            ),
            "source": (
                connection[
                    "source"
                ]
            ),
            "kind": connection.get("kind", "real"),
            "status": (
                connection[
                    "status"
                ]
            ),
        }

        # ---------------------------------------------
        # Safe source-specific information
        # ---------------------------------------------

        source = (
            connection["source"]
        )

        if source == "confluence":

            safe_connection[
                "base_url"
            ] = credentials.get(
                "base_url"
            )

            safe_connection[
                "space_keys"
            ] = credentials.get(
                "space_keys",
                [],
            )

        elif source == "jira":

            safe_connection[
                "server_url"
            ] = credentials.get(
                "server_url"
            )

            safe_connection[
                "project_keys"
            ] = credentials.get(
                "project_keys",
                [],
            )

        elif source == "slack":

            safe_connection[
                "channel_ids"
            ] = credentials.get(
                "channel_ids",
                [],
            )

        elif source == "google_drive":

            safe_connection[
                "folder_ids"
            ] = credentials.get(
                "folder_ids",
                [],
            )

        safe_connections.append(
            safe_connection
        )

    return {
        "user_id": user_id,
        "connections": safe_connections,
    }




# =========================================================
# Sync Connection
# =========================================================

@app.post(
    "/connections/{connection_id}/sync"
)
def sync_connection(
    connection_id: str,
    user: AuthUser = Depends(get_current_user),
):

    connection = (
        connection_manager
        .get_connection(
            connection_id
        )
    )

    if not connection:

        return {
            "status": "error",
            "message": (
                "Connection not found."
            ),
        }

    source = (
        connection["source"]
    )

    user_id = (
        connection["user_id"]
    )

    # Enforce ownership: only the connection owner
    # (or an admin) may sync it.
    require_user_or_admin(user_id, user)

    credentials = (
        connection["credentials"]
    )

    try:

        # -------------------------------------------------
        # Mock mode: use mock loaders for the demo
        # -------------------------------------------------
        if connection.get("kind", "real") == "mock":
            _MOCK_LOADERS = {
                "confluence":   "ingestion.loader.confluence.load_mock_confluence",
                "jira":         "ingestion.loader.jira.load_mock_jira",
                "slack":        "ingestion.loader.slack.load_mock_slack",
                "google_drive": "ingestion.loader.drive.load_mock_google_drive",
            }
            if source in _MOCK_LOADERS:
                import importlib
                parts = _MOCK_LOADERS[source].rsplit(".", 1)
                mod = importlib.import_module(parts[0])
                loader_func = getattr(mod, parts[1])
                documents = loader_func()
                result = connection_sync_service._process_documents(
                    documents=documents,
                    user_id=user_id,
                    connection_id=connection_id,
                    source=source,
                    kind="mock",
                )
            else:
                return {
                    "status": "error",
                    "message": f"Unsupported source: {source}",
                }

        # -------------------------------------------------
        # Real mode: use real loaders
        # -------------------------------------------------
        elif source == "confluence":

            result = (
                connection_sync_service
                .sync_confluence(
                    user_id=user_id,
                    connection_id=(
                        connection_id
                    ),
                    credentials=credentials,
                )
            )

        # -------------------------------------------------
        # Jira
        # -------------------------------------------------

        elif source == "jira":

            result = (
                connection_sync_service
                .sync_jira(
                    user_id=user_id,
                    connection_id=(
                        connection_id
                    ),
                    credentials=credentials,
                )
            )

        # -------------------------------------------------
        # Slack
        # -------------------------------------------------

        elif source == "slack":

            result = (
                connection_sync_service
                .sync_slack(
                    user_id=user_id,
                    connection_id=(
                        connection_id
                    ),
                    credentials=credentials,
                    verified_user_email=user.email,
                )
            )

        # -------------------------------------------------
        # Google Drive
        # -------------------------------------------------

        elif source == "google_drive":

            result = (
                connection_sync_service
                .sync_google_drive(
                    user_id=user_id,
                    connection_id=(
                        connection_id
                    ),
                    credentials=credentials,
                )
            )

        else:

            return {
                "status": "error",
                "message": (
                    f"Unsupported source: "
                    f"{source}"
                ),
            }

        connection[
            "status"
        ] = "synced"

        return {
            "connection_id": (
                connection_id
            ),
            "user_id": (
                user_id
            ),
            "source": (
                source
            ),
            "status": "synced",
            "kind": connection.get("kind", "real"),
            "sync": (
                result
            ),
        }

    except Exception as exc:

        # Check for old index format
        from indexing.indexer import OldIndexFormatError

        if isinstance(exc, OldIndexFormatError):
            return {
                "connection_id": (
                    connection_id
                ),
                "user_id": (
                    user_id
                ),
                "source": (
                    source
                ),
                "status": "error",
                "message": (
                    "Index uses old format. "
                    "Please rebuild by deleting and "
                    "reconnecting the source."
                ),
                "error": str(exc),
            }

        

        print(f"\nSync failed for {source}")
        traceback.print_exc()

        return {
            "connection_id": (
                connection_id
            ),
            "source": (
                source
            ),
            "status": "error",
            "error": str(exc),
        }


# =========================================================
# RAG Query
# =========================================================

@app.post("/connections/mock/connect-all")
def connect_all_mock_sources(user: AuthUser = Depends(get_current_user)):
    """Register and synchronize four mock connections for the authenticated user.

    No caller-supplied identity or source credentials are accepted.
    Connections are reused on repeat calls; the existing authenticated
    sync endpoint enforces ownership and applies the normal ACL/indexing path.
    """
    from fastapi import HTTPException


    sources = ("confluence", "jira", "slack", "google_drive")
    current = connection_manager.get_user_connections(user.user_id)
    results = []
    for source in sources:
        existing = next((c for c in current if c.get("source") == source and c.get("kind") == "mock"), None)
        connection_id = existing["connection_id"] if existing else (
            connection_manager.create_connection(
                user_id=user.user_id, source=source, credentials={}, kind="mock"
            )
        )
        # Call the existing sync implementation, including ownership checks,
        # rather than introducing a second indexing path.
        sync_result = sync_connection(connection_id=connection_id, user=user)
        results.append({
            "source": source,
            "connection_id": connection_id,
            "status": sync_result.get("status", "error"),
            "sync": sync_result.get("sync", {}),
            "message": sync_result.get("message") or sync_result.get("error"),
        })

    failed = [
    r["source"]
    for r in results
    if r["status"] != "synced"
    ]

    return {
        "mode": "mock",
        "user_id": user.user_id,
        "status": "partial_failure" if failed else "completed",
        "failed_sources": failed,
        "connections": results,
    }
    


@app.post("/query")
def query(
    request: UserRequest,
    user: AuthUser = Depends(get_current_user),
):

    try:
        rag_pipeline = get_user_rag_pipeline(user.user_id)

        # Build user_context entirely from server-defined
        # authenticated state — never from the request body.
        user_context = user.to_user_context()

        # Enrich with Slack channel IDs from stored connections
        user_context["slack"]["member_channel_ids"] = (
            _get_user_slack_channels(user.user_id)
        )

        result = rag_pipeline.query(
            query=request.query,
            user_context=user_context,
        )

        return {
            "query":     result["query"],
            "answer":    result["answer"],
            "citations": result["citations"],
            "agentic":   result.get("agentic", {}),
            # Preserve the pipeline's recorded outcome. A routing failure
            # must never be reported to clients as a successful query.
            "status":    "processed" if result.get("status") == "success" else (
                "no_context" if result.get("status") == "no_context" else "error"
            ),
        }

    except FileNotFoundError:
        return {
            "query":     request.query,
            "answer":    (
                "I don't have information on that topic "
                "in the available sources."
            ),
            "citations": [],
            "status":    "error",
        }

    except Exception as exc:
        print("Query error:", str(exc))
        return {
            "query":     request.query,
            "answer":    "Unable to process the query.",
            "citations": [],
            "status":    "error",
            "error":     str(exc),
        }

@app.get("/audit/logs")
def get_audit_logs(
    user:        AuthUser = Depends(get_current_user),
    user_id:     Optional[str] = None,
    event_type:  Optional[str] = None,
    outcome:     Optional[str] = None,
    source:      Optional[str] = None,
    resource_id: Optional[str] = None,
    query_id:    Optional[str] = None,
    since:       Optional[str] = None,
    until:       Optional[str] = None,
    limit:       int = 100,
):
    """
    Admin-only endpoint to retrieve audit history.

    Supports filtering by:
      - user_id     (authenticated user)
      - event_type  (QUERY, PERMISSION_CHANGE, etc.)
      - outcome     (ALLOWED, DENIED, ERROR, SUCCESS)
      - source      (confluence, jira, slack, google_drive)
      - resource_id (document/node ID)
      - query_id    (groups entries for one user query)
      - since / until (ISO date range)
    """
    require_admin(user)

    from datetime import datetime as _dt

    since_dt = _dt.fromisoformat(since) if since else None
    until_dt = _dt.fromisoformat(until) if until else None

    return {
        "logs": query_audit_log(
            user_id=user_id,
            event_type=event_type,
            outcome=outcome,
            source=source,
            resource_id=resource_id,
            query_id=query_id,
            since=since_dt,
            until=until_dt,
            limit=limit,
        )
    }

@app.get("/audit/verify")
def verify_audit_integrity(
    user: AuthUser = Depends(get_current_user),
):
    require_admin(user)
    return verify_chain_integrity()

# ── Request models ─────────────────────────────────────────────────────
class RevokeResourceRequest(BaseModel):
    source:      str          # "confluence" | "jira" | "slack" | "google_drive"
    resource_id: str          # e.g. Confluence page ID, Jira issue key

class RevokeSourceRequest(BaseModel):
    source:     str


# =========================================================
# Revocation endpoints (admin/compliance only)
# =========================================================

@app.post("/revoke/resource")
def revoke_resource(
    request: RevokeResourceRequest,
    target_user_id: str,
    user: AuthUser = Depends(get_current_user),
):
    """
    Remove a specific document from a user's index.
    Admin/compliance only.
    """
    require_admin(user)
    from indexing.vector_store import index_kind
    results = []
    for kind in ("mock", "real"):
        with index_kind(kind):
            result = revocation_service.revoke_resource(
            user_id     = target_user_id,
            source      = request.source,
            resource_id = request.resource_id,
            revoked_by  = user.user_id,
        )
        results.append({"kind": kind, "result": result})
    return {"results": results}


@app.post("/revoke/source")
def revoke_source(
    request: RevokeSourceRequest,
    target_user_id: str,
    user: AuthUser = Depends(get_current_user),
):
    """
    Remove all documents from a source for a user.
    Admin/compliance only.
    """
    require_admin(user)
    from indexing.vector_store import index_kind
    results = []
    for kind in ("mock", "real"):
        with index_kind(kind):
            result = revocation_service.revoke_source(
            user_id    = target_user_id,
            source     = request.source,
            revoked_by = user.user_id,
        )
        results.append({"kind": kind, "result": result})
    return {"results": results}


@app.delete("/connections/{connection_id}")
def disconnect_source(
    connection_id: str,
    user: AuthUser = Depends(get_current_user),
):
    """
    Disconnect a source AND purge its indexed data.
    Only the connection owner or an admin may disconnect.
    """
    connection = connection_manager.get_connection(connection_id)
    if not connection:
        return {"status": "error", "message": "Connection not found."}

    require_user_or_admin(connection["user_id"], user)

    # ── Purge from index before deleting connection ──────────────────
    # Use connection-level revocation (not source-level) to prevent
    # disconnecting one connection from deleting another connection
    # to the same source.
    from indexing.vector_store import index_kind
    with index_kind(connection.get("kind", "real")):
        revocation_service.revoke_connection(
        user_id       = connection["user_id"],
        connection_id = connection_id,
        revoked_by    = user.user_id,
    )

    deleted = connection_manager.delete_connection(connection_id)
    return {"status": "disconnected", "connection_id": connection_id}

class WebhookEvent(BaseModel):
    platform:    str          # "confluence" | "jira" | "slack" | "google_drive"
    event:       str          # "page_restrictions_updated" | "issue_updated" etc.
    resource_id: str          # page ID, issue key, file ID
    user_id:     Optional[str] = None   # specific user, or None = all users

@app.post("/webhooks/permission-change")
def handle_permission_webhook(
    event: WebhookEvent,
    user: AuthUser = Depends(get_current_user),
):
    """
    Called by Confluence/Jira/Slack when a permission changes.
    Admin/compliance only.

    For the hackathon demo: call this manually via curl or
    the Streamlit admin panel to simulate live revocation.
    """
    require_admin(user)
    affected_users = []

    if event.user_id:
        # Revoke for a specific user
        result = revocation_service.revoke_resource(
            user_id     = event.user_id,
            source      = event.platform,
            resource_id = event.resource_id,
            revoked_by  = f"webhook:{event.event}:{user.user_id}",
        )
        affected_users = [event.user_id]

    else:
        # Revoke for ALL users who have this resource indexed
        connections = connection_manager._connections.values()
        for conn in connections:
            if conn["source"] == event.platform:
                result = revocation_service.revoke_resource(
                    user_id     = conn["user_id"],
                    source      = event.platform,
                    resource_id = event.resource_id,
                    revoked_by  = f"webhook:{event.event}:{user.user_id}",
                )
                affected_users.append(conn["user_id"])

    return {
        "status":         "processed",
        "platform":       event.platform,
        "resource_id":    event.resource_id,
        "affected_users": affected_users,
    }


# =========================================================
# Task 4 — Live Permission Change Handling
# =========================================================
#
# These endpoints implement the synthetic permission-change
# workflow:
#
#   1. Admin modifies source ACL metadata (mock JSON files)
#      via /permissions/simulate-acl-change.
#   2. A mock webhook trigger fires /permissions/reconcile
#      which reconciles the index with the updated ACL.
#   3. The revocation registry prevents stale scheduled syncs
#      from restoring revoked access.
#
# All endpoints require Task 1 authentication (get_current_user)
# and admin authorization (require_admin).  The user_context is
# built from the authenticated user's server-defined attributes —
# never from the request body.

class SimulateAclChangeRequest(BaseModel):
    """
    Request to modify source ACL metadata in the mock JSON files.

    The target_user_id specifies whose access to revoke/grant.
    This is resolved server-side — the admin must specify the
    target user, and the system applies the change to the mock
    ACL metadata, NOT through the admin's own identity.
    """
    source:          str   # confluence, jira, slack, google_drive
    source_id:       str   # document ID (CONF-001, JIRA-126, etc.)
    action:          str   # revoke, grant, delete
    target_user_id:  str   # whose permission to change


@app.post("/permissions/simulate-acl-change")
def simulate_acl_change(
    request: SimulateAclChangeRequest,
    user: AuthUser = Depends(get_current_user),
):
    """
    Modify source ACL metadata to simulate a real platform
    permission change.  Admin/compliance only.

    This modifies the mock JSON files — NOT the index directly.
    The index is reconciled separately via /permissions/reconcile.
    """
    require_admin(user)

    from permissions import mock_acl_store
    from auth import DEMO_USERS

    # Resolve the target user's identity from the server-defined
    # user registry.  We do NOT trust the caller to provide the
    # correct identity token — we look it up ourselves.
    target_user = DEMO_USERS.get(request.target_user_id)
    if not target_user:
        return {
            "status": "error",
            "message": f"Unknown target user: {request.target_user_id}",
        }

    # Build user_context from the target user's server-defined attributes
    target_user_context = target_user.to_user_context()

    # Get the appropriate identity token for this source
    if request.source == "confluence":
        identity = target_user_context.get("confluence_account_id", "")
    elif request.source == "jira":
        identity = target_user_context.get("email", "")
    elif request.source == "slack":
        identity = target_user_context.get("slack", {}).get("user_id", "")
    elif request.source == "google_drive":
        identity = target_user_context.get("email", "")
    else:
        return {
            "status": "error",
            "message": f"Unknown source: {request.source}",
        }

    # Apply the ACL change to the mock JSON file
    if request.action == "revoke":
        if request.source == "confluence":
            result = mock_acl_store.revoke_confluence_access(
                request.source_id, identity,
            )
        elif request.source == "jira":
            result = mock_acl_store.revoke_jira_access(
                request.source_id, identity,
            )
        elif request.source == "slack":
            result = mock_acl_store.revoke_slack_access(
                request.source_id, identity,
            )
        elif request.source == "google_drive":
            result = mock_acl_store.revoke_drive_access(
                request.source_id, identity,
            )
    elif request.action == "grant":
        if request.source == "confluence":
            result = mock_acl_store.grant_confluence_access(
                request.source_id, identity,
            )
        elif request.source == "jira":
            result = mock_acl_store.grant_jira_access(
                request.source_id, identity,
            )
        elif request.source == "slack":
            result = mock_acl_store.grant_slack_access(
                request.source_id, identity,
            )
        elif request.source == "google_drive":
            result = mock_acl_store.grant_drive_access(
                request.source_id, identity,
            )
    elif request.action == "delete":
        result = mock_acl_store.delete_mock_document(
            request.source, request.source_id,
        )
    else:
        return {
            "status": "error",
            "message": f"Unknown action: {request.action}",
        }

    # Audit log the ACL modification
    audit_logger.log(
        event_type  = "PERMISSION_CHANGE",
        user_id     = request.target_user_id,
        source      = request.source,
        resource_id = request.source_id,
        outcome     = "ALLOWED",
        detail={
            "action":        request.action,
            "simulated_by":  user.user_id,
            "identity":      identity,
            "acl_modified":  result,
        },
    )

    return {
        "status":           "acl_modified",
        "source":           request.source,
        "source_id":        request.source_id,
        "action":           request.action,
        "target_user_id":   request.target_user_id,
        "identity_used":    identity,
        "acl_change_result": result,
    }


class ReconcileRequest(BaseModel):
    """
    Trigger reconciliation after an ACL change.

    The target_user_id specifies whose index to reconcile.
    The action specifies what type of reconciliation to perform.
    """
    source:          str
    source_id:       str
    action:          str = "check"  # revoke, grant, delete, check
    target_user_id:  str
    connection_id:   str = ""


@app.post("/permissions/reconcile")
def reconcile_permission(
    request: ReconcileRequest,
    user: AuthUser = Depends(get_current_user),
):
    """
    Trigger reconciliation of a single document after a
    permission change.  Admin/compliance only.

    This is the mock webhook trigger — it detects the ACL
    change (by comparing the source ACL metadata with the
    indexed content) and reconciles accordingly.
    """
    require_admin(user)

    from auth import DEMO_USERS

    # Resolve target user identity from server registry
    target_user = DEMO_USERS.get(request.target_user_id)
    if not target_user:
        return {
            "status": "error",
            "message": f"Unknown target user: {request.target_user_id}",
        }

    # Build user_context from server-defined attributes
    user_context = target_user.to_user_context()

    # Enrich with Slack channel IDs from stored connections
    user_context["slack"]["member_channel_ids"] = (
        _get_user_slack_channels(request.target_user_id)
    )

    result = reconciliation_service.reconcile(
        user_id=request.target_user_id,
        source=request.source,
        source_id=request.source_id,
        user_context=user_context,
        action=request.action,
        connection_id=request.connection_id,
        triggered_by=f"webhook:{user.user_id}",
    )

    return result


class ReconcileAllRequest(BaseModel):
    source:          str
    target_user_id:  str
    connection_id:   str = ""


@app.post("/permissions/reconcile-all")
def reconcile_all_permissions(
    request: ReconcileAllRequest,
    user: AuthUser = Depends(get_current_user),
):
    """
    Reconcile ALL documents for a given source.
    Admin/compliance only.
    """
    require_admin(user)

    from auth import DEMO_USERS

    target_user = DEMO_USERS.get(request.target_user_id)
    if not target_user:
        return {
            "status": "error",
            "message": f"Unknown target user: {request.target_user_id}",
        }

    user_context = target_user.to_user_context()
    user_context["slack"]["member_channel_ids"] = (
        _get_user_slack_channels(request.target_user_id)
    )

    results = reconciliation_service.reconcile_all(
        user_id=request.target_user_id,
        source=request.source,
        user_context=user_context,
        connection_id=request.connection_id,
        triggered_by=f"webhook:{user.user_id}",
    )

    return {
        "status":    "completed",
        "source":    request.source,
        "user_id":   request.target_user_id,
        "reconciled": len(results),
        "results":   results,
    }


@app.get("/permissions/status")
def get_permission_status(
    user: AuthUser = Depends(get_current_user),
):
    """
    Return the current revocation registry state.
    Admin/compliance only.
    """
    require_admin(user)

    from permissions import mock_acl_store

    return {
        "revoked_documents": mock_acl_store.get_all_revoked(),
        "total_revoked":     len(mock_acl_store.get_all_revoked()),
    }


@app.get("/permissions/mock-documents")
def list_mock_documents(
    source: str,
    user: AuthUser = Depends(get_current_user),
):
    """
    List mock documents for a source (for the admin demo UI).
    Any authenticated user can view the list (read-only).
    """
    from permissions import mock_acl_store

    return {
        "source":    source,
        "documents": mock_acl_store.get_all_mock_documents(source),
    }


# =========================================================
# Task 5 — Automatic Synchronization Status
# =========================================================
#
# Endpoints to inspect freshness status and recent sync
# activity.  All require authentication (Task 1 RBAC).
# The status endpoint is available to any authenticated
# user; the trigger endpoint is admin-only.

@app.get("/sync/status")
def get_sync_status(
    user: AuthUser = Depends(get_current_user),
):
    """
    Return the current auto-sync scheduler status:
      - scheduler_running
      - last_successful_sync
      - last_change_detected
      - last_error
      - total_syncs / total_errors
      - freshness_latency_seconds
      - source_hashes (per-source file hash)
      - recent_cycles (last 10 sync cycles with add/update/delete counts)
    """
    return sync_status_tracker.snapshot()


@app.post("/sync/trigger")
def trigger_sync(
    user: AuthUser = Depends(get_current_user),
):
    """
    Manually trigger a single sync cycle (admin only).
    Useful for demonstrations where you don't want to wait
    for the 30-second interval.
    """
    require_admin(user)
    auto_sync_scheduler.sync_once()
    return {
        "status":   "completed",
        "triggered_by": user.user_id,
        "snapshot": sync_status_tracker.snapshot(),
    }