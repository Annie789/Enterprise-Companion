# DEMO — Aspire Internal Brain (Agentic RAG)

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
DATA_MODE=mock
OPENAI_API_KEY=your_key
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
ADP_APP_KEY=your_adp_app_key
ADP_BASE_URL=https://wss.lke.tencentcloud.com/adp/v2/chat
MOCK_SYNC_INTERVAL_SECONDS=30
MOCK_SYNC_DISABLE=false
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
| Alice | `demo-alice-key` | Engineering |
| Bob | `demo-bob-key` | Compliance / admin |
| Contractor | `demo-contractor-key` | Restricted |

Select each persona in the sidebar and click **Login**. Mock credentials are for the demo only.

## 5. One-Time Setup

1. Log in as **Alice** → **Connections** → connect and sync **Confluence, Jira, Slack and Google Drive** (use **Connect All Mock Sources** if shown).
2. Repeat for **Bob**, then **Contractor**. Ensure the intended connections are indexed for each user.
3. Keep the mock JSON files unchanged until **Scenario C**. Reset any earlier revocations before beginning.
4. Confirm Sync Status reports success.

## 6. Demonstration Scenarios

### Scenario A — Unified Query + Mandatory Agent Tools

**Goal:** Prove that the agent executed retrieval tools and ADP used authorized results.

1. Login: **Alice** → **Chat**.
2. Ask: **What is the production release schedule and what validation is needed?**
3. **Expected:** Answer includes **15 October 2026** release and **14 October** validation, with citations from relevant Confluence/Jira/Slack/Drive records. The exact sources depend on actual retrieved nodes.
4. Expand **Agentic RAG Evaluation**: `Actual tool executions` must be **greater than zero**. `tool_trace` must contain real `search_*` calls and result counts. Confirm cited document IDs belong to executed tools and are authorized for Alice.
5. **Fail** if there were no tool executions, an agent failure was silently replaced by ordinary retrieval, or citations refer to inaccessible content.

### Scenario B — Negative Permission Enforcement

**Goal:** Restricted content must not enter the answer or citations.

1. Login: **Alice**. Ask **What is the incident response procedure?**
2. **Expected:** Authorized Confluence procedure (`CONF-004`) may be cited.
3. Login: **Contractor**. Ask the **same** question.
4. **Expected:** **I don't have information on that topic in the available sources.** No citation to `CONF-004`, and no confirmation of restricted document existence.
5. **Pass** only if denial is enforced before ADP context assembly. Audit the resulting query separately in Scenario E.

### Scenario C — Data Freshness

**Goal:** Updated source data becomes visible after synchronization.

1. Login: **Alice**. Ask **Who is the release owner in the Production Release Plan?** Baseline: **Alice Tan**, cited to `CONF-001`.
2. Back up `data/mock/confluence_mock.json`.
3. Edit **CONF-001** `content`: change **Alice Tan** to **David Lim**, and append **Database backup verification is mandatory before deployment.** Update `updated_at` to a newer, valid UTC timestamp (do not modify ACL fields).
4. Wait for automatic sync and inspect **Sync Status**; or login as **Bob** and trigger **Sync**.
5. Ask as Alice: **According to the latest Production Release Plan, who owns the release and what new step is mandatory?**
6. **Expected:** **David Lim** and **database backup verification**, citing the updated `CONF-001`. Record edit time, sync time and first correct query time.
7. Restore the JSON backup and re-sync after this scenario.

### Scenario D — Live Permission Revocation

**Goal:** Revoked access is not returned by subsequent queries or restored by another sync.

1. As **Alice**, ask **What does the Restricted Architecture Migration Plan say about Q4 edge migration?** Expected: `CONF-003` while access is granted.
2. As **Bob**, open **Admin** → permission/revocation controls. Revoke **Alice** access to **confluence / CONF-003** and run permission reconciliation (or the available **Reconcile** action).
3. As Alice, repeat the question. **Expected:** No protected content or citation for `CONF-003`; if no alternative authorized evidence exists, generic no-information response.
4. Run another sync; ask again. **Expected:** `CONF-003` must **not** reappear.
5. Reset the revocation/mock state before repeating the demo.

### Scenario E — Audit Inquiry and Integrity

**Goal:** Reconstruct the previous user activity and verify audit integrity.

1. Login: **Bob** → **Admin** → **Audit Log**.
2. Filter user **alice** and event **QUERY**.
3. Open a successful query. **Expected:** `query_id`, timestamp, query, authorized retrieved document IDs, allow decisions, answer, citations and agent trace in the query's detail.
4. Click **Verify Chain Integrity**. **Expected:** chain `intact = true`.
5. **Note:** The current query audit records authorized results; do not claim it captures every rejected vector candidate.

### Scenario F — RAG Triad Evaluation

**Goal:** Show measured RAG quality.

1. Keep `RAG_EVALUATION_ENABLED=true`; ensure `ragas` and model credentials work.
2. Login as **Alice** and repeat the **Scenario A** question.
3. Expand **Agentic RAG Evaluation** and record these actual values:

| Metric | Suggested demo target* | Actual score |
|---|---:|---|
| Context relevance | ≥ 0.80 | ______ |
| Faithfulness | ≥ 0.90 | ______ |
| Answer relevance | ≥ 0.80 | ______ |

4. Check **Context evaluation** = `ok` and **Answer evaluation** = `ok`; each score must be numeric between 0 and 1. If any score is missing or a status is `unavailable`, `error`, `invalid_score` or `partial_failure`, evaluation has **not passed**.
5. Use a different question, such as **What is the VPN support guide?**, and compare the three results. Scores need not differ on every query, but each must be calculated for the submitted question/context/answer.
6. Assess the result: context relevance tests retrieval quality; faithfulness tests whether claims are grounded; answer relevance tests whether the user question was answered. **RAG Triad does not prove RBAC security**; Scenario B and D cover that.

*Thresholds are suggested project targets, not universal standards. Scores are not supplied in advance.

**Optional API verification (Alice):**

```powershell
$body = @{query='What is the production release schedule and what validation is needed?'} | ConvertTo-Json
$r = Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8000/query -Headers @{Authorization='Bearer demo-alice-key'} -ContentType 'application/json' -Body $body
$r.status
$r.agentic | Format-List tool_execution_count,tool_trace,context_relevance,faithfulness,answer_relevancy,evaluation_status,post_evaluation_status
```

`status=processed` means successful generation; `status=no_context` means no authorized evidence; `status=error` means an application/agent failure. Treat missing RAGAS scores as not evaluated.

## 7. Quick Results Checklist

| Test | Pass? |
|---|---|
| Backend health and all four sources synced | ☐ |
| A — Answer + real agent tool executions + citations | ☐ |
| B — Contractor receives no restricted information | ☐ |
| C — Updated fact returned after sync | ☐ |
| D — Revoked content stays unavailable after resync | ☐ |
| E — Audit reconstruction + chain integrity | ☐ |
| F — All three actual RAGAS scores with `ok` statuses | ☐ |

**Done:** Once every row passes, capture screenshots of the chat answer/citations, tool trace, RAG scores, sync update, revocation result, and audit verification for the presentation.
