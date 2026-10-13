from datetime import datetime, timezone
from typing import List

from llama_index.core.schema import (
    NodeWithScore,
)


DEFAULT_SIMILARITY_WEIGHT = 0.8
DEFAULT_RECENCY_WEIGHT = 0.2


def _parse_datetime(
    value: str,
):
    """
    Parse ISO-style datetime values.

    Returns:
        timezone-aware datetime or None.
    """

    if not value:
        return None

    try:

        parsed = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed

    except (
        ValueError,
        TypeError,
    ):
        return None


def _calculate_recency_score(
    updated_at: str,
) -> float:
    """
    Calculate recency score between 0 and 1.

    Newer documents receive a higher score.

    This implementation uses a simple
    decay based on document age.
    """

    updated_time = (
        _parse_datetime(
            updated_at
        )
    )

    if updated_time is None:
        return 0.0

    now = datetime.now(
        timezone.utc
    )

    age_days = max(
        (
            now
            - updated_time
        ).days,
        0,
    )

    # Simple decay:
    #
    # today       -> ~1.0
    # 30 days     -> ~0.5
    # 90+ days    -> much lower
    #
    recency_score = (
        1.0
        / (
            1.0
            + age_days / 30.0
        )
    )

    return recency_score


def rerank_nodes(
    nodes: List[NodeWithScore],
    top_k: int = 5,
    similarity_weight: float = (
        DEFAULT_SIMILARITY_WEIGHT
    ),
    recency_weight: float = (
        DEFAULT_RECENCY_WEIGHT
    ),
) -> List[NodeWithScore]:
    """
    Rerank nodes using:

        similarity score
        +
        recency score

    Args:
        nodes:
            Nodes returned from vector retrieval.

        top_k:
            Final number of nodes to return.

        similarity_weight:
            Weight given to vector similarity.

        recency_weight:
            Weight given to recency.

    Returns:
        Reranked NodeWithScore list.
    """

    reranked_nodes = []

    for node_with_score in nodes:

        similarity_score = (
            node_with_score.score
            or 0.0
        )

        metadata = (
            node_with_score.node.metadata
            or {}
        )

        updated_at = metadata.get(
            "updated_at"
        )

        recency_score = (
            _calculate_recency_score(
                updated_at
            )
        )

        combined_score = (
            similarity_weight
            * similarity_score
            +
            recency_weight
            * recency_score
        )

        # Replace score with combined score
        node_with_score.score = (
            combined_score
        )

        reranked_nodes.append(
            node_with_score
        )

    reranked_nodes.sort(
        key=lambda item: (
            item.score or 0.0
        ),
        reverse=True,
    )

    return reranked_nodes[:top_k]