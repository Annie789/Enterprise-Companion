# permissions/mock_acl_store.py
#
# Task 4 — Live Permission Change Handling
#
# This module is the synthetic source-of-truth for ACL metadata.
# In a real system, permission changes come from the platform's
# webhook API (Confluence restrictions API, Jira security level
# API, Slack conversations.info, Drive permissions API).
#
# For the hackathon demo we simulate those changes by modifying
# the mock JSON files in data/mock/*.json — the same files that
# the mock loaders read at sync time.
#
# The key design decision: permission revocation is simulated by
# modifying source ACL metadata (the mock JSON), NOT by manually
# deleting data through the admin panel.  The reconciliation
# service then detects the drift between the source ACL and the
# indexed content and reconciles accordingly.

import json
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Mock file paths ──────────────────────────────────────────
_MOCK_DIR = Path("data/mock")
_MOCK_FILES = {
    "confluence":   _MOCK_DIR / "confluence_mock.json",
    "jira":         _MOCK_DIR / "jira_mock.json",
    "slack":         _MOCK_DIR / "slack_mock.json",
    "google_drive": _MOCK_DIR / "google_drive_mock.json",
}

# ── Revocation registry ──────────────────────────────────────
# A thread-safe set of (user_id, source, source_id) tuples that
# have been explicitly revoked.  This prevents a stale scheduled
# sync from restoring revoked content — even if the old mock JSON
# still lists the user as having access, the revocation registry
# blocks re-indexing until the ACL is explicitly re-granted.
#
# Entries are removed only when a permission GRANT event is
# processed (i.e., the user's access is re-verified in the
# updated ACL).

_revoked_lock = threading.Lock()
_revoked: Dict[Tuple[str, str, str], bool] = {}


def _revoke_key(user_id: str, source: str, source_id: str) -> Tuple[str, str, str]:
    return (user_id, source, source_id)


def register_revocation(user_id: str, source: str, source_id: str) -> None:
    """Mark a document as explicitly revoked for a user."""
    with _revoked_lock:
        _revoked[_revoke_key(user_id, source, source_id)] = True


def clear_revocation(user_id: str, source: str, source_id: str) -> None:
    """Clear a revocation (e.g., access re-granted and verified)."""
    with _revoked_lock:
        _revoked.pop(_revoke_key(user_id, source, source_id), None)


def is_revoked(user_id: str, source: str, source_id: str) -> bool:
    """Check if a document is currently in the revocation registry."""
    with _revoked_lock:
        return _revoked.get(_revoke_key(user_id, source, source_id), False)


def get_all_revoked() -> List[dict]:
    """Return all revoked entries as a list of dicts (for inspection)."""
    with _revoked_lock:
        return [
            {"user_id": k[0], "source": k[1], "source_id": k[2]}
            for k in _revoked
        ]


# ── Mock ACL modification functions ──────────────────────────

