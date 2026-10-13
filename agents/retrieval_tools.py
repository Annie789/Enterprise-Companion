# agents/retrieval_tools.py
#
# Four LlamaIndex FunctionTool wrappers around the
# existing RetrievalService. Each tool targets one
# enterprise source and reuses the shared vector index
# + permission filter — no per-tool indexes are created.
#
# Security model:
#
#   1. User identity and the full user_context dict are
#      injected by trusted application code via
#      FunctionTool(partial_params=...). The LLM/agent
#      NEVER selects these values — they are not part
#      of the tool's function signature that the model
#      sees.
#
#   2. Each tool retrieves from the *existing* index,
#      then applies a source filter so only nodes
#      whose metadata["source"] matches the tool's
#      source are kept. Permission filtering then
#      removes any unauthorized nodes from that set.
#
#   3. Only display-safe citation metadata is returned
#      (source, source_id, title, url, updated_at,
#      document_type, and content snippet). ACL fields
#      and retrieval scores are stripped to prevent
#      side-channel leakage.
#
#   4. The same generic "no results" response is
#      returned whether content was unauthorized or
#      simply didn't exist — preventing inference of
#      restricted documents.

from typing import Any, Dict, List
from threading import Lock

from llama_index.core.tools import FunctionTool
from llama_index.core.schema import NodeWithScore
from llama_index.core.vector_stores import MetadataFilters, MetadataFilter, FilterOperator

from retrieval.retriever import RetrievalService
from retrieval.permission_filter import filter_by_permissions
from retrieval.reranker import rerank_nodes
from generation.citations import build_citations


class AuthorizedEvidenceCollector:
    """Request-local, server-owned store; NEVER passed to the LLM as content.

    Tools add only permission-checked nodes. The agent sees only serialized snippets.
    """
    def __init__(self):
        self._lock = Lock()
        self._nodes = []
        self._calls = []

    def record(self, source, query, nodes):
        with self._lock:
            self._calls.append({"tool": f"search_{source}", "source": source,
                                "result_count": len(nodes)})
            self._nodes.extend(nodes)

    def snapshot(self):
        with self._lock:
            return list(self._nodes), list(self._calls)


# ── Display-safe metadata keys ──────────────────────────────
# These are the ONLY fields exposed in tool results.
# ACL/permission fields are intentionally excluded.
_SAFE_CONTENT_KEYS = {
    "source",
    "source_id",
    "title",
    "url",
    "updated_at",
    "document_type",
}

# Maximum content snippet length in tool results.
_MAX_CONTENT_SNIPPET = 500

# Generic response for no-results — identical whether
# content was unauthorized or simply absent.
_NO_RESULTS = "No authorized results found for this query."


def _filter_by_source(
    nodes: List[NodeWithScore],
    source: str,
) -> List[NodeWithScore]:
    """
    Keep only nodes whose metadata["source"] matches
    the given source. This is applied after vector
    retrieval but BEFORE permission filtering, so that
    unauthorized nodes from other sources are never
    considered.
    """
    return [
        nws for nws in nodes
        if (nws.node.metadata or {}).get(
            "source", ""
        ).lower() == source
    ]


def _build_tool_result(
    nodes: List[NodeWithScore],
) -> Dict[str, Any]:
    """
    Build a display-safe result dict from authorized nodes.

    Only citation-safe fields and a content snippet
    are included. ACL metadata and scores are stripped.
    """
    if not nodes:
        return {
            "status": "no_results",
            "message": _NO_RESULTS,
            "results": [],
        }

    citations = build_citations(nodes)

    results = []
    for citation, nws in zip(citations, nodes):
        content = nws.node.get_content()
        snippet = content[:_MAX_CONTENT_SNIPPET]
        if len(content) > _MAX_CONTENT_SNIPPET:
            snippet += "..."

        results.append({
            "citation": citation,
            "content_snippet": snippet,
        })

    return {
        "status": "ok",
        "message": f"{len(results)} authorized result(s) found.",
        "results": results,
    }


