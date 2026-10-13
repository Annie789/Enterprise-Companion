# indexing/doc_identity.py
#
# Stable document and chunk identity utilities.
#
# Identity is based on:
#   user_id + connection_id + source + source_id
#
# This ensures:
#   - Repeated sync of the same document does not create
#     duplicate indexed entries.
#   - Each chunk has a stable, deterministic ID derived
#     from the parent document + chunk position.
#   - Deletion by ref_doc_id removes all chunks.

import hashlib
from typing import Tuple


def stable_doc_id(
    user_id: str,
    connection_id: str,
    source: str,
    source_id: str,
) -> str:
    """
    Compute a stable document ID from identity components.

    The format is: doc::<sha256-hex-16>
    This is used as the LlamaIndex doc_id / ref_doc_id,
    enabling delete_ref_doc to remove all chunks at once.
    """
    raw = f"{user_id}|{connection_id}|{source}|{source_id}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"doc::{h}"


def stable_chunk_id(
    doc_id: str,
    chunk_index: int,
) -> str:
    """
    Compute a stable chunk ID from the parent doc_id
    and chunk position.

    Format: <doc_id>::chunk::<index>
    """
    return f"{doc_id}::chunk::{chunk_index}"


def content_hash(text: str) -> str:
    """
    Compute a SHA-256 content hash for change detection.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def identity_components_from_metadata(
    metadata: dict,
) -> Tuple[str, str, str, str]:
    """
    Extract (user_id, connection_id, source, source_id)
    from a node's metadata.
    """
    return (
        metadata.get("user_id", ""),
        metadata.get("connection_id", ""),
        metadata.get("source", ""),
        metadata.get("source_id", ""),
    )


def doc_id_from_metadata(metadata: dict) -> str:
    """
    Compute the stable doc_id from a node's metadata.
    """
    user_id, connection_id, source, source_id = (
        identity_components_from_metadata(metadata)
    )
    return stable_doc_id(user_id, connection_id, source, source_id)
