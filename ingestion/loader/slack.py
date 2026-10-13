# ingestion/loader/slack.py

"""
Slack data loader.

Provides functions to load both mock and real Slack data.
Credentials are accepted from the UI (credentials dict) or
fall back to environment variables for testing.

ACL metadata (channel_id, is_private, member list) is attached
to every document so retrieval can be scoped to channels the
connecting user is a member of.
"""

import os
import json
from pathlib import Path

from llama_index.core import Document
from slack_sdk.errors import SlackApiError


def _resolve_channel_acl(client, channel_id: str) -> dict:
    """
    Calls the Slack API to fetch channel metadata and member list.

    Returns:
      - channel_name:    str
      - is_private:      bool
      - member_user_ids: list[str]   — all member user IDs in this channel
    """

    acl = {
        "channel_name":    channel_id,
        "is_private":      True,         # default to private (safe)
        "member_user_ids": [],
        "acl_verified": False,
    }

    # ── Channel info ──────────────────────────────────────────────────
    try:
        info_resp = client.conversations_info(channel=channel_id)
        channel   = info_resp["channel"]

        acl["channel_name"] = channel.get("name", channel_id)
        acl["is_private"]   = channel.get("is_private", True)

    except SlackApiError as e:
        print(f"  [ACL] Could not fetch channel info for {channel_id}: {e}")
        return acl

    # ── Member list ───────────────────────────────────────────────────
    try:
        members    = []
        cursor     = None

        while True:
            kwargs = {"channel": channel_id, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor

            resp   = client.conversations_members(**kwargs)
            members.extend(resp.get("members", []))

            cursor = resp.get(
                "response_metadata", {}
            ).get("next_cursor")

            if not cursor:
                break

        acl["member_user_ids"] = members
        acl["acl_verified"] = True

    except SlackApiError as e:
        print(f"  [ACL] Could not fetch members for {channel_id}: {e}")

    return acl


def load_mock_slack():
    """
    Loads Slack documents from a local mock JSON file.

    Expected mock record format:
    {
        "id":              "msg-001",
        "title":           "#engineering | 2024-01-15",
        "content":         "Alice: Has anyone reviewed the PR for...",
        "updated_at":      "2024-01-15T14:22:00Z",
        "channel_id":      "C01234567",
        "channel_name":    "engineering",
        "is_private":      false,
        "member_user_ids": ["U001", "U002", "U003"]
    }
    """

    path = Path(
        os.getenv("SLACK_MOCK_FILE", "data/mock/slack_mock.json")
    )

    if not path.exists():
        raise FileNotFoundError(f"Mock Slack file not found: {path}")

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
                    "source":        "slack",
                    "document_type": "slack_message",

                    # ── Display ───────────────────────────────
                    "title":        record["title"],
                    "updated_at":   record["updated_at"],
                    "channel_id":   record.get("channel_id", ""),
                    "channel_name": record.get("channel_name", ""),

                    # ── ACL ───────────────────────────────────
                    # A user sees this chunk if:
                    #   - is_private is False (public channel), OR
                    #   - their user_id is in member_user_ids
                    "is_private":      record.get("is_private", True),
                    "member_user_ids": record.get("member_user_ids", []),

                    # ── Ownership ─────────────────────────────
                    "user_id":         record.get("user_id", ""),
                },
            )
        )

    return documents

def _resolve_slack_user_id(client, email: str) -> str:
    """
    Resolves a user email to their Slack user ID (e.g. U01234567).
    Used so permission_filter.py compares Slack ID → Slack ID.
    """
    if not email:
        return ""

    try:
        response = client.users_lookupByEmail(email=email)
        return response["user"]["id"]
    except Exception as e:
        print(f"  [ACL] Could not resolve Slack user ID for {email}: {e}")
        return ""

def load_real_slack(credentials=None):
    """
    Loads real Slack channel messages using the SlackReader.

    Credentials are resolved in this priority order:
      1. credentials dict  (supplied by the UI connection form)
      2. Environment variables  (used for local testing / CI)

    credentials dict shape:
    {
        "bot_token":   "xoxb-...",
        "channel_ids": ["C01234567", "C07654321"]   # optional
    }

    ACL resolution:
      For each channel, the Slack API is called to fetch channel
      metadata (name, is_private) and the full member list.
      This is attached as metadata so retrieval can verify the
      querying user is a member of the channel.
    """

    creds       = credentials or {}
    bot_token   = creds.get("bot_token") or os.getenv("SLACK_BOT_TOKEN")
    channel_ids = creds.get("channel_ids") or [
        v.strip()
        for v in os.getenv("SLACK_CHANNEL_IDS", "").split(",")
        if v.strip()
    ]

    if not bot_token:
        raise ValueError("Slack bot token is required.")
    if not channel_ids:
        raise ValueError("At least one Slack channel ID is required.")

    from slack_sdk import WebClient
    from llama_index.readers.slack import SlackReader

    acl_client = WebClient(token=bot_token)

    # ── Resolve caller's Slack user ID ───────────────────────────────
    caller_email          = creds.get("email", "")
    caller_slack_user_id  = _resolve_slack_user_id(acl_client, caller_email)

    channel_acls = {}
    for channel_id in channel_ids:
        print(f"  Resolving ACL for Slack channel: {channel_id}")
        channel_acls[channel_id] = _resolve_channel_acl(acl_client, channel_id)

    unverified = [c for c, a in channel_acls.items() if not a["acl_verified"]]
    if unverified:
        raise PermissionError(f"Slack ACL verification failed for channels: {unverified}")

    reader    = SlackReader(slack_token=bot_token)
    documents = reader.load_data(channel_ids=channel_ids)

    for doc in documents:
        channel_id = doc.metadata.get("channel", channel_ids[0])
        acl        = channel_acls.get(channel_id, {})

        doc.metadata.update({
            "source_id":             doc.id_,
            "source":                "slack",
            "document_type":         "slack_message",
            "title":                 f"#{acl.get('channel_name', channel_id)} | {doc.metadata.get('date', '')}",
            "updated_at":            doc.metadata.get("date", ""),
            "channel_id":            channel_id,
            "channel_name":          acl.get("channel_name", channel_id),
            "is_private":            acl.get("is_private", True),
            "member_user_ids":       acl.get("member_user_ids", []),  # Slack IDs
            "acl_verified":          acl.get("acl_verified", False),
            "slack_user_id":         caller_slack_user_id,            # ← ADD: resolved ID
            "connected_user_email":  caller_email,
        })

    return documents

def load_slack(mode: str, credentials=None):
    """
    Main entry point for the Slack loader.

    mode="mock"  → loads from local JSON file (no credentials needed)
    mode="real"  → loads from live Slack API
    """

    mode = mode.lower()

    if mode == "mock":
        return load_mock_slack()

    if mode == "real":
        return load_real_slack(credentials=credentials)

    raise ValueError(f"Unsupported DATA_MODE: '{mode}'. Use 'mock' or 'real'.")
