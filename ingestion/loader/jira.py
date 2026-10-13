# ingestion/loader/jira.py

"""
Jira data loader.

Provides functions to load both mock and real Jira data.
Credentials are accepted from the UI (credentials dict) or
fall back to environment variables for testing.

ACL metadata (project key, security level, issue type) is
attached to every document for downstream permission scoping.
"""

import os
import json
import requests
from pathlib import Path

from llama_index.core import Document


def _resolve_issue_acl(
    server_url: str,
    issue_key: str,
    api_token: str,
    email: str,
) -> dict:
    """
    Calls the Jira REST API to fetch security level and issue type
    for a single issue. These drive permission filtering.

    Returns:
      - security_level_id:   str  (0 = no restriction, higher = more restricted)
      - security_level_name: str  e.g. "Internal", "Confidential"
      - issue_type:          str  e.g. "Bug", "Story", "Task"
      - status:              str  e.g. "In Progress", "Done"
      - url:                 str  browser URL for the issue
    """

    url = f"{server_url.rstrip('/')}/rest/api/3/issue/{issue_key}?fields=security,issuetype,status"

    try:
        response = requests.get(
            url,
            auth=(email, api_token),
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

    except Exception as e:
        print(f"  [ACL] Could not fetch metadata for issue {issue_key}: {e}")
        return {
            "security_level_id":   "UNKNOWN",
            "security_level_name": "unknown",
            "issue_type":          "unknown",
            "status":              "unknown",
            "url":                 f"{server_url.rstrip('/')}/browse/{issue_key}",
        }

    fields = data.get("fields")
    if not isinstance(fields, dict) or "security" not in fields:
        raise ValueError(f"Jira issue {issue_key}: security field unavailable")

    security        = fields.get("security") or {}
    security_id     = str(security.get("id", "")) if security else ""      # ← keep as STRING identifier
    security_name   = security.get("name", "none")

    return {
        "security_level_id":   security_id,            # ← string ID, not int rank
        "security_level_name": security_name,
        "issue_type":          fields.get("issuetype", {}).get("name", "unknown"),
        "status":              fields.get("status", {}).get("name", "unknown"),
        "url":                 f"{server_url.rstrip('/')}/browse/{issue_key}",
    }



def load_mock_jira():
    """
    Loads Jira documents from a local mock JSON file.

    Expected mock record format:
    {
        "id":                  "PLAT-001",
        "title":               "Fix login bug",
        "content":             "...",
        "updated_at":          "2024-01-20T08:30:00Z",
        "project_key":         "PLAT",
        "issue_key":           "PLAT-001",
        "issue_type":          "Bug",
        "status":              "In Progress",
        "url":                 "https://company.atlassian.net/browse/PLAT-001",
        "security_level_id":   "0",
        "security_level_name": "none",
        "allowed_users":       ["user@company.com"]
    }
    """

    path = Path(
        os.getenv("JIRA_MOCK_FILE", "data/mock/jira_mock.json")
    )

    if not path.exists():
        raise FileNotFoundError(f"Mock Jira file not found: {path}")

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
                    "source":        "jira",
                    "document_type": "jira_issue",

                    # ── Display ───────────────────────────────
                    "title":       record["title"],
                    "url":         record.get("url", ""),
                    "updated_at":  record["updated_at"],
                    "project_key": record.get("project_key", ""),
                    "issue_key":   record.get("issue_key", record["id"]),
                    "issue_type":  record.get("issue_type", "unknown"),
                    "status":      record.get("status", "unknown"),

                    # ── ACL ───────────────────────────────────
                    # security_level_id: "" = visible to all project members
                    # Non-empty = restricted to specific security level
                    "security_level_id":   str(record.get("security_level_id", record.get("security_level", ""))),
                    "security_level_name": record.get("security_level_name", "none"),
                    # Users explicitly allowed (from connection token scope)
                    "allowed_users": record.get("allowed_users", []),

                    # ── Ownership ─────────────────────────────
                    "user_id":      record.get("user_id", ""),
                },
            )
        )

    return documents


