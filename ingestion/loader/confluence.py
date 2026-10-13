# ingestion/loader/confluence.py

"""
Confluence data loader.

Provides functions to load both mock and real Confluence data.
Credentials are accepted from the UI (credentials dict) or
fall back to environment variables for testing.

ACL metadata is attached to every document so that downstream
retrieval can be scoped to what the connecting user can access.
"""

import os
import json
import requests
from pathlib import Path

from llama_index.core import Document



def _resolve_page_acl(
    base_url: str,
    page_id: str,
    space_key: str,
    api_token: str,
    email: str,
) -> dict:
    """
    Read Confluence page-level view restrictions.

    Fail closed if the API request fails, if results
    are incomplete, or if permissions are unknown.

    An unrestricted page can be read by the account
    that successfully fetched it, subject to the
    app's user-scoped index.
    """

    url = (
        f"{base_url.rstrip('/')}/rest/api/content/{page_id}"
        "?expand=restrictions.read.restrictions.user,"
        "restrictions.read.restrictions.group"
    )

    try:
        response = requests.get(
            url,
            auth=(email, api_token),
            headers={"Accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()

        read = (
            data.get("restrictions", {})
                .get("read", {})
                .get("restrictions")
        )

        if not isinstance(read, dict):
            raise ValueError(
                "Confluence read restriction metadata missing"
            )

        user_data = read.get("user", {})
        group_data = read.get("group", {})

        if not isinstance(user_data, dict) or \
           not isinstance(group_data, dict):
            raise ValueError("Invalid restriction metadata")

        # Fail closed when restrictions are paginated.
        # A partial ACL must not be treated as complete.
        for collection in (user_data, group_data):
            size = collection.get("size")
            results = collection.get("results")
            if not isinstance(results, list):
                raise ValueError("Incomplete ACL response")
            if size is not None and size > len(results):
                raise ValueError("Paginated ACL requires handling")

        groups = [
            item["name"]
            for item in group_data["results"]
        ]

        users = [
            item["accountId"]
            for item in user_data["results"]
        ]

        if groups and users:
            restriction_type = "mixed"
        elif groups:
            restriction_type = "group"
        elif users:
            restriction_type = "user"
        else:
            restriction_type = "none"

        return {
            "allowed_groups": groups,
            "restricted_to_users": users,
            "restriction_type": restriction_type,
            "has_restrictions": bool(groups or users),
        }

    except Exception as exc:
        print(
            f"[ACL] Could not verify page {page_id}: "
            f"{type(exc).__name__}: {exc}"
        )

        return {
            "allowed_groups": [],
            "restricted_to_users": [],
            "restriction_type": "inherited",
            "has_restrictions": True,
        }


def _resolve_account_id(base_url: str, email: str, api_token: str) -> str:
    """
    Resolves a user email to their Atlassian account ID.
    Used so permission_filter.py compares accountId → accountId,
    not email → accountId (which always fails).
    """
    if not email:
        return ""

    url = f"{base_url.rstrip('/')}/rest/api/3/user/search?query={email}"

    try:
        response = requests.get(
            url,
            auth=(email, api_token),
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()

        if results:
            return results[0].get("accountId", "")

    except Exception as e:
        print(f"  [ACL] Could not resolve account ID for {email}: {e}")

    return ""


def load_mock_confluence():
    """
    Loads Confluence documents from a local mock JSON file.

    Expected mock record format:
    {
        "id":                  "page-001",
        "title":               "Deployment Guide",
        "content":             "...",
        "updated_at":          "2024-01-15T10:00:00Z",
        "space_key":           "ENG",
        "url":                 "https://company.atlassian.net/wiki/...",
        "allowed_groups":      ["engineering", "devops"],
        "restricted_to_users": [],
        "restriction_type":    "group",
        "has_restrictions":    true
    }
    """

    mock_file = os.getenv(
        "CONFLUENCE_MOCK_FILE",
        "data/mock/confluence_mock.json",
    )

    path = Path(mock_file)

    if not path.exists():
        raise FileNotFoundError(
            f"Mock Confluence file not found: {path}"
        )

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
                    "source":        "confluence",
                    "document_type": "confluence_page",

                    # ── Display ───────────────────────────────
                    "title":      record["title"],
                    "url":        record.get("url", ""),
                    "updated_at": record["updated_at"],
                    "space_key":  record.get("space_key", ""),

                    # ── ACL ───────────────────────────────────
                    # These fields drive query-time permission scoping.
                    # A user sees this chunk if:
                    #   - has_restrictions is False (space-level default), OR
                    #   - their group is in allowed_groups, OR
                    #   - their account_id is in restricted_to_users
                    "allowed_groups":      record.get("allowed_groups", []),
                    "restricted_to_users": record.get("restricted_to_users", []),
                    "restriction_type":    record.get("restriction_type", "inherited"),
                    "has_restrictions":    record.get("has_restrictions", False),

                    # ── Ownership ─────────────────────────────
                    # Binds the indexed content to the user who
                    # owns the connection — prevents cross-user
                    # index leakage.
                    "user_id":             record.get("user_id", ""),
                },
            )
        )

    return documents


