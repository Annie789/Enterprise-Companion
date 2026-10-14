
# indexing/indexer.py
#
# Enterprise RAG - Reliable Vector Index Management
#
# Features:
# - Per-user isolated vector indexes
# - Stable document and chunk identities
# - Content-hash based change detection
# - Idempotent incremental synchronization
# - Added/removed chunk detection
# - Document, connection, and source deletion
# - Windows-friendly index persistence
# - Backup and rollback on persistence failures
# - Per-user locking
# - Backward compatibility detection
#
# Uses the existing:
#   indexing/vector_store.py
#   indexing/doc_identity.py
#   indexing/index_lock.py

import json
import os
import shutil
import tempfile
import time
import uuid

from pathlib import Path
from typing import List, Dict, Set, Optional

from llama_index.core import (
    VectorStoreIndex,
    load_index_from_storage,
)
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.schema import (
    BaseNode,
    NodeRelationship,
    RelatedNodeInfo,
)

from indexing.vector_store import (
    create_storage_context,
    get_index_directory,
    load_storage_context,
    scoped_index_operation,
)

from indexing.doc_identity import (
    stable_doc_id,
    stable_chunk_id,
    content_hash,
    doc_id_from_metadata,
)

from indexing.index_lock import index_lock_manager


# =========================================================
# INDEX FORMAT
# =========================================================

INDEX_FORMAT_VERSION = 2


class OldIndexFormatError(Exception):
    """
    Raised when an existing index uses an unsupported
    format and needs rebuilding.
    """

    def __init__(self, user_id: str):
        self.user_id = user_id

        super().__init__(
            f"Index for user '{user_id}' uses an old format. "
            "Rebuild required: sync all connections again."
        )


# =========================================================
# INDEX EXISTENCE / METADATA
# =========================================================

@scoped_index_operation
def index_exists(user_id: str) -> bool:
    index_dir = get_index_directory(user_id)

    return (index_dir / "index_store.json").is_file()


def _is_old_format(user_id: str) -> bool:
    index_dir = get_index_directory(user_id)

    meta_file = index_dir / "__index_meta.json"

    if not meta_file.exists():
        return True

    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            meta = json.load(f)

        return (
            meta.get("version", 0)
            < INDEX_FORMAT_VERSION
        )

    except Exception:
        return True


