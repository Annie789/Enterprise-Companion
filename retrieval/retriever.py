# retriever.py — full updated file

from typing import List

from llama_index.core import VectorStoreIndex
from llama_index.core.schema import NodeWithScore

from retrieval.permission_filter import filter_by_permissions
from retrieval.reranker import rerank_nodes


class RetrievalService:
    """
    Retrieve relevant enterprise knowledge
    from the vector index.

    Pipeline:
        vector retrieval (over-fetch)
            ↓
        permission filtering (deny-by-default)
            ↓
        recency/relevance reranking
            ↓
        final context nodes

    Security note:
        We over-fetch (initial_top_k) before filtering so
        that unauthorized top-ranked documents do not
        prevent the discovery of lower-ranked authorized
        documents. After filtering, only authorized nodes
        proceed to reranking and generation.
    """

    def __init__(
        self,
        index: VectorStoreIndex,
        initial_top_k: int = 20,
        final_top_k: int = 5,
    ):

        self.index       = index
        self.initial_top_k = initial_top_k
        self.final_top_k   = final_top_k

        self.retriever = index.as_retriever(
            similarity_top_k=initial_top_k
        )

    def retrieve(
        self,
        query: str,
        user_context: dict,
    ) -> List[NodeWithScore]:
        """
        Retrieve the best nodes for a query.

        Args:
            query:
                User question.

            user_context:
                Full user context dict containing
                identity and per-source ACL info.
                Built entirely from authenticated state
                by the backend (Task 1).

        Returns:
            Permission-filtered and reranked nodes.
            No unauthorized node can appear in the
            returned list.
        """

        if not query.strip():
            raise ValueError("Query cannot be empty.")

        # ── 1. Vector retrieval (over-fetch) ────────────────────
        # Fetch more candidates than needed so that after
        # permission filtering, enough authorized nodes
        # remain for reranking.
        retrieved_nodes = self.retriever.retrieve(query)

        # ── 2. Permission filtering ───────────────────────────────
        # This is the security boundary. No unauthorized
        # node passes beyond this point.
        permitted_nodes = filter_by_permissions(
            nodes=retrieved_nodes,
            user_context=user_context,
        )

        # ── 3. Reranking ──────────────────────────────────────────
        # Only authorized nodes are reranked.
        final_nodes = rerank_nodes(
            nodes=permitted_nodes,
            top_k=self.final_top_k,
        )

        return final_nodes