def load_real_jira(credentials=None):
    """
    Loads real Jira issues using the JiraReader.

    Credentials are resolved in this priority order:
      1. credentials dict  (supplied by the UI connection form)
      2. Environment variables  (used for local testing / CI)

    credentials dict shape:
    {
        "server_url":    "https://yourcompany.atlassian.net",
        "email":         "user@company.com",
        "api_token":     "ATATT3xFfGF0...",
        "project_keys":  ["PLAT", "DATA"]       # optional
    }

    ACL resolution:
      For each issue loaded, the Jira REST API is called to fetch
      its security level and issue type. These are attached as
      metadata for permission-aware retrieval.
    """

    creds = credentials or {}

    # ── Resolve credentials: UI first, env fallback ───────────────────
    server_url = creds.get("server_url") or os.getenv("JIRA_SERVER_URL")
    email      = creds.get("email")      or os.getenv("JIRA_EMAIL")
    api_token  = creds.get("api_token")  or os.getenv("JIRA_API_TOKEN")

    project_keys = creds.get("project_keys") or [
        v.strip()
        for v in os.getenv("JIRA_PROJECT_KEYS", "").split(",")
        if v.strip()
    ]

    # ── Validate ──────────────────────────────────────────────────────
    if not server_url:
        raise ValueError(
            "Jira server URL is required. "
            "Set JIRA_SERVER_URL or pass server_url in credentials."
        )

    if not email:
        raise ValueError(
            "Jira email is required. "
            "Set JIRA_EMAIL or pass email in credentials."
        )

    if not api_token:
        raise ValueError(
            "Jira API token is required. "
            "Set JIRA_API_TOKEN or pass api_token in credentials."
        )

    if not project_keys:
        raise ValueError(
            "At least one Jira project key is required. "
            "Set JIRA_PROJECT_KEYS or pass project_keys in credentials."
        )

    # ── Reader ────────────────────────────────────────────────────────
    from llama_index.readers.jira import JiraReader

    reader = JiraReader(
        email=email,
        api_token=api_token,
        server_url=server_url,
    )

    documents = []

    for project in project_keys:

        print(f"  Loading Jira project: {project}")

        try:
            project_docs = reader.load_data(
                query=f"project = {project} ORDER BY updated DESC"
            )
        except Exception as e:
            print(f"  [ERROR] Failed to load Jira project {project}: {e}")
            raise

        for doc in project_docs:
            issue_key = doc.metadata.get("issue_key") or doc.id_
            acl       = _resolve_issue_acl(
                server_url=server_url,
                issue_key=issue_key,
                api_token=api_token,
                email=email,
            )

            doc.metadata.update({
                "source_id":           issue_key,
                "source":              "jira",
                "document_type":       "jira_issue",
                "title":               doc.metadata.get("title", issue_key),
                "url":                 acl["url"],
                "updated_at":          doc.metadata.get("updated_at", ""),
                "project_key":         project,
                "issue_key":           issue_key,
                "issue_type":          acl["issue_type"],
                "status":              acl["status"],
                # ← Use string ID, not integer rank
                "security_level_id":   acl["security_level_id"],
                "security_level_name": acl["security_level_name"],
                # User who connected — they can access what their token returned
                "allowed_users":         [email],
                "connected_user_email":  email,
            })
        documents.extend(project_docs)

    return documents


def load_jira(mode: str, credentials=None):
    """
    Main entry point for the Jira loader.

    mode="mock"  → loads from local JSON file (no credentials needed)
    mode="real"  → loads from live Jira API
    """

    mode = mode.lower()

    if mode == "mock":
        return load_mock_jira()

    if mode == "real":
        return load_real_jira(credentials=credentials)

    raise ValueError(f"Unsupported DATA_MODE: '{mode}'. Use 'mock' or 'real'.")