def _write_index_meta_to_dir(dir_path: str):
    """
    Write format information to the index directory.
    """

    meta = {
        "version": INDEX_FORMAT_VERSION,
        "description": (
            "Stable node IDs + content_hash + ref_doc_id"
        ),
    }

    meta_file = Path(dir_path) / "__index_meta.json"

    with open(
        meta_file,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(meta, f, indent=2)


def _write_index_meta(user_id: str):
    """
    Retained for compatibility with older callers.
    """

    index_dir = get_index_directory(user_id)
    index_dir.mkdir(parents=True, exist_ok=True)

    _write_index_meta_to_dir(str(index_dir))


# =========================================================
# SAFE WINDOWS INDEX PERSISTENCE
# =========================================================

def _safe_persist(index, user_id: str):
    """
    Persist the index using a temporary directory.

    Flow:
        1. Persist to temporary directory
        2. Validate temporary index files
        3. Move previous index to backup
        4. Promote temporary index
        5. Remove backup after success

    On promotion failure, attempt to restore the
    previous index.

    Uses the existing reentrant per-user index lock.
    """

    with index_lock_manager.acquire(user_id):

        index_dir = get_index_directory(user_id)

        parent = index_dir.parent
        parent.mkdir(parents=True, exist_ok=True)

        tmp_dir = Path(
            tempfile.mkdtemp(
                prefix=f".tmp_index_{index_dir.name}_",
                dir=str(parent),
            )
        )

        backup_dir = parent / (
            f".bak_index_{index_dir.name}_"
            f"{uuid.uuid4().hex[:8]}"
        )

        old_moved = False
        promoted = False

        try:
            # STEP 1: Persist to temporary directory
            index.storage_context.persist(
                persist_dir=str(tmp_dir)
            )

            _write_index_meta_to_dir(str(tmp_dir))

            # STEP 2: Validate persisted index
            required_files = [
                "index_store.json",
                "docstore.json",
            ]

            for filename in required_files:
                if not (tmp_dir / filename).is_file():
                    raise RuntimeError(
                        "Temporary index validation failed: "
                        f"missing {filename}"
                    )

            # STEP 3: Move current index to backup
            if index_dir.exists():
                os.rename(
                    str(index_dir),
                    str(backup_dir),
                )
                old_moved = True

            # STEP 4: Promote temporary index
            last_error = None

            for attempt in range(5):

                try:
                    if index_dir.exists():
                        raise RuntimeError(
                            "Index destination recreated "
                            "during persistence. Possible "
                            "concurrent writer."
                        )

                    os.rename(
                        str(tmp_dir),
                        str(index_dir),
                    )

                    promoted = True
                    break

                except PermissionError as exc:
                    last_error = exc

                    if attempt < 4:
                        time.sleep(
                            0.2 * (attempt + 1)
                        )

            if not promoted:
                raise RuntimeError(
                    f"Index promotion failed for {user_id}"
                ) from last_error

            print(
                f"  [OK] Index persisted: {index_dir}"
            )

        except Exception:

            # STEP 5: Restore previous index if necessary
            if old_moved and not promoted:

                try:
                    if not index_dir.exists():
                        os.rename(
                            str(backup_dir),
                            str(index_dir),
                        )
                        old_moved = False

                        print(
                            "  [ROLLBACK] Previous index restored."
                        )
                    else:
                        print(
                            "  [ERROR] Index destination exists. "
                            f"Backup retained: {backup_dir}"
                        )

                except OSError as rollback_error:
                    print(
                        "  [ERROR] Rollback failed: "
                        f"{rollback_error}. "
                        f"Backup retained: {backup_dir}"
                    )

            raise

        finally:

            # Remove unused temporary directory
            if tmp_dir.exists():
                shutil.rmtree(
                    tmp_dir,
                    ignore_errors=True,
                )

            # Clean old backup only after successful promotion
            if (
                promoted
                and old_moved
                and backup_dir.exists()
            ):
                try:
                    shutil.rmtree(backup_dir)

                except OSError as exc:
                    print(
                        "  [WARN] Backup cleanup failed: "
                        f"{exc}"
                    )


# =========================================================
# STABLE DOCUMENT AND CHUNK IDENTITIES
# =========================================================

def _stamp_stable_ids(nodes: List[BaseNode]):
    """
    Assign stable document and chunk identities.

    Document:
        user_id + connection_id + source + source_id

    Chunk:
        document ID + chunk_index

    Also records a content hash.

    Note: this hash tracks text changes. Permission
    reconciliation must still independently detect ACL
    changes and enforce current access at retrieval.
    """

    for node in nodes:

        metadata = node.metadata or {}
        node.metadata = metadata

        doc_id = doc_id_from_metadata(metadata)

        node.relationships[
            NodeRelationship.SOURCE
        ] = RelatedNodeInfo(
            node_id=doc_id
        )

        chunk_index = metadata.get(
            "chunk_index",
            0,
        )

        node.id_ = stable_chunk_id(
            doc_id,
            chunk_index,
        )

        text = node.get_content()

        node.metadata["content_hash"] = (
            content_hash(text)
        )


# =========================================================
# EXISTING INDEX INFORMATION
# =========================================================

def _get_existing_content_hashes(
    index: VectorStoreIndex,
) -> Dict[str, str]:
    """
    Return node_id -> content_hash.
    """

    hashes = {}

    for node_id, node in index.docstore.docs.items():

        metadata = getattr(
            node,
            "metadata",
            {},
        ) or {}

        hash_value = metadata.get(
            "content_hash",
            "",
        )

        if hash_value:
            hashes[node_id] = hash_value

    return hashes


def _get_existing_doc_ids(
    index: VectorStoreIndex,
) -> Set[str]:
    """
    Return indexed ref_doc_ids.
    """

    return set(index.ref_doc_info.keys())


def _group_nodes_by_document(
    nodes: List[BaseNode],
) -> Dict[str, List[BaseNode]]:
    """
    Group incoming chunks by their source document.
    """

    grouped = {}

    for node in nodes:

        doc_id = node.ref_doc_id

        if not doc_id:
            raise ValueError(
                "Node missing ref_doc_id"
            )

        grouped.setdefault(
            doc_id,
            [],
        ).append(node)

    return grouped


def _document_unchanged(
    doc_id: str,
    new_nodes: List[BaseNode],
    index: VectorStoreIndex,
    existing_hashes: Dict[str, str],
) -> bool:
    """
    A document is unchanged only when:

    1. Existing and incoming chunk IDs match.
    2. Every chunk content hash matches.

    This detects document updates and changes in
    the number of chunks.
    """

    ref_info = index.ref_doc_info.get(doc_id)

    if ref_info is None:
        return False

    existing_node_ids = set(
        ref_info.node_ids
    )

    incoming_node_ids = {
        node.node_id
        for node in new_nodes
    }

    if len(incoming_node_ids) != len(new_nodes):
        raise ValueError(
            f"Duplicate chunk IDs for document {doc_id}"
        )

    if existing_node_ids != incoming_node_ids:
        return False

    for node in new_nodes:

        old_hash = existing_hashes.get(
            node.node_id
        )

        new_hash = node.metadata.get(
            "content_hash"
        )

        if (
            not old_hash
            or not new_hash
            or old_hash != new_hash
        ):
            return False

    return True


# =========================================================
# CREATE INDEX
# =========================================================

@scoped_index_operation
def create_index(
    nodes: List[BaseNode],
    embed_model: BaseEmbedding,
    user_id: str,
) -> VectorStoreIndex:
    """
    Create a new user index from supplied nodes.

    Existing callers retain the same API.
    """

    if not nodes:
        raise ValueError(
            "Cannot create vector index: no nodes provided."
        )

    with index_lock_manager.acquire(user_id):

        print(
            f"\nCreating index for user: "
            f"{user_id} ({len(nodes)} nodes)"
        )

        _print_source_breakdown(nodes)

        _stamp_stable_ids(nodes)

        storage_context = create_storage_context()

        index = VectorStoreIndex(
            nodes=nodes,
            embed_model=embed_model,
            storage_context=storage_context,
            show_progress=False,
        )

        _safe_persist(index, user_id)

        print(
            "  Index saved -> "
            f"{get_index_directory(user_id)}"
        )

        return index


# =========================================================
# LOAD EXISTING INDEX
# =========================================================

@scoped_index_operation
def load_existing_index(
    embed_model: BaseEmbedding,
    user_id: str,
) -> VectorStoreIndex:
    """
    Load a persisted user index.
    """

    with index_lock_manager.acquire(user_id):

        if not index_exists(user_id):
            raise FileNotFoundError(
                f"No index found for user: {user_id}. "
                "Connect a source first."
            )

        if _is_old_format(user_id):
            raise OldIndexFormatError(user_id)

        storage_context = load_storage_context(
            user_id
        )

        return load_index_from_storage(
            storage_context=storage_context,
            embed_model=embed_model,
        )


# =========================================================
# INCREMENTAL INDEX UPDATE
# =========================================================

@scoped_index_operation
def add_nodes_to_index(
    nodes: List[BaseNode],
    embed_model: BaseEmbedding,
    user_id: str,
) -> VectorStoreIndex:
    """
    Incrementally add or update documents.

    - New document: insert
    - Changed document: replace all chunks
    - Unchanged document: skip
    - No changes: do not persist

    Existing source documents not included in the
    incoming batch are preserved. Source deletions
    are handled by the explicit removal functions.
    """

    if not nodes:
        raise ValueError(
            "Cannot update index: no nodes provided."
        )

    with index_lock_manager.acquire(user_id):

        if (
            index_exists(user_id)
            and _is_old_format(user_id)
        ):
            raise OldIndexFormatError(user_id)

        if not index_exists(user_id):
            return create_index(
                nodes=nodes,
                embed_model=embed_model,
                user_id=user_id,
            )

        print(
            f"\nUpdating index for user: {user_id}"
        )

        _print_source_breakdown(nodes)

        _stamp_stable_ids(nodes)

        index = load_existing_index(
            embed_model=embed_model,
            user_id=user_id,
        )

        existing_hashes = (
            _get_existing_content_hashes(index)
        )

        existing_doc_ids = (
            _get_existing_doc_ids(index)
        )

        grouped = _group_nodes_by_document(nodes)

        inserted = 0
        replaced = 0
        skipped = 0

        # Process each source document as a unit
        for doc_id, doc_nodes in grouped.items():

            if doc_id in existing_doc_ids:

                if _document_unchanged(
                    doc_id=doc_id,
                    new_nodes=doc_nodes,
                    index=index,
                    existing_hashes=existing_hashes,
                ):
                    skipped += len(doc_nodes)
                    continue

                # Changed document
                # Remove all old chunks before inserting new ones
                try:
                    index.delete_ref_doc(
                        doc_id,
                        delete_from_docstore=True,
                    )

                except Exception as exc:
                    raise RuntimeError(
                        "Failed to delete old version "
                        f"of document {doc_id}"
                    ) from exc

                index.insert_nodes(doc_nodes)

                replaced += len(doc_nodes)
                inserted += len(doc_nodes)

            else:

                # Completely new document
                index.insert_nodes(doc_nodes)
                inserted += len(doc_nodes)

        print(
            f"  Inserted: {inserted}, "
            f"Replaced: {replaced}, "
            f"Skipped (unchanged): {skipped}"
        )

        # IMPORTANT WINDOWS FIX:
        # Do not rewrite the directory if nothing changed.
        if inserted == 0 and replaced == 0:

            print(
                "  [SKIP] No index changes; "
                "persistence not required."
            )

            return index

        # Persist only modified indexes
        _safe_persist(index, user_id)

        print(
            "  [OK] Index changes persisted successfully."
        )

        return index


# =========================================================
# REF DOCUMENT VERIFICATION
# =========================================================

def _register_ref_doc_info(
    index: VectorStoreIndex,
    nodes: List[BaseNode],
):
    """
    Compatibility helper.

    Standard LlamaIndex insertion is responsible for
    maintaining the docstore's reference-document data.

    Verify the references rather than manually creating
    entries in index.ref_doc_info.
    """

    ref_docs = index.ref_doc_info

    for node in nodes:

        doc_id = node.ref_doc_id

        if not doc_id:
            raise ValueError(
                "Node missing ref_doc_id"
            )

        ref_info = ref_docs.get(doc_id)

        if (
            ref_info is None
            or node.node_id not in ref_info.node_ids
        ):
            raise RuntimeError(
                "Missing indexed document reference: "
                f"{doc_id} / {node.node_id}"
            )


# =========================================================
# DELETE SINGLE DOCUMENT
# =========================================================

def _delete_doc_id(
    index: VectorStoreIndex,
    doc_id: str,
    user_id: str,
    persist: bool = True,
) -> int:
    """
    Delete one document and all associated chunks.

    Returns number of removed chunks.

    Raises on deletion failure rather than reporting
    a successful permission revocation.
    """

    ref_info = index.ref_doc_info.get(doc_id)

    if not ref_info:
        return 0

    node_ids = list(ref_info.node_ids)

    if not node_ids:
        return 0

    try:
        index.delete_ref_doc(
            doc_id,
            delete_from_docstore=True,
        )

    except Exception as exc:
        raise RuntimeError(
            f"Failed to delete document {doc_id}"
        ) from exc

    removed = len(node_ids)

    if persist and removed > 0:
        _safe_persist(index, user_id)

    return removed


# =========================================================
# REMOVE DOCUMENTS BY SOURCE / CONNECTION
# =========================================================

@scoped_index_operation
def remove_source_from_index(
    user_id: str,
    embed_model: BaseEmbedding,
    source: str,
) -> int:

    return _remove_from_index(
        user_id=user_id,
        embed_model=embed_model,
        source=source,
        connection_id=None,
        doc_id=None,
    )


@scoped_index_operation
def remove_connection_from_index(
    user_id: str,
    embed_model: BaseEmbedding,
    connection_id: str,
) -> int:

    return _remove_from_index(
        user_id=user_id,
        embed_model=embed_model,
        source=None,
        connection_id=connection_id,
        doc_id=None,
    )


@scoped_index_operation
def remove_document_from_index(
    user_id: str,
    embed_model: BaseEmbedding,
    source: str,
    source_id: str,
    connection_id: str = "",
) -> int:

    doc_id = stable_doc_id(
        user_id,
        connection_id,
        source,
        source_id,
    )

    return _remove_from_index(
        user_id=user_id,
        embed_model=embed_model,
        source=None,
        connection_id=None,
        doc_id=doc_id,
    )


# =========================================================
# SHARED REMOVAL IMPLEMENTATION
# =========================================================

def _remove_from_index(
    user_id: str,
    embed_model: BaseEmbedding,
    source: Optional[str] = None,
    connection_id: Optional[str] = None,
    doc_id: Optional[str] = None,
) -> int:

    with index_lock_manager.acquire(user_id):

        if not index_exists(user_id):
            print(
                f"  No index for user '{user_id}'."
            )
            return 0

        if _is_old_format(user_id):

            if doc_id:
                raise OldIndexFormatError(user_id)

            return _remove_from_index_old(
                user_id=user_id,
                embed_model=embed_model,
                source=source,
                connection_id=connection_id,
            )

        index = load_existing_index(
            embed_model=embed_model,
            user_id=user_id,
        )

        # Single document
        if doc_id:
            return _delete_doc_id(
                index=index,
                doc_id=doc_id,
                user_id=user_id,
                persist=True,
            )

        matching_doc_ids = []

        for ref_doc_id, ref_info in (
            index.ref_doc_info.items()
        ):

            metadata = ref_info.metadata or {}

            if (
                source
                and metadata.get("source") != source
            ):
                continue

            if (
                connection_id
                and metadata.get("connection_id")
                != connection_id
            ):
                continue

            matching_doc_ids.append(
                ref_doc_id
            )

        if not matching_doc_ids:

            print(
                "  No matching documents found."
            )

            return 0

        print(
            f"  Removing {len(matching_doc_ids)} documents..."
        )

        removed_documents = 0

        for did in matching_doc_ids:

            removed_nodes = _delete_doc_id(
                index=index,
                doc_id=did,
                user_id=user_id,
                persist=False,
            )

            if removed_nodes > 0:
                removed_documents += 1

        # Persist once after batch deletion
        if removed_documents > 0:

            _safe_persist(index, user_id)

            print(
                f"  Removed {removed_documents} "
                "documents. Index saved."
            )

        return removed_documents


# =========================================================
# OLD INDEX FORMAT REMOVAL
# =========================================================

def _remove_from_index_old(
    user_id: str,
    embed_model: BaseEmbedding,
    source: Optional[str],
    connection_id: Optional[str],
) -> int:
    """
    Fallback for old-format indexes.

    Uses metadata matching and node-level deletion.
    """

    with index_lock_manager.acquire(user_id):

        storage_context = load_storage_context(
            user_id
        )

        index = load_index_from_storage(
            storage_context=storage_context,
            embed_model=embed_model,
        )

        node_ids_to_remove = []

        for node_id, node in (
            index.docstore.docs.items()
        ):

            metadata = getattr(
                node,
                "metadata",
                {},
            ) or {}

            if (
                source
                and metadata.get("source") != source
            ):
                continue

            if (
                connection_id
                and metadata.get("connection_id")
                != connection_id
            ):
                continue

            node_ids_to_remove.append(
                node_id
            )

        if not node_ids_to_remove:
            return 0

        # A failed vector deletion must not be hidden.
        try:
            index.delete_nodes(
                node_ids_to_remove,
                delete_from_docstore=True,
            )

        except Exception as exc:
            raise RuntimeError(
                "Old-format index deletion failed. "
                "A rebuild may be required."
            ) from exc

        _safe_persist(index, user_id)

        return len(node_ids_to_remove)


# =========================================================
# REMOVE ALL USER DOCUMENTS
# =========================================================

@scoped_index_operation
def remove_all_user_documents(
    user_id: str,
    embed_model: BaseEmbedding,
) -> int:
    """
    Remove every indexed document for a user.

    Return number of removed documents.
    """

    with index_lock_manager.acquire(user_id):

        if not index_exists(user_id):
            return 0

        index = load_existing_index(
            embed_model=embed_model,
            user_id=user_id,
        )

        all_doc_ids = list(
            index.ref_doc_info.keys()
        )

        removed = 0

        for doc_id in all_doc_ids:

            removed_nodes = _delete_doc_id(
                index=index,
                doc_id=doc_id,
                user_id=user_id,
                persist=False,
            )

            if removed_nodes > 0:
                removed += 1

        if removed > 0:
            _safe_persist(index, user_id)

        print(
            f"  Removed {removed} documents "
            f"for user {user_id}"
        )

        return removed


# =========================================================
# PUBLIC ENTRY POINT
# =========================================================

@scoped_index_operation
def build_index(
    nodes: List[BaseNode],
    embed_model: BaseEmbedding,
    user_id: str,
) -> VectorStoreIndex:
    """
    Main entry point used by ingestion and sync services.
    """

    return add_nodes_to_index(
        nodes=nodes,
        embed_model=embed_model,
        user_id=user_id,
    )


# =========================================================
# FORCE REBUILD
# =========================================================

@scoped_index_operation
def rebuild_index(
    nodes: List[BaseNode],
    embed_model: BaseEmbedding,
    user_id: str,
) -> VectorStoreIndex:
    """
    Build a completely fresh index.

    Do not delete the previous index before the
    new one has been constructed successfully.
    """

    if not nodes:
        raise ValueError(
            "Cannot rebuild index with zero nodes."
        )

    with index_lock_manager.acquire(user_id):

        print(
            f"\nRebuilding index for user: {user_id}"
        )

        _print_source_breakdown(nodes)

        _stamp_stable_ids(nodes)

        storage_context = create_storage_context()

        new_index = VectorStoreIndex(
            nodes=nodes,
            embed_model=embed_model,
            storage_context=storage_context,
            show_progress=False,
        )

        # Publish only after building new index
        _safe_persist(
            new_index,
            user_id,
        )

        print(
            "  [OK] Index rebuilt successfully."
        )

        return new_index


# =========================================================
# LOGGING UTILITIES
# =========================================================

def _print_source_breakdown(
    nodes: List[BaseNode],
) -> None:

    counts: Dict[str, int] = {}

    for node in nodes:

        source = (
            node.metadata or {}
        ).get(
            "source",
            "unknown",
        )

        counts[source] = (
            counts.get(source, 0) + 1
        )

    for source, count in counts.items():

        print(
            f"  {source}: {count} nodes"
        )
