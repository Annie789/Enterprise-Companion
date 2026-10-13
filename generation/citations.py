# generation/citations.py

from typing import List, Dict
from llama_index.core.schema import NodeWithScore

def build_citations(
    nodes: List[NodeWithScore],
) -> List[Dict]:
    """
    Build citation metadata from retrieved nodes
    for display in the UI.

    Only display-safe fields are included.
    ACL metadata is never exposed to the frontend.

    Retrieval scores are NOT included to prevent
    side-channel information leakage about which
    documents exist but were filtered out.
    """

    citations = []

    for index, result in enumerate(nodes, start=1):

        node     = result.node
        metadata = node.metadata or {}

        citation = {
            "citation_id":   index,
            "source":        metadata.get("source",        "unknown"),
            "source_id":     metadata.get("source_id",    node.node_id),
            "title":         metadata.get("title",         "Untitled"),
            "url":           metadata.get("url",           ""),
            "updated_at":    metadata.get("updated_at",   ""),
            "document_type": metadata.get("document_type",""),
        }

        citations.append(citation)

    return citations
