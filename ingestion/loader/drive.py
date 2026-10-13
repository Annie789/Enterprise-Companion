# ingestion/loader/drive.py

"""
Google Drive data loader.

Provides functions to load both mock and real Google Drive data.
Credentials are accepted from the UI (credentials dict) or
fall back to environment variables for testing.

ACL metadata (accessible users, shared drive, parent folders)
is attached to every document for downstream permission scoping.
"""

import os
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

from llama_index.core import Document


def _build_drive_service(credentials: dict):
    """
    Builds a Google Drive API service client from credentials.

    Supports:
      - service_account_json_path:  path to a service account JSON file
      - service_account_json:       the JSON content as a dict (inline)

    Returns a Google Drive API v3 resource.
    """

    sa_path   = credentials.get("service_account_json_path") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")
    sa_inline = credentials.get("service_account_json")

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]

    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    if sa_path:
        creds = Credentials.from_service_account_file(sa_path, scopes=scopes)

    elif sa_inline:
        creds = Credentials.from_service_account_info(sa_inline, scopes=scopes)

    else:
        # Fall back to application default credentials (local dev / GCE)
        import google.auth
        creds, _ = google.auth.default(scopes=scopes)

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _resolve_file_acl(drive_service, file_id: str) -> dict:
    """
    Calls the Drive API to fetch the permission list for a single file.

    Returns:
      - accessible_by_users:   list[str]   email addresses with direct access
      - accessible_by_domains: list[str]   domains with access (e.g. company.com)
      - shared_drive_id:       str         empty string if not in a shared drive
      - parent_folder_ids:     list[str]   immediate parent folder IDs
      - is_public:             bool        True if anyone-with-link
      - url:                   str         browser URL
    """

    acl = {
        "accessible_by_users":   [],
        "accessible_by_domains": [],
        "shared_drive_id":       "",
        "parent_folder_ids":     [],
        "is_public":             False,
        "url":                   f"https://drive.google.com/file/d/{file_id}/view",
    }

    # ── File metadata (parents + driveId) ─────────────────────────────
    try:
        file_meta = drive_service.files().get(
            fileId=file_id,
            fields="id,name,parents,driveId,webViewLink",
            supportsAllDrives=True,
        ).execute()

        acl["shared_drive_id"]   = file_meta.get("driveId", "")
        acl["parent_folder_ids"] = file_meta.get("parents", [])
        acl["url"]               = file_meta.get("webViewLink", acl["url"])

    except Exception as e:
        raise PermissionError(f"Drive file metadata unavailable for {file_id}") from e

    # ── Permissions list ──────────────────────────────────────────────
    try:
        perms_resp = drive_service.permissions().list(
            fileId=file_id,
            fields="permissions(id,type,role,emailAddress,domain)",
            supportsAllDrives=True,
        ).execute()

        for perm in perms_resp.get("permissions", []):
            perm_type = perm.get("type")
            role      = perm.get("role", "")

            # Only care about read-or-above access
            if role not in ("reader", "commenter", "writer", "owner"):
                continue

            if perm_type == "anyone":
                acl["is_public"] = True

            elif perm_type == "domain":
                domain = perm.get("domain", "")
                if domain:
                    acl["accessible_by_domains"].append(domain)

            elif perm_type in ("user", "group"):
                email = perm.get("emailAddress", "")
                if email:
                    acl["accessible_by_users"].append(email)

    except Exception as e:
        raise PermissionError(f"Drive permissions unavailable for {file_id}") from e

    return acl


