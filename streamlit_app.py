# streamlit_app.py

import requests
import streamlit as st


import os

API_BASE_URL = os.getenv(
    "API_BASE_URL",
    "http://127.0.0.1:8000"
).rstrip("/")


# =========================================================
# Demo personas (must match auth/auth.py DEMO_USERS)
# =========================================================

DEMO_PERSONAS = {
    "annie": {
        "label": "Annie — Engineering",
        "api_key": "demo-annie-key",
    },
    "bob": {
        "label": "Bob — Security/Compliance (Admin)",
        "api_key": "demo-bob-key",
    },
    "contractor": {
        "label": "Contractor — Restricted Access",
        "api_key": "demo-contractor-key",
    },
}


# =========================================================
# Page config
# =========================================================

st.set_page_config(
    page_title="ECO - Enterprise Companion",
    page_icon="🤖",
    layout="wide",
)


# =========================================================
# Session state
# =========================================================

if "connections" not in st.session_state:
    st.session_state.connections = {}

if "messages" not in st.session_state:
    st.session_state.messages = []

if "auth_user" not in st.session_state:
    st.session_state.auth_user = None

if "auth_api_key" not in st.session_state:
    st.session_state.auth_api_key = ""


# =========================================================
# Auth helpers
# =========================================================

def get_auth_headers():
    if st.session_state.auth_api_key:
        return {"Authorization": f"Bearer {st.session_state.auth_api_key}"}
    return {}

def post_json(endpoint, payload=None):
    response = requests.post(
        f"{API_BASE_URL}{endpoint}",
        json=payload,
        headers=get_auth_headers(),
        timeout=120,
    )
    response.raise_for_status()
    return response.json()