def _retrieve_for_source(
    retrieval_service: RetrievalService,
    source: str,
    query: str,
    user_context: dict,
    evidence_collector: AuthorizedEvidenceCollector = None,
) -> Dict[str, Any]:
    """
    Shared retrieval logic for all four tools.

    1. Vector retrieval (over-fetch via existing retriever)
    2. Source filtering — keep only this tool's source
    3. Permission filtering — remove unauthorized nodes
    4. Reranking — only authorized source nodes
    5. Build display-safe result

    No separate vector index is used. The existing
    RetrievalService.index is reused.
    """
    if not query or not query.strip():
        return {
            "status": "error",
            "message": "Query cannot be empty.",
            "results": [],
        }

    if not user_context or "user_id" not in user_context:
        return {
            "status": "error",
            "message": "Missing authenticated user context.",
            "results": [],
        }

    # ── 1. Source-constrained vector retrieval ──────────────
    # Apply the source constraint IN the index query rather than globally
    # selecting top-K nodes first. Otherwise a less frequent source can be
    # starved by high-scoring results from other sources.
    # Fail closed if the index/adapter cannot honor metadata filtering.
    source_retriever = retrieval_service.index.as_retriever(
        similarity_top_k=retrieval_service.initial_top_k,
        filters=MetadataFilters(filters=[
            MetadataFilter(key="source", operator=FilterOperator.EQ, value=source)
        ]),
    )
    retrieved_nodes = source_retriever.retrieve(query)

    # ── 2. Defense-in-depth source check ─────────────────────
    source_nodes = _filter_by_source(retrieved_nodes, source)

    # ── 3. Permission filtering ──────────────────────────────
    # This is the security boundary. No unauthorized node
    # from this source can pass beyond this point.
    permitted_nodes = filter_by_permissions(
        nodes=source_nodes,
        user_context=user_context,
    )

    # ── 4. Reranking (only authorized source nodes) ─────────
    final_nodes = rerank_nodes(
        nodes=permitted_nodes,
        top_k=retrieval_service.final_top_k,
    )

    # ── 5. Store authoritative evidence outside the LLM response.
    if evidence_collector is not None:
        evidence_collector.record(source, query, final_nodes)

    # ── 6. Build display-safe result ─────────────────────────
    return _build_tool_result(final_nodes)


# =========================================================
# Tool functions
#
# Each function accepts ONLY the query (model-selected)
# plus retrieval_service and user_context, which are
# injected via partial_params — never model-selected.
# =========================================================

def search_confluence(
    query: str,
    retrieval_service: RetrievalService,
    user_context: dict,
    evidence_collector: AuthorizedEvidenceCollector = None,
) -> Dict[str, Any]:
    """
    Search Confluence pages that the authenticated user
    is authorized to access.

    Only pages from the 'confluence' source and passing
    permission checks (space-level, page-level, and
    ownership binding) are returned.

    Args:
        query: Natural-language search query.
    """
    return _retrieve_for_source(
        retrieval_service=retrieval_service,
        source="confluence",
        query=query,
        user_context=user_context,
        evidence_collector=evidence_collector,
    )


def search_jira(
    query: str,
    retrieval_service: RetrievalService,
    user_context: dict,
    evidence_collector: AuthorizedEvidenceCollector = None,
) -> Dict[str, Any]:
    """
    Search Jira issues that the authenticated user is
    authorized to access.

    Only issues from the 'jira' source and passing
    permission checks (project-level access, issue-level
    security, and ownership binding) are returned.

    Args:
        query: Natural-language search query.
    """
    return _retrieve_for_source(
        retrieval_service=retrieval_service,
        source="jira",
        query=query,
        user_context=user_context,
        evidence_collector=evidence_collector,
    )