def load_mock_google_drive():
    """
    Loads Google Drive documents from a local mock JSON file.

    Expected mock record format:
    {
        "id":                    "file-001",
        "title":                 "Q4 Roadmap.pdf",
        "content":               "...",
        "updated_at":            "2024-01-10T09:00:00Z",
        "folder_id":             "folder-abc",
        "url":                   "https://drive.google.com/file/d/file-001/view",
        "accessible_by_users":   ["annie@company.com", "bob@company.com"],
        "accessible_by_domains": ["company.com"],
        "shared_drive_id":       "",
        "parent_folder_ids":     ["folder-abc"],
        "is_public":             false
    }
    """

    path = Path(
        os.getenv(
            "GOOGLE_DRIVE_MOCK_FILE",
            "data/mock/google_drive_mock.json",
        )
    )

    if not path.exists():
        raise FileNotFoundError(f"Mock Google Drive file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)

    documents = []

    for record in records:
        documents.append(
            Document(
                text=record["content"],
                metadata={
                    # ── Identity ──────────────────────────────
                    "source_id":     record["id"],
                    "source":        "google_drive",
                    "document_type": "drive_file",

                    # ── Display ───────────────────────────────
                    "title":      record["title"],
                    "url":        record.get("url", ""),
                    "updated_at": record["updated_at"],
                    "folder_id":  record.get("folder_id", ""),

                    # ── ACL ───────────────────────────────────
                    # A user sees this chunk if:
                    #   - is_public is True, OR
                    #   - their email is in accessible_by_users, OR
                    #   - their domain is in accessible_by_domains, OR
                    #   - their shared_drive_id matches
                    "accessible_by_users":   record.get("accessible_by_users", []),
                    "accessible_by_domains": record.get("accessible_by_domains", []),
                    "shared_drive_id":       record.get("shared_drive_id", ""),
                    "parent_folder_ids":     record.get("parent_folder_ids", []),
                    "is_public":             record.get("is_public", False),

                    # ── Ownership ─────────────────────────────
                    "user_id":               record.get("user_id", ""),
                },
            )
        )

    return documents


def load_real_google_drive(credentials=None):
    """
    Loads real Google Drive files using the GoogleDriveReader.

    Credentials are resolved in this priority order:
      1. credentials dict  (supplied by the UI connection form)
      2. Environment variables  (used for local testing / CI)

    credentials dict shape:
    {
        "folder_ids":                 ["folder-id-1", "folder-id-2"],
        "service_account_json_path":  "/path/to/sa.json",    # Option A
        "service_account_json":       { ... }                # Option B (inline dict)
        # If neither is provided, falls back to Application Default Credentials
    }

    ACL resolution:
      For each file loaded, the Drive API is called to fetch
      its permission list (users, domains, public access).
      This is attached as metadata for permission-aware retrieval.
    """

    creds = credentials or {}

    # ── Resolve folder IDs: UI first, env fallback ────────────────────
    folder_ids = creds.get("folder_ids") or [
        v.strip()
        for v in os.getenv("GOOGLE_DRIVE_FOLDER_IDS", "").split(",")
        if v.strip()
    ]

    if not folder_ids:
        raise ValueError(
            "At least one Google Drive folder ID is required. "
            "Set GOOGLE_DRIVE_FOLDER_IDS or pass folder_ids in credentials."
        )

    # Use the same identity for API permissions and reader ingestion.
    drive_service = _build_drive_service(creds)
    sa_path = creds.get("service_account_json_path") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")
    sa_inline = creds.get("service_account_json")

    from llama_index.readers.google import GoogleDriveReader
    if sa_inline and not sa_path:
        raise ValueError(
            "Inline service account JSON is not supported by the reader. "
            "Provide GOOGLE_SERVICE_ACCOUNT_JSON_PATH instead."
        )
    reader = (GoogleDriveReader(service_account_key_path=sa_path)
              if sa_path else GoogleDriveReader())

    documents = []

    for folder_id in folder_ids:

        print(f"  Loading Google Drive folder: {folder_id}")

        try:
            folder_docs = reader.load_data(folder_id=folder_id)
        except Exception as e:
            print(f"  [ERROR] Failed to load Drive folder {folder_id}: {e}")
            raise

        for doc in folder_docs:

            file_id = doc.id_

            # ── Fetch ACL for this file ────────────────────────────
            acl = _resolve_file_acl(drive_service, file_id)

            doc.metadata.update({
                # Identity
                "source_id":     file_id,
                "source":        "google_drive",
                "document_type": "drive_file",

                # Display
                "title":      doc.metadata.get("file name", file_id),
                "url":        acl["url"],
                "updated_at": doc.metadata.get("modified at", ""),
                "folder_id":  folder_id,

                # ACL — resolved from Drive API
                "accessible_by_users":   acl["accessible_by_users"],
                "accessible_by_domains": acl["accessible_by_domains"],
                "shared_drive_id":       acl["shared_drive_id"],
                "parent_folder_ids":     acl["parent_folder_ids"],
                "is_public":             acl["is_public"],
            })

        documents.extend(folder_docs)

    return documents


def load_google_drive(mode: str, credentials=None):
    """
    Main entry point for the Google Drive loader.

    mode="mock"  → loads from local JSON file (no credentials needed)
    mode="real"  → loads from live Google Drive API
    """

    mode = mode.lower()

    if mode == "mock":
        return load_mock_google_drive()

    if mode == "real":
        return load_real_google_drive(credentials=credentials)

    raise ValueError(f"Unsupported DATA_MODE: '{mode}'. Use 'mock' or 'real'.")