def _load_mock_file(source: str) -> List[dict]:
    """Load a mock JSON file for the given source."""
    path = _MOCK_FILES.get(source)
    if not path:
        raise ValueError(f"Unknown source: {source}")
    if not path.exists():
        raise FileNotFoundError(f"Mock file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_mock_file(source: str, records: List[dict]) -> None:
    """Save records back to the mock JSON file."""
    path = _MOCK_FILES.get(source)
    if not path:
        raise ValueError(f"Unknown source: {source}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def get_mock_document(source: str, source_id: str) -> Optional[dict]:
    """Get a single mock document by source and source_id."""
    records = _load_mock_file(source)
    for r in records:
        if r["id"] == source_id:
            return r
    return None


def get_all_mock_documents(source: str) -> List[dict]:
    """Get all mock documents for a source."""
    return _load_mock_file(source)


# ── ACL modification actions ────────────────────────────────
# Each action modifies the source ACL metadata in the mock JSON
# file, simulating what a real platform would do when permissions
# change.

def revoke_confluence_access(source_id: str, account_id: str) -> dict:
    """
    Simulate a Confluence permission revocation by removing the
    user's account_id from the page's restricted_to_users list
    and setting has_restrictions=True so only explicitly listed
    users/groups can access it.

    If the page has no restrictions, we add a restriction that
    excludes the target user.
    """
    records = _load_mock_file("confluence")
    modified = False
    for r in records:
        if r["id"] != source_id:
            continue
        # Remove user from restricted_to_users
        if account_id in r.get("restricted_to_users", []):
            r["restricted_to_users"] = [
                u for u in r["restricted_to_users"] if u != account_id
            ]
            modified = True
        # Remove user's groups from allowed_groups to be thorough
        # (in real Confluence, removing a user from a group is a
        # directory change, but we simulate the effect here)
        r["has_restrictions"] = True
        if not modified:
            # If the user wasn't explicitly listed, we simulate
            # revocation by adding a restriction that excludes them.
            # The page becomes restricted to only listed users.
            r["has_restrictions"] = True
            modified = True
    if modified:
        _save_mock_file("confluence", records)
    return {"source": "confluence", "source_id": source_id, "account_id": account_id, "modified": modified}


def grant_confluence_access(source_id: str, account_id: str) -> dict:
    """
    Simulate a Confluence permission grant by adding the user's
    account_id to the page's restricted_to_users list.
    """
    records = _load_mock_file("confluence")
    modified = False
    for r in records:
        if r["id"] != source_id:
            continue
        if account_id not in r.get("restricted_to_users", []):
            r.setdefault("restricted_to_users", []).append(account_id)
            r["has_restrictions"] = True
            modified = True
    if modified:
        _save_mock_file("confluence", records)
    return {"source": "confluence", "source_id": source_id, "account_id": account_id, "modified": modified}


def revoke_jira_access(source_id: str, user_email: str) -> dict:
    """
    Simulate a Jira permission revocation by removing the user's
    email from the issue's allowed_users list.
    """
    records = _load_mock_file("jira")
    modified = False
    for r in records:
        if r["id"] != source_id:
            continue
        if user_email in r.get("allowed_users", []):
            r["allowed_users"] = [u for u in r["allowed_users"] if u != user_email]
            modified = True
    if modified:
        _save_mock_file("jira", records)
    return {"source": "jira", "source_id": source_id, "user_email": user_email, "modified": modified}


def grant_jira_access(source_id: str, user_email: str) -> dict:
    """
    Simulate a Jira permission grant by adding the user's email
    to the issue's allowed_users list.
    """
    records = _load_mock_file("jira")
    modified = False
    for r in records:
        if r["id"] != source_id:
            continue
        if user_email not in r.get("allowed_users", []):
            r.setdefault("allowed_users", []).append(user_email)
            modified = True
    if modified:
        _save_mock_file("jira", records)
    return {"source": "jira", "source_id": source_id, "user_email": user_email, "modified": modified}


def revoke_slack_access(source_id: str, slack_user_id: str) -> dict:
    """
    Simulate a Slack permission revocation by removing the user's
    Slack ID from the message's member_user_ids list (for private
    channels) and/or setting is_private=True.
    """
    records = _load_mock_file("slack")
    modified = False
    for r in records:
        if r["id"] != source_id:
            continue
        r["is_private"] = True
        if slack_user_id in r.get("member_user_ids", []):
            r["member_user_ids"] = [
                u for u in r["member_user_ids"] if u != slack_user_id
            ]
            modified = True
        elif not modified:
            # Make the channel private and exclude the user
            r["is_private"] = True
            modified = True
    if modified:
        _save_mock_file("slack", records)
    return {"source": "slack", "source_id": source_id, "slack_user_id": slack_user_id, "modified": modified}


def grant_slack_access(source_id: str, slack_user_id: str) -> dict:
    """
    Simulate a Slack permission grant by adding the user's Slack
    ID to the message's member_user_ids list.
    """
    records = _load_mock_file("slack")
    modified = False
    for r in records:
        if r["id"] != source_id:
            continue
        if slack_user_id not in r.get("member_user_ids", []):
            r.setdefault("member_user_ids", []).append(slack_user_id)
            modified = True
    if modified:
        _save_mock_file("slack", records)
    return {"source": "slack", "source_id": source_id, "slack_user_id": slack_user_id, "modified": modified}


def revoke_drive_access(source_id: str, user_email: str) -> dict:
    """
    Simulate a Google Drive permission revocation by removing the
    user's email from accessible_by_users and setting is_public=False.
    """
    records = _load_mock_file("google_drive")
    modified = False
    for r in records:
        if r["id"] != source_id:
            continue
        r["is_public"] = False
        if user_email in r.get("accessible_by_users", []):
            r["accessible_by_users"] = [
                u for u in r["accessible_by_users"] if u != user_email
            ]
            modified = True
        elif not modified:
            r["is_public"] = False
            modified = True
    if modified:
        _save_mock_file("google_drive", records)
    return {"source": "google_drive", "source_id": source_id, "user_email": user_email, "modified": modified}


def grant_drive_access(source_id: str, user_email: str) -> dict:
    """
    Simulate a Google Drive permission grant by adding the user's
    email to accessible_by_users.
    """
    records = _load_mock_file("google_drive")
    modified = False
    for r in records:
        if r["id"] != source_id:
            continue
        if user_email not in r.get("accessible_by_users", []):
            r.setdefault("accessible_by_users", []).append(user_email)
            r["is_public"] = False
            modified = True
    if modified:
        _save_mock_file("google_drive", records)
    return {"source": "google_drive", "source_id": source_id, "user_email": user_email, "modified": modified}


def delete_mock_document(source: str, source_id: str) -> dict:
    """
    Simulate document deletion by removing it from the mock JSON.
    The reconciliation service will detect it's gone and remove
    it from the index.
    """
    records = _load_mock_file(source)
    original_len = len(records)
    records = [r for r in records if r["id"] != source_id]
    if len(records) < original_len:
        _save_mock_file(source, records)
    return {"source": source, "source_id": source_id, "deleted": len(records) < original_len}


def restore_mock_document(source: str, document: dict) -> dict:
    """
    Restore a previously deleted mock document (for test cleanup).
    """
    records = _load_mock_file(source)
    # Check if already present
    existing_ids = {r["id"] for r in records}
    if document["id"] not in existing_ids:
        records.append(document)
        _save_mock_file(source, records)
    return {"source": source, "source_id": document["id"], "restored": True}