def get_json(endpoint, params=None):
    response = requests.get(
        f"{API_BASE_URL}{endpoint}",
        params=params,
        headers=get_auth_headers(),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()

def sync_connection(connection_id):
    response = requests.post(
        f"{API_BASE_URL}/connections/{connection_id}/sync",
        headers=get_auth_headers(),
        timeout=300,
    )
    response.raise_for_status()
    return response.json()


# =========================================================
# Shared UI helpers
# =========================================================

def show_sync_result(result):
    sync_result = result.get("sync", {})
    if sync_result.get("status") == "completed":
        st.success(
            f"Sync complete — "
            f"{sync_result.get('documents', 0)} documents / "
            f"{sync_result.get('nodes', 0)} nodes."
        )
    else:
        st.error(result.get("error", "Sync failed."))

def show_connection_error(result):
    st.error(result.get("error", "Connection failed."))


# =========================================================
# Sidebar
# =========================================================

with st.sidebar:

    st.title("Enterprise Companion")
    st.caption(
        "Connect enterprise sources "
        "and ask questions across them."
    )

    st.divider()

    # ── Login / persona selector ──────────────────────────
    if st.session_state.auth_user is None:
        st.subheader("Login")
        persona = st.selectbox(
            "Select demo persona",
            list(DEMO_PERSONAS.keys()),
            format_func=lambda k: DEMO_PERSONAS[k]["label"],
        )

        if st.button("Login", use_container_width=True):
            api_key = DEMO_PERSONAS[persona]["api_key"]
            try:
                resp = requests.get(
                    f"{API_BASE_URL}/auth/whoami",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    new_user = resp.json()
                    
                    # Reset user-scoped state on every successful login.
                    st.session_state.messages = []
                    st.session_state.connections = {}
                    
                    st.session_state.auth_user = new_user
                    st.session_state.auth_api_key = api_key
                    
                    st.success(f"Logged in as {new_user['user_id']}")
                    st.rerun()
                       
                else:
                    st.error("Login failed: invalid credentials.")
            except Exception as exc:
                st.error(f"Login failed: {exc}")
    else:
        u = st.session_state.auth_user
        st.success(f"Logged in as **{u['user_id']}**")
        st.caption(f"Email: {u['email']}")
        st.caption(f"Role: {u['role']}")
        st.caption(f"Groups: {', '.join(u.get('groups', []))}")
        if u.get("is_admin"):
            st.caption("Admin access enabled")

        if st.button("Logout", use_container_width=True):
            st.session_state.messages = []
            st.session_state.connections = {}
            st.session_state.auth_user = None
            st.session_state.auth_api_key = ""
            st.rerun()

    st.divider()

    if st.session_state.auth_user:
        page = st.radio(
            "Navigation",
            ["Chat", "Connections", "Sync Status", "Admin"],
        )

        # ── Quick sync status in sidebar ──────────────────────
        try:
            sync_resp = requests.get(
                f"{API_BASE_URL}/sync/status",
                headers=get_auth_headers(),
                timeout=5,
            )
            if sync_resp.status_code == 200:
                sync_data = sync_resp.json()
                if sync_data.get("scheduler_running"):
                    last = sync_data.get("last_successful_sync")
                    if last:
                        st.caption(f"🔄 Last sync: {last[:19]}")
                    else:
                        st.caption("🔄 Scheduler running, no sync yet")
                else:
                    st.caption("⚠️ Scheduler stopped")
        except Exception:
            pass
    else:
        page = None
        st.info("Please login to continue.")


# =========================================================
# CONNECTIONS PAGE
# =========================================================

if page == "Connections":

    if not st.session_state.auth_user:
        st.warning("Please login to access connections.")
        st.stop()

    st.title("Data Sources")
    st.write("Connect your enterprise knowledge sources.")

    # Mock and real connections are simultaneously available.
    try:
        registered = get_json(f"/connections/{st.session_state.auth_user['user_id']}")
        existing = registered.get("connections", [])
    except requests.RequestException as exc:
        st.error(f"Unable to load connections: {exc}")
        st.stop()

    # Refresh UI state from authenticated backend rather than trusting
    # stale Streamlit session values after a backend restart.
    st.session_state.connections = {
        c["source"]: c["connection_id"] for c in existing if c.get("kind", "real") == "real"
        if c.get("source") in ("confluence", "jira", "slack", "google_drive")
    }

    mock_tab, real_tab = st.tabs(["Mock Connections", "Real Connections"])

    with mock_tab:
        st.subheader("Mock Enterprise Sources")
        st.caption("Synthetic Confluence, Jira, Slack and Google Drive data. No provider credentials required.")
        st.success("Mock connectors are available")
        for source in ("confluence", "jira", "slack", "google_drive"):
            label = source.replace("_", " ").title()
            connected = any(c.get("source") == source and c.get("kind") == "mock" for c in existing)
            st.write(f"{'Connected' if connected else 'Not connected'} — {label}")

        if st.button("Connect All Mock Sources", type="primary", use_container_width=True,
                     key="connect_all_mock_sources"):
            try:
                with st.spinner("Registering and synchronizing four mock sources..."):
                    result = post_json("/connections/mock/connect-all", {})
                for item in result.get("connections", []):
                    if item.get("status") == "synced":
                        st.success(f"{item['source'].replace('_',' ').title()}: synced")
                    else:
                        st.error(f"{item['source']}: {item.get('message') or 'sync failed'}")
            except requests.RequestException as exc:
                st.error(f"Mock connection failed: {exc}")
        st.caption("Connections are currently stored in backend memory and may need to be registered again after restart.")

    with real_tab:
        st.subheader("Real Enterprise Connections")
        st.caption("Connect real provider accounts using their respective credentials.")
        (
            confluence_tab,
            jira_tab,
            slack_tab,
            drive_tab,
        ) = st.tabs(["Confluence", "Jira", "Slack", "Google Drive"])


        # =====================================================
        # CONFLUENCE
        # =====================================================

        with confluence_tab:

            st.subheader("Confluence")

            confluence_url = st.text_input(
                "Confluence URL",
                placeholder="https://company.atlassian.net/wiki",
                key="confluence_url",
            )

            confluence_email = st.text_input(        # ← FIXED: added email field
                "Atlassian Email",
                placeholder="user@company.com",
                key="confluence_email",
            )

            confluence_token = st.text_input(
                "API Token",
                type="password",
                key="confluence_token",
            )

            confluence_space = st.text_input(
                "Space Key",
                placeholder="ENG",
                key="confluence_space",
            )

            col1, col2 = st.columns(2)

            with col1:

                if st.button("Test Confluence", use_container_width=True):

                    if not confluence_url or not confluence_email or not confluence_token or not confluence_space:
                        st.warning("Enter URL, email, token and space key.")
                    else:
                        try:
                            with st.spinner("Testing Confluence..."):
                                result = post_json(
                                    "/connections/confluence/test",
                                    {
                                        "user_id":    st.session_state.auth_user["user_id"],
                                        "base_url":   confluence_url,
                                        "email":      confluence_email,      # ← FIXED
                                        "api_token":  confluence_token,
                                        "space_keys": [confluence_space],
                                    },
                                )
                            if result.get("status") == "success":
                                st.success("Confluence connection successful.")
                            else:
                                show_connection_error(result)
                        except Exception as exc:
                            st.error(f"Connection failed: {exc}")


            with col2:

                if st.button("Connect Confluence", use_container_width=True):

                    if not confluence_url or not confluence_email or not confluence_token or not confluence_space:
                        st.warning("Enter URL, email, token and space key.")
                    else:
                        try:
                            with st.spinner("Connecting Confluence..."):
                                result = post_json(
                                    "/connections/confluence",
                                    {
                                        "user_id":    st.session_state.auth_user["user_id"],
                                        "base_url":   confluence_url,
                                        "email":      confluence_email,      # ← FIXED
                                        "api_token":  confluence_token,
                                        "space_keys": [confluence_space],
                                    },
                                )
                            if result.get("status") == "connected":
                                st.session_state.connections["confluence"] = result["connection_id"]
                                st.success("Confluence connected.")
                            else:
                                show_connection_error(result)
                        except Exception as exc:
                            st.error(f"Connection failed: {exc}")


            connection_id = st.session_state.connections.get("confluence")

            if connection_id:
                st.divider()
                st.success("Confluence is connected.")

                if st.button("Sync Confluence", use_container_width=True):
                    try:
                        with st.spinner("Syncing Confluence..."):
                            result = sync_connection(connection_id)
                        show_sync_result(result)
                    except Exception as exc:
                        st.error(f"Sync failed: {exc}")


        # =====================================================
        # JIRA
        # =====================================================

        with jira_tab:

            st.subheader("Jira")

            jira_url = st.text_input(
                "Jira URL",
                placeholder="https://company.atlassian.net",
                key="jira_url",
            )

            jira_email = st.text_input(
                "Email",
                placeholder="user@company.com",
                key="jira_email",
            )

            jira_token = st.text_input(
                "API Token",
                type="password",
                key="jira_token",
            )

            jira_project = st.text_input(
                "Project Key",
                placeholder="PROJ",
                key="jira_project",
            )

            col1, col2 = st.columns(2)

            with col1:

                if st.button("Test Jira", use_container_width=True):

                    if not jira_url or not jira_email or not jira_token or not jira_project:
                        st.warning("Enter URL, email, token and project key.")
                    else:
                        try:
                            with st.spinner("Testing Jira..."):
                                result = post_json(
                                    "/connections/jira/test",
                                    {
                                        "user_id":      st.session_state.auth_user["user_id"],
                                        "server_url":   jira_url,
                                        "email":        jira_email,
                                        "api_token":    jira_token,
                                        "project_keys": [jira_project],
                                    },
                                )
                            if result.get("status") == "success":
                                st.success("Jira connection successful.")
                            else:
                                show_connection_error(result)
                        except Exception as exc:
                            st.error(f"Connection failed: {exc}")


            with col2:

                if st.button("Connect Jira", use_container_width=True):

                    if not jira_url or not jira_email or not jira_token or not jira_project:
                        st.warning("Enter URL, email, token and project key.")
                    else:
                        try:
                            with st.spinner("Connecting Jira..."):
                                result = post_json(
                                    "/connections/jira",
                                    {
                                        "user_id":      st.session_state.auth_user["user_id"],
                                        "server_url":   jira_url,
                                        "email":        jira_email,
                                        "api_token":    jira_token,
                                        "project_keys": [jira_project],
                                    },
                                )
                            if result.get("status") == "connected":
                                st.session_state.connections["jira"] = result["connection_id"]
                                st.success("Jira connected.")
                            else:
                                show_connection_error(result)
                        except Exception as exc:
                            st.error(f"Connection failed: {exc}")


            connection_id = st.session_state.connections.get("jira")

            if connection_id:
                st.divider()
                st.success("Jira is connected.")

                if st.button("Sync Jira", use_container_width=True):
                    try:
                        with st.spinner("Syncing Jira..."):
                            result = sync_connection(connection_id)
                        show_sync_result(result)
                    except Exception as exc:
                        st.error(f"Sync failed: {exc}")


        # =====================================================
        # SLACK
        # =====================================================

        with slack_tab:

            st.subheader("Slack")

            slack_token = st.text_input(
                "Bot Token",
                type="password",
                placeholder="xoxb-...",
                key="slack_token",
            )

            slack_channel = st.text_input(
                "Channel ID",
                placeholder="C0123456789",
                key="slack_channel",
            )

            col1, col2 = st.columns(2)

            with col1:

                if st.button("Test Slack", use_container_width=True):

                    if not slack_token or not slack_channel:
                        st.warning("Enter bot token and channel ID.")
                    else:
                        try:
                            with st.spinner("Testing Slack..."):
                                result = post_json(
                                    "/connections/slack/test",
                                    {
                                        "user_id":     st.session_state.auth_user["user_id"],
                                        "bot_token":   slack_token,
                                        "channel_ids": [slack_channel],
                                    },
                                )
                            if result.get("status") == "success":
                                st.success("Slack connection successful.")
                            else:
                                show_connection_error(result)
                        except Exception as exc:
                            st.error(f"Connection failed: {exc}")


            with col2:

                if st.button("Connect Slack", use_container_width=True):

                    if not slack_token or not slack_channel:
                        st.warning("Enter bot token and channel ID.")
                    else:
                        try:
                            with st.spinner("Connecting Slack..."):
                                result = post_json(
                                    "/connections/slack",
                                    {
                                        "user_id":     st.session_state.auth_user["user_id"],
                                        "bot_token":   slack_token,
                                        "channel_ids": [slack_channel],
                                    },
                                )
                            if result.get("status") == "connected":
                                st.session_state.connections["slack"] = result["connection_id"]
                                st.success("Slack connected.")
                            else:
                                show_connection_error(result)
                        except Exception as exc:
                            st.error(f"Connection failed: {exc}")


            connection_id = st.session_state.connections.get("slack")

            if connection_id:
                st.divider()
                st.success("Slack is connected.")

                if st.button("Sync Slack", use_container_width=True):
                    try:
                        with st.spinner("Syncing Slack..."):
                            result = sync_connection(connection_id)
                        show_sync_result(result)
                    except Exception as exc:
                        st.error(f"Sync failed: {exc}")


        # =====================================================
        # GOOGLE DRIVE
        # =====================================================

        with drive_tab:

            st.subheader("Google Drive")

            st.info(
                "Google Drive currently uses the "
                "Google authentication configured "
                "for this application."
            )

            drive_folder = st.text_input(
                "Folder ID",
                placeholder="Google Drive folder ID",
                key="drive_folder",
            )

            col1, col2 = st.columns(2)

            with col1:

                if st.button("Test Google Drive", use_container_width=True):

                    if not drive_folder:
                        st.warning("Enter a Google Drive folder ID.")
                    else:
                        try:
                            with st.spinner("Testing Google Drive..."):
                                result = post_json(
                                    "/connections/google-drive/test",
                                    {
                                        "user_id":    st.session_state.auth_user["user_id"],
                                        "folder_ids": [drive_folder],
                                    },
                                )
                            if result.get("status") == "success":
                                st.success("Google Drive connection successful.")
                            else:
                                show_connection_error(result)
                        except Exception as exc:
                            st.error(f"Connection failed: {exc}")


            with col2:

                if st.button("Connect Google Drive", use_container_width=True):

                    if not drive_folder:
                        st.warning("Enter a Google Drive folder ID.")
                    else:
                        try:
                            with st.spinner("Connecting Google Drive..."):
                                result = post_json(
                                    "/connections/google-drive",
                                    {
                                        "user_id":    st.session_state.auth_user["user_id"],
                                        "folder_ids": [drive_folder],
                                    },
                                )
                            if result.get("status") == "connected":
                                st.session_state.connections["google_drive"] = result["connection_id"]
                                st.success("Google Drive connected.")
                            else:
                                show_connection_error(result)
                        except Exception as exc:
                            st.error(f"Connection failed: {exc}")


            connection_id = st.session_state.connections.get("google_drive")

            if connection_id:
                st.divider()
                st.success("Google Drive is connected.")

                if st.button("Sync Google Drive", use_container_width=True):
                    try:
                        with st.spinner("Syncing Google Drive..."):
                            result = sync_connection(connection_id)
                        show_sync_result(result)
                    except Exception as exc:
                        st.error(f"Sync failed: {exc}")


# =========================================================
# SYNC STATUS PAGE
# =========================================================

elif page == "Sync Status":

    if not st.session_state.auth_user:
        st.warning("Please login to access sync status.")
        st.stop()

    st.title("🔄 Auto-Sync Status")
    st.caption(
        "Mock-source freshness simulation — checks "
        "Confluence, Jira, Slack and Google Drive mock "
        "data every 30 seconds (configurable via "
        "MOCK_SYNC_INTERVAL_SECONDS)."
    )

    try:
        status = get_json("/sync/status")
    except Exception as exc:
        st.error(f"Failed to fetch sync status: {exc}")
        st.stop()

    # ── Scheduler state ───────────────────────────────────────
    if status.get("scheduler_running"):
        st.success("✅ Auto-sync scheduler is running.")
    else:
        st.warning("⚠️ Auto-sync scheduler is NOT running.")

    # ── Freshness summary ─────────────────────────────────────
    col1, col2, col3 = st.columns(3)

    with col1:
        last_sync = status.get("last_successful_sync")
        if last_sync:
            st.metric("Last Successful Sync", last_sync[:19])
        else:
            st.metric("Last Successful Sync", "Never")

    with col2:
        last_change = status.get("last_change_detected")
        if last_change:
            st.metric("Last Change Detected", last_change[:19])
        else:
            st.metric("Last Change Detected", "None")

    with col3:
        latency = status.get("freshness_latency_seconds")
        if latency is not None:
            st.metric("Freshness Latency", f"{latency:.1f}s")
        else:
            st.metric("Freshness Latency", "N/A")

    # ── Is source data current? ───────────────────────────────
    st.divider()
    st.subheader("Source Data Freshness")

    source_hashes = status.get("source_hashes", {})
    if source_hashes:
        for source, h in source_hashes.items():
            st.text(f"  {source}: {h[:16]}...")
    else:
        st.info("No source data has been scanned yet.")

    # ── Error ─────────────────────────────────────────────────
    last_error = status.get("last_error")
    if last_error:
        st.error(f"Last error: {last_error}")

    # ── Recent cycles ──────────────────────────────────────────
    st.divider()
    st.subheader("Recent Sync Cycles")

    cycles = status.get("recent_cycles", [])
    if cycles:
        import pandas as pd
        df = pd.DataFrame(cycles)
        display_cols = [
            "timestamp", "added", "updated", "deleted",
            "skipped_unchanged", "skipped_revoked",
            "changes_detected", "latency_ms",
        ]
        for col in display_cols:
            if col not in df.columns:
                df[col] = ""
        st.dataframe(
            df[display_cols],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No sync cycles recorded yet.")

    st.caption(f"Total syncs: {status.get('total_syncs', 0)} | "
                f"Total errors: {status.get('total_errors', 0)}")

    # ── Manual trigger (admin only) ───────────────────────────
    if st.session_state.auth_user.get("is_admin"):
        st.divider()
        st.subheader("Manual Sync Trigger")
        st.caption("Force a sync cycle now (admin only).")
        if st.button("🔄 Trigger Sync Now", use_container_width=True):
            try:
                result = post_json("/sync/trigger", {})
                st.success("Sync cycle completed.")
                st.rerun()
            except Exception as exc:
                st.error(f"Sync trigger failed: {exc}")


# =========================================================
# CHAT PAGE
# =========================================================

elif page == "Chat":

    if not st.session_state.auth_user:
        st.warning("Please login to access chat.")
        st.stop()

    st.title("Enterprise Knowledge Assistant")
    st.caption("Ask questions across your indexed enterprise knowledge.")

    # Read all user connections from the backend, including mock and real.
    # st.session_state.connections intentionally tracks only real connections
    # for the real-provider connection/sync forms.
    try:
        registered = get_json(
            f"/connections/{st.session_state.auth_user['user_id']}"
        )
        all_connections = registered.get("connections", [])
        active_connections = [
            c for c in all_connections
            if c.get("status") in ("connected", "synced")
            and c.get("source") in
            ("confluence", "jira", "slack", "google_drive")
        ]
        if active_connections:
            source_labels = list(dict.fromkeys(
                f"{c['source'].replace('_', ' ').title()} "
                f"({'Mock' if c.get('kind') == 'mock' else 'Real'})"
                for c in active_connections
            ))
            st.caption("Connected sources: " + ", ".join(source_labels))
        else:
            st.info("No sources are connected yet. Open Connections to add a source.")
    except requests.RequestException as exc:
        st.warning(f"Unable to check connected sources: {exc}")

    # Display history
    for message in st.session_state.messages:

        with st.chat_message(message["role"]):
            st.markdown(message["content"])

            citations = message.get("citations", [])

            if citations:
                with st.expander("Sources"):
                    for citation in citations:
                        citation_id = citation.get("citation_id", "?")
                        source      = citation.get("source", "unknown")
                        title       = citation.get("title", "Untitled")
                        updated_at  = citation.get("updated_at")
                        url         = citation.get("url", "")

                        if url:
                            st.markdown(f"**[{citation_id}] {source}** — [{title}]({url})")
                        else:
                            st.markdown(f"**[{citation_id}] {source}** — {title}")

                        if updated_at:
                            st.caption(f"Updated: {updated_at}")

    # Chat input
    prompt = st.chat_input("Ask a question...")

    if prompt:

        st.session_state.messages.append({
            "role":    "user",
            "content": prompt,
        })

        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Searching..."):
                try:
                    result = post_json(
                        "/query",
                        {
                            "query": prompt,
                        },
                    )

                    answer    = result.get("answer", "No answer returned.")
                    citations = result.get("citations", [])

                    st.markdown(answer)

                    agentic = result.get("agentic", {})
                    if agentic.get("enabled"):
                        with st.expander("Agentic RAG Evaluation"):
                            st.write("Selected sources:", ", ".join(agentic.get("selected_sources", [])) or "Unavailable")
                            st.write("Actual tool executions:", agentic.get("tool_execution_count", 0))
                            st.write("Tool execution trace:")
                            st.json(agentic.get("tool_trace", []))
                            for label, field in (("Context relevance", "context_relevance"),
                                                 ("Faithfulness", "faithfulness"),
                                                 ("Answer relevancy", "answer_relevancy")):
                                val = agentic.get(field)
                                st.write(label + ":", f"{val:.2f}" if isinstance(val, (int, float)) else "Not evaluated")
                            st.caption("Context evaluation: " + agentic.get("evaluation_status", "unknown")
                                       + " | Answer evaluation: " + agentic.get("post_evaluation_status", "unknown"))

                    if citations:
                        with st.expander("Sources"):
                            for citation in citations:
                                citation_id = citation.get("citation_id", "?")
                                source      = citation.get("source", "unknown")
                                title       = citation.get("title", "Untitled")
                                updated_at  = citation.get("updated_at")
                                url         = citation.get("url", "")

                                if url:
                                    st.markdown(f"**[{citation_id}] {source}** — [{title}]({url})")
                                else:
                                    st.markdown(f"**[{citation_id}] {source}** — {title}")

                                if updated_at:
                                    st.caption(f"Updated: {updated_at}")

                    st.session_state.messages.append({
                        "role":      "assistant",
                        "content":   answer,
                        "citations": citations,
                        "agentic": agentic,
                    })

                except Exception as exc:
                    error_msg = f"Error: {exc}"
                    st.error(error_msg)
                    st.session_state.messages.append({
                        "role":    "assistant",
                        "content": error_msg,
                    })


# =========================================================
# ADMIN PAGE
# =========================================================

elif page == "Admin":

    # ── Admin-only page ──────────────────────────────────
    if not st.session_state.auth_user or not st.session_state.auth_user.get("is_admin"):
        st.warning("Admin access required. Please login as Bob (compliance).")
        st.stop()

    st.title("🔒 Admin — Live Revocation & Audit")
    st.caption("Simulate real-time access revocation and inspect the audit trail.")

    # ─────────────────────────────────────────────────────
    # Revoke a specific document
    # ─────────────────────────────────────────────────────
    st.subheader("Revoke a Specific Document")

    col1, col2, col3 = st.columns(3)

    with col1:
        rev_user = st.text_input("Target User ID", key="rev_user")

    with col2:
        rev_source = st.selectbox(
            "Source",
            ["confluence", "jira", "slack", "google_drive"],
            key="rev_source",
        )

    with col3:
        rev_resource = st.text_input(
            "Resource ID",
            placeholder="CONF-001 / JIRA-126 / SLACK-104",
            key="rev_resource",
        )

    if st.button("🚫 Revoke Document Access", use_container_width=True):
        if not rev_user or not rev_resource:
            st.warning("Enter Target User ID and Resource ID.")
        else:
            try:
                result = post_json(
                    f"/revoke/resource?target_user_id={rev_user}",
                    {
                        "source":      rev_source,
                        "resource_id": rev_resource,
                    },
                )
                st.success(
                    f"Revoked **{rev_resource}** from **{rev_source}** "
                    f"for user **{rev_user}**. "
                    f"Nodes removed: {result.get('nodes_removed', 0)}"
                )
            except Exception as exc:
                st.error(f"Revocation failed: {exc}")

    st.divider()

    # ─────────────────────────────────────────────────────
    # Revoke entire source
    # ─────────────────────────────────────────────────────
    st.subheader("Revoke Entire Source")

    col1, col2 = st.columns(2)

    with col1:
        rs_user = st.text_input("Target User ID", key="rs_user")

    with col2:
        rs_source = st.selectbox(
            "Source",
            ["confluence", "jira", "slack", "google_drive"],
            key="rs_source",
        )

    if st.button("🚫 Revoke Full Source", use_container_width=True):
        if not rs_user:
            st.warning("Enter a Target User ID.")
        else:
            try:
                result = post_json(
                    f"/revoke/source?target_user_id={rs_user}",
                    {
                        "source": rs_source,
                    },
                )
                st.success(
                    f"All **{rs_source}** content revoked for **{rs_user}**. "
                    f"Nodes removed: {result.get('nodes_removed', 0)}"
                )
            except Exception as exc:
                st.error(f"Revocation failed: {exc}")

    st.divider()

    # ─────────────────────────────────────────────────────
    # Simulate webhook permission change
    # ─────────────────────────────────────────────────────
    st.subheader("Simulate Permission-Change Webhook")
    st.caption("Mimics a real-time webhook from Confluence / Jira when permissions change.")

    col1, col2, col3 = st.columns(3)

    with col1:
        wh_platform = st.selectbox(
            "Platform",
            ["confluence", "jira", "slack", "google_drive"],
            key="wh_platform",
        )

    with col2:
        wh_resource = st.text_input(
            "Resource ID",
            placeholder="CONF-003",
            key="wh_resource",
        )

    with col3:
        wh_user = st.text_input(
            "User ID (blank = all users)",
            key="wh_user",
        )

    if st.button("📡 Fire Webhook", use_container_width=True):
        if not wh_resource:
            st.warning("Enter a Resource ID.")
        else:
            try:
                result = post_json(
                    "/webhooks/permission-change",
                    {
                        "platform":    wh_platform,
                        "event":       "page_restrictions_updated",
                        "resource_id": wh_resource,
                        "user_id":     wh_user if wh_user.strip() else None,
                    },
                )
                st.success(
                    f"Webhook processed. "
                    f"Affected users: {result.get('affected_users', [])}"
                )
            except Exception as exc:
                st.error(f"Webhook failed: {exc}")

    st.divider()

    # ─────────────────────────────────────────────────────
    # Task 4 — Live Permission Change Reconciliation
    # ─────────────────────────────────────────────────────
    st.subheader("🔄 Live Permission Change Reconciliation (Task 4)")
    st.caption(
        "Simulate an ACL change in the mock source data, then "
        "trigger reconciliation to update the index."
    )

    # Step 1: Select source document
    col1, col2 = st.columns(2)

    with col1:
        perm_source = st.selectbox(
            "Source",
            ["confluence", "jira", "slack", "google_drive"],
            key="perm_source",
        )

    with col2:
        perm_action = st.selectbox(
            "ACL Action",
            ["revoke", "grant", "delete"],
            key="perm_action",
        )

    # Load mock documents for the selected source
    try:
        mock_resp = requests.get(
            f"{API_BASE_URL}/permissions/mock-documents",
            params={"source": perm_source},
            headers=get_auth_headers(),
            timeout=10,
        )
        if mock_resp.status_code == 200:
            mock_docs = mock_resp.json().get("documents", [])
            doc_options = [
                f"{d['id']} — {d.get('title', '')}"
                for d in mock_docs
            ]
            doc_ids = [d["id"] for d in mock_docs]
        else:
            doc_options = []
            doc_ids = []
    except Exception:
        doc_options = []
        doc_ids = []

    if doc_options:
        selected_idx = st.selectbox(
            "Select Mock Document",
            range(len(doc_options)),
            format_func=lambda i: doc_options[i],
            key="perm_doc_idx",
        )
        selected_source_id = doc_ids[selected_idx]
    else:
        st.warning("No mock documents found for this source.")
        selected_source_id = ""

    perm_target_user = st.selectbox(
        "Target User",
        ["annie", "bob", "contractor"],
        key="perm_target_user",
    )

    # Step 2: Simulate ACL change
    if st.button("📝 1. Simulate ACL Change", use_container_width=True):
        if not selected_source_id:
            st.warning("Select a document first.")
        else:
            try:
                result = post_json(
                    "/permissions/simulate-acl-change",
                    {
                        "source":         perm_source,
                        "source_id":      selected_source_id,
                        "action":         perm_action,
                        "target_user_id": perm_target_user,
                    },
                )
                st.success(
                    f"ACL modified: **{perm_action}** "
                    f"for **{perm_target_user}** on "
                    f"**{selected_source_id}**. "
                    f"Result: {result.get('acl_change_result', {})}"
                )
            except Exception as exc:
                st.error(f"ACL simulation failed: {exc}")

    # Step 3: Trigger reconciliation
    if st.button("🔄 2. Trigger Reconciliation", use_container_width=True):
        if not selected_source_id:
            st.warning("Select a document first.")
        else:
            try:
                result = post_json(
                    "/permissions/reconcile",
                    {
                        "source":         perm_source,
                        "source_id":      selected_source_id,
                        "action":         perm_action,
                        "target_user_id": perm_target_user,
                        "connection_id":  "",
                    },
                )
                st.success(
                    f"Reconciliation complete. "
                    f"Indexed before: {result.get('indexed_before')}, "
                    f"after: {result.get('indexed_after')}. "
                    f"Nodes removed: {result.get('nodes_removed', 0)}, "
                    f"added: {result.get('nodes_added', 0)}. "
                    f"Skipped: {result.get('skipped', False)} "
                    f"({result.get('reason', '')})"
                )
            except Exception as exc:
                st.error(f"Reconciliation failed: {exc}")

    # Step 4: View revocation status
    if st.button("🔍 3. View Revocation Status", use_container_width=True):
        try:
            resp = requests.get(
                f"{API_BASE_URL}/permissions/status",
                headers=get_auth_headers(),
                timeout=10,
            )
            resp.raise_for_status()
            status = resp.json()
            revoked = status.get("revoked_documents", [])
            if revoked:
                st.dataframe(revoked, use_container_width=True)
                st.caption(f"{status.get('total_revoked', 0)} revoked entries.")
            else:
                st.info("No documents currently in the revocation registry.")
        except Exception as exc:
            st.error(f"Status check failed: {exc}")

    st.divider()

    # ─────────────────────────────────────────────────────
    # Audit log viewer
    # ─────────────────────────────────────────────────────
    st.subheader("📋 Audit Log")
    st.caption(
        "Reconstruct any user's query: who asked, what was "
        "retrieved, access decisions, and the final answer."
    )

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        audit_user = st.text_input("Filter: User ID", key="audit_user")

    with col2:
        audit_outcome = st.selectbox(
            "Filter: Outcome",
            ["", "ALLOWED", "DENIED", "SUCCESS", "ERROR"],
            key="audit_outcome",
        )

    with col3:
        audit_source = st.selectbox(
            "Filter: Source",
            ["", "confluence", "jira", "slack", "google_drive"],
            key="audit_source",
        )

    with col4:
        audit_query_id = st.text_input(
            "Filter: Query ID", key="audit_query_id"
        )

    col_d1, col_d2 = st.columns(2)
    with col_d1:
        audit_since = st.date_input("From date", key="audit_since")
    with col_d2:
        audit_until = st.date_input("To date", key="audit_until")

    if st.button("🔍 Load Audit Log", use_container_width=True):
        try:
            params = {}
            if audit_user:     params["user_id"] = audit_user
            if audit_outcome:  params["outcome"] = audit_outcome
            if audit_source:   params["source"]  = audit_source
            if audit_query_id: params["query_id"] = audit_query_id
            if audit_since:
                params["since"] = f"{audit_since}T00:00:00"
            if audit_until:
                params["until"] = f"{audit_until}T23:59:59"

            resp = requests.get(
                f"{API_BASE_URL}/audit/logs",
                params=params,
                headers=get_auth_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            logs = resp.json().get("logs", [])

            if logs:
                # ── Summary table ──────────────────────────────
                import pandas as pd
                summary_rows = []
                for log in logs:
                    detail = log.get("detail", {})
                    summary_rows.append({
                        "Query ID":  log.get("query_id", "")[:8],
                        "Timestamp": log.get("timestamp", "")[:19],
                        "User":      log.get("user_id", ""),
                        "Question":  log.get("query", "")[:60],
                        "Source":    log.get("source", ""),
                        "Doc ID":    log.get("resource_id", ""),
                        "Outcome":   log.get("outcome", ""),
                    })
                st.dataframe(
                    pd.DataFrame(summary_rows),
                    use_container_width=True,
                    hide_index=True,
                )
                st.caption(f"{len(logs)} entries returned.")

                # ── Detailed per-query reconstruction ───────────
                st.divider()
                st.subheader("Query Reconstruction")
                st.caption(
                    "Groups entries by Query ID to show the full "
                    "audit trail for each query."
                )

                # Group by query_id
                from collections import OrderedDict
                grouped = OrderedDict()
                for log in logs:
                    qid = log.get("query_id") or "(no query_id)"
                    grouped.setdefault(qid, []).append(log)

                for qid, entries in grouped.items():
                    with st.expander(
                        f"Query {qid[:8]} — {entries[0].get('user_id', '?')} — "
                        f"{entries[0].get('timestamp', '')[:19]}"
                    ):
                        # User + timestamp + question
                        first = entries[0]
                        st.markdown(f"**User:** {first.get('user_id', '')}")
                        st.markdown(f"**Timestamp:** {first.get('timestamp', '')[:19]}")
                        st.markdown(f"**Question:** {first.get('query', '')}")

                        # Collect detail from SUCCESS/ERROR entries
                        retrieved_doc_ids = []
                        auth_decisions = []
                        final_answer = ""
                        citations = []
                        query_outcome = ""

                        for e in entries:
                            d = e.get("detail", {})
                            if d.get("retrieved_doc_ids"):
                                retrieved_doc_ids = d["retrieved_doc_ids"]
                            if d.get("auth_decisions"):
                                auth_decisions = d["auth_decisions"]
                            if d.get("final_answer"):
                                final_answer = d["final_answer"]
                            if d.get("citations"):
                                citations = d["citations"]
                            if d.get("query_outcome"):
                                query_outcome = d["query_outcome"]

                        # Retrieved source documents
                        if retrieved_doc_ids:
                            st.markdown("**Retrieved Source Documents:**")
                            for doc_id in retrieved_doc_ids:
                                st.markdown(f"  - `{doc_id}`")
                        else:
                            st.markdown("**Retrieved Source Documents:** None")

                        # Access decisions
                        if auth_decisions:
                            st.markdown("**Access Decisions:**")
                            for dec in auth_decisions:
                                st.markdown(
                                    f"  - `{dec.get('source_id', '')}` "
                                    f"({dec.get('source', '')}) — "
                                    f"**{dec.get('decision', '')}**"
                                )
                        elif first.get("outcome") == "DENIED":
                            st.markdown("**Access Decisions:** All denied / no results")

                        # Final answer
                        if final_answer:
                            st.markdown("**Final Answer:**")
                            st.text(final_answer[:500] + ("..." if len(final_answer) > 500 else ""))

                        # Citations
                        if citations:
                            st.markdown("**Source Citations:**")
                            for cit in citations:
                                st.markdown(
                                    f"  - [{cit.get('citation_id', '?')}] "
                                    f"{cit.get('source', '')} — "
                                    f"{cit.get('title', '')}"
                                )

                        # Outcome
                        st.markdown(f"**Query Outcome:** {query_outcome or first.get('outcome', '')}")

            else:
                st.info("No audit entries found for the selected filters.")

        except Exception as exc:
            st.error(f"Failed to load audit log: {exc}")

    st.divider()

    # ─────────────────────────────────────────────────────
    # Chain integrity verification
    # ─────────────────────────────────────────────────────
    st.subheader("🔐 Verify Audit Chain Integrity")
    st.caption("Confirms no audit entries have been tampered with.")

    if st.button("✅ Verify Chain", use_container_width=True):
        try:
            resp = requests.get(
                f"{API_BASE_URL}/audit/verify",
                headers=get_auth_headers(),
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("intact"):
                st.success("✅ Audit chain is intact — no tampering detected.")
            else:
                st.error(
                    f"⚠️ Chain integrity broken at entry: "
                    f"`{result.get('broken_at')}`"
                )
        except Exception as exc:
            st.error(f"Verification failed: {exc}")
