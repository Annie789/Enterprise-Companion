# indexing/__init__.py

from indexing.indexer import (
    build_index,
    create_index,
    add_nodes_to_index,
    load_existing_index,
    index_exists,
    remove_source_from_index,    # ← add this
)

from indexing.vector_store import (
    get_index_directory,
    create_storage_context,
    load_storage_context,
)

__all__ = [
    "build_index",
    "create_index",
    "add_nodes_to_index",
    "load_existing_index",
    "index_exists",
    "remove_source_from_index",  # ← add this
    "get_index_directory",
    "create_storage_context",
    "load_storage_context",
]
