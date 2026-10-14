
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


# Independent index roots. The active root is scoped to the operation,
# never selected by a process-wide DATA_MODE setting.
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps

_INDEX_KIND = ContextVar("enterprise_index_kind", default="mock")

@contextmanager
def index_kind(kind: str):
    if kind not in ("mock", "real"):
        raise ValueError("Index kind must be mock or real")
    token = _INDEX_KIND.set(kind)
    try:
        yield
    finally:
        _INDEX_KIND.reset(token)

def scoped_index_operation(func):
    """Allow public index APIs to accept index_kind='mock'|'real'."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        kind = kwargs.pop("index_kind", None)
        if kind is None:
            return func(*args, **kwargs)
        with index_kind(kind):
            return func(*args, **kwargs)
    return wrapped

def get_index_base_directory(kind: str | None = None) -> Path:
    kind = kind or _INDEX_KIND.get()
    if kind not in ("mock", "real"):
        raise ValueError(f"Unknown index kind: {kind}")
    env_name = "MOCK_VECTOR_INDEX_DIR" if kind == "mock" else "REAL_VECTOR_INDEX_DIR"
    default = DEFAULT_MOCK_INDEX_DIR if kind == "mock" else DEFAULT_REAL_INDEX_DIR
    configured = os.getenv(env_name)
    base = Path(configured.strip()) if configured and configured.strip() else default
    if not base.is_absolute():
        base = BASE_DIR / base
    return base.resolve()


# ==========================================
# PER-USER INDEX DIRECTORY
# ==========================================

def get_index_directory(user_id: str, kind: str | None = None) -> Path:
    """
    Return the mode-specific, user-specific
    persisted index location.

    Example:
        data/index/mock/alice
        data/index/real/alice

    Does not create the directory, preserving
    compatibility with atomic index replacement.
    """
    base = get_index_base_directory(kind)
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
            f"No persisted {_INDEX_KIND.get()} index "
            f"for user '{user_id}' at: {index_dir}"
        )

    return StorageContext.from_defaults(
        persist_dir=str(index_dir)
    )