def search_slack(
    query: str,
    retrieval_service: RetrievalService,
    user_context: dict,
    evidence_collector: AuthorizedEvidenceCollector = None,
) -> Dict[str, Any]:
    """
    Search Slack messages that the authenticated user
    is authorized to access.

    Only messages from the 'slack' source and passing
    permission checks (workspace membership, channel
    membership, private-channel/DM access, and ownership
    binding) are returned.

    Args:
        query: Natural-language search query.
    """
    return _retrieve_for_source(
        retrieval_service=retrieval_service,
        source="slack",
        query=query,
        user_context=user_context,
        evidence_collector=evidence_collector,
    )


def search_google_drive(
    query: str,
    retrieval_service: RetrievalService,
    user_context: dict,
    evidence_collector: AuthorizedEvidenceCollector = None,
) -> Dict[str, Any]:
    """
    Search Google Drive files that the authenticated
    user is authorized to access.

    Only files from the 'google_drive' source and
    passing permission checks (direct user access,
    domain access, public access, verifiable inherited
    permissions, and ownership binding) are returned.

    Args:
        query: Natural-language search query.
    """
    return _retrieve_for_source(
        retrieval_service=retrieval_service,
        source="google_drive",
        query=query,
        user_context=user_context,
        evidence_collector=evidence_collector,
    )


# =========================================================
# Tool factory
#
# Creates FunctionTool instances with user_context and
# retrieval_service injected via partial_params. The
# model only sees the `query` parameter — it cannot
# select or override the authenticated user identity.
# =========================================================

def _make_tool(
    fn,
    name: str,
    description: str,
    retrieval_service: RetrievalService,
    user_context: dict,
    evidence_collector: AuthorizedEvidenceCollector = None,
) -> FunctionTool:
    """
    Create a FunctionTool with retrieval_service and
    user_context pre-bound via partial_params.

    The resulting tool's visible schema contains only
    `query` — the trusted parameters are invisible
    to the LLM.
    """
    return FunctionTool.from_defaults(
        fn=fn,
        name=name,
        description=description,
        partial_params={
            "retrieval_service": retrieval_service,
            "user_context": user_context,
            "evidence_collector": evidence_collector,
        },
    )


def create_retrieval_tools(
    retrieval_service: RetrievalService,
    user_context: dict,
    evidence_collector: AuthorizedEvidenceCollector = None,
) -> List[FunctionTool]:
    """
    Create the four agent-compatible retrieval tools.

    This function is called by trusted application code
    (e.g. the backend or agent orchestrator) AFTER user
    authentication. The retrieval_service and user_context
    are bound at creation time and cannot be overridden by
    the LLM at tool-call time.

    Args:
        retrieval_service:
            The existing RetrievalService (reuses the
            shared vector index — no new indexes).

        user_context:
            The authenticated user's permission context,
            built entirely from server-defined attributes.

    Returns:
        List of four FunctionTool instances.
    """
    tool_specs = [
        (
            search_confluence,
            "search_confluence",
            (
                "Search Confluence pages the user is "
                "authorized to access. Returns only "
                "permission-filtered results with "
                "citation metadata."
            ),
        ),
        (
            search_jira,
            "search_jira",
            (
                "Search Jira issues the user is "
                "authorized to access. Returns only "
                "permission-filtered results with "
                "citation metadata."
            ),
        ),
        (
            search_slack,
            "search_slack",
            (
                "Search Slack messages the user is "
                "authorized to access. Returns only "
                "permission-filtered results with "
                "citation metadata."
            ),
        ),
        (
            search_google_drive,
            "search_google_drive",
            (
                "Search Google Drive files the user is "
                "authorized to access. Returns only "
                "permission-filtered results with "
                "citation metadata."
            ),
        ),
    ]

    return [
        _make_tool(
            fn=fn,
            name=name,
            description=desc,
            retrieval_service=retrieval_service,
            user_context=user_context,
            evidence_collector=evidence_collector,
        )
        for fn, name, desc in tool_specs
    ]