def load_real_confluence(credentials=None):
    """
    Loads real Confluence pages using the ConfluenceReader.

    Credentials are resolved in this priority order:
      1. credentials dict  (supplied by the UI connection form)
      2. Environment variables  (used for local testing / CI)

    credentials dict shape:
    {
        "base_url":   "https://yourcompany.atlassian.net/wiki",
        "email":      "user@company.com",
        "api_token":  "ATATT3xFfGF0...",
        "space_keys": ["ENG", "PLATFORM"]       # optional
    }

    ACL resolution:
      For each page loaded, the Confluence REST API is called to fetch
      its read restrictions (groups + users). This is attached as
      metadata so the retrieval layer can scope results to the user
      who connected.
    """

    creds = credentials or {}

    # ── Resolve credentials: UI first, env fallback ───────────────────
    base_url  = creds.get("base_url")  or os.getenv("CONFLUENCE_BASE_URL")
    api_token = creds.get("api_token") or os.getenv("CONFLUENCE_API_TOKEN")
    email     = creds.get("email")     or os.getenv("CONFLUENCE_EMAIL", "")

    space_keys = creds.get("space_keys") or [
        s.strip()
        for s in os.getenv("CONFLUENCE_SPACE_KEYS", "").split(",")
        if s.strip()
    ]

    include_attachments = (
        os.getenv("CONFLUENCE_INCLUDE_ATTACHMENTS", "false").lower() == "true"
    )

    # ── Validate ──────────────────────────────────────────────────────
    if not base_url:
        raise ValueError(
            "Confluence base URL is required. "
            "Set CONFLUENCE_BASE_URL or pass base_url in credentials."
        )

    if not api_token:
        raise ValueError(
            "Confluence API token is required. "
            "Set CONFLUENCE_API_TOKEN or pass api_token in credentials."
        )

    if not space_keys:
        raise ValueError(
            "At least one Confluence space key is required. "
            "Set CONFLUENCE_SPACE_KEYS or pass space_keys in credentials."
        )

    # ── Reader ────────────────────────────────────────────────────────
    from llama_index.readers.confluence import ConfluenceReader

    reader = ConfluenceReader(
        base_url=base_url.rstrip("/"),
        user_name=email,
        password=api_token,
        cloud=True,
        client_args={"backoff_and_retry": True},
    )

    documents = []

    for space_key in space_keys:

        print(f"  Loading Confluence space: {space_key}")

        try:
            space_docs = reader.load_data(
                space_key=space_key,
                page_status="current",
                include_attachments=include_attachments,
                max_num_results=100,
            )
        except Exception as e:
            import traceback
            print(f"[ERROR] Failed to load Confluence space: {space_key}")
            traceback.print_exc()
            raise

        for doc in space_docs:

            # page_id is stored in doc.id_ by ConfluenceReader
            page_id = doc.id_

            # ── Fetch ACL for this page ────────────────────────────
            acl = _resolve_page_acl(
                base_url=base_url,
                page_id=page_id,
                space_key=space_key,
                api_token=api_token,
                email=email,
            )

            # ── Build URL ──────────────────────────────────────────
            page_url = (
                f"{base_url.rstrip('/')}/pages/{page_id}"
            )

            # ── Attach all metadata ────────────────────────────────
            doc.metadata.update({
                # Identity
                "source_id":     page_id,
                "source":        "confluence",
                "document_type": "confluence_page",

                # Display
                "title":      doc.metadata.get("title", ""),
                "url":        page_url,
                "updated_at": doc.metadata.get("last_updated", ""),
                "space_key":  space_key,

                # ACL — resolved from Confluence REST API
                "allowed_groups":      acl["allowed_groups"],
                "restricted_to_users": acl["restricted_to_users"],
                "restriction_type":    acl["restriction_type"],
                "has_restrictions":    acl["has_restrictions"],

                # Connecting user's email — scopes retrieval to them
                "connected_user_email": email,
            })

        documents.extend(space_docs)

    return documents


def load_confluence(mode: str, credentials=None):
    """
    Main entry point for the Confluence loader.

    mode="mock"  → loads from local JSON file (no credentials needed)
    mode="real"  → loads from live Confluence API
    """

    mode = mode.lower()

    if mode == "mock":
        return load_mock_confluence()

    if mode == "real":
        return load_real_confluence(credentials=credentials)

    raise ValueError(f"Unsupported DATA_MODE: '{mode}'. Use 'mock' or 'real'.")
