
# indexing/vector_store.py

import os
import re
import hashlib
from pathlib import Path

from dotenv import load_dotenv
from llama_index.core import StorageContext


# ==========================================
# PROJECT CONFIGURATION
# ==========================================

BASE_DIR = Path(__file__).resolve().parent.parent

# Load the project's .env file
load_dotenv(BASE_DIR / ".env")

DEFAULT_MOCK_INDEX_DIR = BASE_DIR / "data" / "index" / "mock"
DEFAULT_REAL_INDEX_DIR = BASE_DIR / "data" / "index" / "real"


# ==========================================
# USER DIRECTORY SAFETY
# ==========================================

def _safe_directory_name(user_id: str) -> str:
    """
    Convert user IDs into safe directory names.
    Preserve simple IDs such as alice and bob.
    Hash anything containing unsafe characters.
    """
    if re.fullmatch(r"[a-zA-Z0-9_\-]{1,64}", user_id):
        return user_id

    return hashlib.sha256(
        user_id.encode("utf-8")
    ).hexdigest()


# ==========================================
# DATA MODE
# ==========================================

def get_data_mode() -> str:
    """
    Read the active ingestion/retrieval mode.

    Supported:
        mock
        real
    """
    mode = os.getenv("DATA_MODE", "mock").strip().lower()

    if mode not in ("mock", "real"):
        raise ValueError(
            f"Invalid DATA_MODE='{mode}'. "
            "Expected 'mock' or 'real'."
        )

    return mode


# ==========================================
# INDEX BASE DIRECTORY
# ==========================================

def get_index_base_directory() -> Path:
    """
    Return the index base directory for the
    active DATA_MODE.

    Mock: data/index/mock
    Real: data/index/real

    Paths can be overridden in .env.
    """
    mode = get_data_mode()

    if mode == "mock":
        configured = os.getenv("MOCK_VECTOR_INDEX_DIR")
        default_base = DEFAULT_MOCK_INDEX_DIR
    else:
        configured = os.getenv("REAL_VECTOR_INDEX_DIR")
        default_base = DEFAULT_REAL_INDEX_DIR

    if configured and configured.strip():
        base = Path(configured.strip())

        if not base.is_absolute():
            base = BASE_DIR / base
    else:
        base = default_base

    return base.resolve()


# ==========================================
# PER-USER INDEX DIRECTORY
# ==========================================

def get_index_directory(user_id: str) -> Path:
    """
    Return the mode-specific, user-specific
    persisted index location.

    Example:
        data/index/mock/alice
        data/index/real/alice

    Does not create the directory, preserving
    compatibility with atomic index replacement.
    """
    base = get_index_base_directory()
    safe_name = _safe_directory_name(user_id)

    index_dir = (base / safe_name).resolve()

    # Prevent path traversal
    if index_dir.parent != base:
        raise ValueError(
            f"Index path escape detected "
            f"for user_id='{user_id}'"
        )

    return index_dir


# ==========================================
# STORAGE CONTEXT
# ==========================================

def create_storage_context() -> StorageContext:
    return StorageContext.from_defaults()


def load_storage_context(user_id: str) -> StorageContext:
    index_dir = get_index_directory(user_id)
    index_store_file = index_dir / "index_store.json"

    if not index_store_file.is_file():
        raise FileNotFoundError(
            f"No persisted {get_data_mode()} index "
            f"for user '{user_id}' at: {index_dir}"
        )

    return StorageContext.from_defaults(
        persist_dir=str(index_dir)
    )
