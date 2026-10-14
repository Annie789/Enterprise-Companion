# ingestion/pipeline.py

"""
Ingestion pipeline for loading documents from various sources.

Supports two usage patterns:

  Pattern 1 — Environment variables only (testing / CI):
      run_ingestion()

  Pattern 2 — UI-supplied credentials (production chatbot):
      run_ingestion(credentials={
          "confluence":   {"base_url": "...", "email": "...", "api_token": "...", "space_keys": [...]},
          "jira":         {"server_url": "...", "email": "...", "api_token": "...", "project_keys": [...]},
          "slack":        {"bot_token": "...", "channel_ids": [...]},
          "google_drive": {"folder_ids": [...], "service_account_json_path": "..."},
      })

  In both patterns:
    - source kind is selected per call (default mock), not process-wide
    - Credentials dict takes priority over env vars when both are present
    - Each source is loaded independently; a failure in one does not stop others
    - Snapshots are saved per-source when SAVE_REAL_SNAPSHOT=true
"""

import os

from ingestion.loader.confluence import load_confluence
from ingestion.loader.jira import load_jira
from ingestion.loader.slack import load_slack
from ingestion.loader.drive import load_google_drive
from ingestion.snapshot import save_documents_to_json


def run_ingestion(credentials=None, kind="mock"):
    """
    Main ingestion entry point.

    Parameters
    ----------
    credentials : dict, optional
        Per-source credential dicts supplied from the UI.
        Keys: "confluence", "jira", "slack", "google_drive"
        If omitted or a source key is missing, that source
        falls back to environment variables.

    Returns
    -------
    list[Document]
        All successfully loaded documents across all sources,
        each tagged with full ACL metadata.
    """

    # ── Config from environment ────────────────────────────────────────
    if kind not in ("mock", "real"):
        raise ValueError("kind must be mock or real")
    mode = kind
    save_snapshot = os.getenv("SAVE_REAL_SNAPSHOT", "false").lower() == "true"

    # ── Credential resolution ─────────────────────────────────────────
    # credentials is a dict of dicts, one entry per source.
    # If credentials=None or a source key is absent, the loader
    # falls back to its own env var resolution.
    creds = credentials or {}

    # ── Source registry ───────────────────────────────────────────────
    loaders = {
        "confluence":   load_confluence,
        "jira":         load_jira,
        "slack":        load_slack,
        "google_drive": load_google_drive,
    }

    snapshot_env_vars = {
        "confluence":   "CONFLUENCE_SNAPSHOT_FILE",
        "jira":         "JIRA_SNAPSHOT_FILE",
        "slack":        "SLACK_SNAPSHOT_FILE",
        "google_drive": "GOOGLE_DRIVE_SNAPSHOT_FILE",
    }

    # ── Ingestion loop ────────────────────────────────────────────────
    all_documents = []

    for source, loader in loaders.items():

        print(f"\nLoading {source} (mode={mode})...")

        try:
            documents = loader(
                mode=mode,
                # Pass this source's credentials dict, or None
                # (None causes the loader to fall back to env vars)
                credentials=creds.get(source),
            )

        except Exception as e:
            # One source failing does not stop the rest
            print(f"  [ERROR] {source} failed: {e}")
            continue

        print(f"  {source}: {len(documents)} documents loaded")

        # ── Snapshot saving ───────────────────────────────────────────
        if mode == "real" and save_snapshot:

            output_file = os.getenv(snapshot_env_vars[source])

            if output_file:
                save_documents_to_json(documents, output_file)
                print(f"  Snapshot saved → {output_file}")

        all_documents.extend(documents)

    print(f"\nIngestion complete: {len(all_documents)} total documents\n")

    return all_documents
