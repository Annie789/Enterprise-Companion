# ECO - Enterprise Companion

Follow these scenarios in order. 

## 1. Prerequisites

Install dependencies in your activated virtual environment:

```powershell
python -m pip install -r requirements.txt
python -m pip check
```

## 2. Environment Variables

Create `.env` in the application root (never commit real keys):

```dotenv
OPENAI_API_KEY=your_key
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
ADP_APP_KEY=your_adp_app_key
ADP_BASE_URL=https://wss.lke.tencentcloud.com/adp/v2/chat
MOCK_SYNC_INTERVAL_SECONDS=30
RAG_EVALUATION_ENABLED=true
RAGAS_LLM_MODEL=gpt-4o-mini
RAGAS_EMBED_MODEL=text-embedding-3-small
```

## 3. Commands to Start

**Terminal 1 — Backend**

```powershell
python -m uvicorn backend:app --host 127.0.0.1 --port 8000
```

Check `http://127.0.0.1:8000/health` for a healthy response.

**Terminal 2 — Streamlit**

```powershell
python -m streamlit run streamlit_app.py --server.port 8501
```

Open `http://localhost:8501`.

## 4. Demo Users

| Persona | Demo API key | Role |
|---|---|---|
| Annie | `demo-annie-key` | Engineering |
| Bob | `demo-bob-key` | Compliance / admin |
| Contractor | `demo-contractor-key` | Restricted |

Select each persona in the sidebar and click **Login**. Mock credentials are for the demo only.

## 5. One-Time Setup

1. Log in as **Annie** → **Connections** → connect and sync **Confluence, Jira, Slack and Google Drive** (use **Connect All Mock Sources** if shown).
2. Repeat for **Bob**, then **Contractor**. Ensure the intended connections are indexed for each user.
3. Keep the mock JSON files unchanged until **Scenario C**. Reset any earlier revocations before beginning.
4. Confirm Sync Status reports success.

## 6. Demonstration Scenarios

### Scenario A — Unified Query + Mandatory Agent Tools

**Goal:** Prove that the agent executed retrieval tools and ADP used authorized results.

1. Login: **Annie** → **Chat**.
2. Ask: **What is the production release schedule and what validation is needed?**
3. **Expected:** Answer includes **15 October 2026** release and **14 October** validation, with citations from relevant Confluence/Jira/Slack/Drive records. The exact sources depend on actual retrieved nodes.
4. Expand **Agentic RAG Evaluation**: `Actual tool executions` must be **greater than zero**. `tool_trace` must contain real `search_*` calls and result counts. Confirm cited document IDs belong to executed tools and are authorized for Annie.
5. **Fail** if there were no tool executions, an agent failure was silently replaced by ordinary retrieval, or citations refer to inaccessible content.

### Scenario B — Negative Permission Enforcement

**Goal:** Restricted content must not enter the answer or citations.

1. Login: **Annie**. Ask **What is the incident response procedure?**
2. **Expected:** Authorized Confluence procedure (`CONF-004`) may be cited.
3. Login: **Contractor**. Ask the **same** question.
4. **Expected:** **I don't have information on that topic in the available sources.** No citation to `CONF-004`, and no confirmation of restricted document existence.
5. **Pass** only if denial is enforced before ADP context assembly. Audit the resulting query separately in Scenario E.

### Scenario C — Data Freshness

**Goal:** Updated source data becomes visible after synchronization.

1. Login: **Annie**. Ask **Who is the release owner in the Production Release Plan?** Baseline: **Annie**, cited to `CONF-001`.
2. Back up `data/mock/confluence_mock.json`.
3. Edit **CONF-001** `content`: change **Annie** to **David**, and append **Database backup verification is mandatory before deployment.** Update `updated_at` to a newer, valid UTC timestamp (do not modify ACL fields).
4. Wait for automatic sync and inspect **Sync Status**; or login as **Bob** and trigger **Sync**.
5. Ask as Annie: **According to the latest Production Release Plan, who owns the release and what new step is mandatory?**
6. **Expected:** **David Lim** and **database backup verification**, citing the updated `CONF-001`. Record edit time, sync time and first correct query time.
7. Restore the JSON backup and re-sync after this scenario.

### Scenario D — Live Permission Revocation

**Goal:** Revoked access is not returned by subsequent queries or restored by another sync.

1. As **Annie**, ask **What does the Restricted Architecture Migration Plan say about Q4 edge migration?** Expected: `CONF-003` while access is granted.
2. As **Bob**, open **Admin** → permission/revocation controls. Revoke **Annie** access to **confluence / CONF-003** and run permission reconciliation (or the available **Reconcile** action).
3. As Annie, repeat the question. **Expected:** No protected content or citation for `CONF-003`; if no alternative authorized evidence exists, generic no-information response.
4. Run another sync; ask again. **Expected:** `CONF-003` must **not** reappear.
5. Reset the revocation/mock state before repeating the demo.

### Scenario E — Audit Inquiry and Integrity

**Goal:** Reconstruct the previous user activity and verify audit integrity.

1. Login: **Bob** → **Admin** → **Audit Log**.
2. Filter user **annie** and event **QUERY**.
3. Open a successful query. **Expected:** `query_id`, timestamp, query, authorized retrieved document IDs, allow decisions, answer, citations and agent trace in the query's detail.
4. Click **Verify Chain Integrity**. **Expected:** chain `intact = true`.
5. **Note:** The current query audit records authorized results; do not claim it captures every rejected vector candidate.

