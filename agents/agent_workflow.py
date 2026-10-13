# agents/agent_workflow.py
#
# Task 10 — Agent Orchestration and Query Routing
#
# Uses LlamaIndex FunctionAgent (an established LlamaIndex
# agent framework) to decide which source retrieval tools
# should be called for a user question.
#
# ── ADP Investigation Summary ──────────────────────────
#
# Tencent ADP (generation/adp_client.py) is a text-in /
# text-out SSE streaming endpoint. Its request payload
# sends `Contents: [{Type: "text", Text: ...}]` and the
# response stream delivers `text.delta`, `text.replace`,
# `response.completed`, and `message.done` events.
#
# ADP does NOT implement an OpenAI-style function-calling
# / tool-calling protocol. There is no `tools` parameter,
# no `tool_calls` in the response, and no structured
# tool-selection interface.
#
# Decision: Use OpenAI (via llama-index-llms-openai) as the
# routing LLM for FunctionAgent. Keep Tencent ADP unchanged
# for answer generation only. Do NOT build a homemade
# tool-calling engine (Task 10 requirement 11).
#
# ── Security Model ──────────────────────────────────────
#
#   1. User identity (user_context) and retrieval_service
#      are injected by trusted application code via
#      FunctionTool partial_params. The LLM/agent NEVER
#      selects these values.
#
#   2. The agent system prompt forbids changing identity,
#      permissions, or connection ownership. The agent can
#      only call the four retrieval tools with a query
#      string.
#
#   3. max_iterations=4 enforces a maximum of four tool
#      execution rounds per retrieval attempt.
#      timeout=30s prevents runaway loops.
#
#   4. Only display-safe citation metadata and content
#      snippets are returned (see retrieval_tools.py).
#      ACL fields and retrieval scores are stripped.
#      Unauthorized nodes are indistinguishable from absent
#      content.

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, PrivateAttr

from llama_index.core.agent import FunctionAgent
from llama_index.core.tools import FunctionTool

from agents.retrieval_tools import create_retrieval_tools, AuthorizedEvidenceCollector
from retrieval.retriever import RetrievalService

logger = logging.getLogger("agent_workflow")


# ── Constants ─────────────────────────────────────────────

MAX_TOOL_ITERATIONS = 4
EXECUTION_TIMEOUT_SECONDS = 30

_AGENT_SYSTEM_PROMPT = """\
You are an enterprise knowledge retrieval router.

Your ONLY job is to decide which source retrieval tools to call
for a user's question. You do NOT answer the question yourself.

Available sources:
- Confluence  (search_confluence)  — wiki pages, documentation, runbooks
- Jira        (search_jira)         — issues, bugs, project tracking
- Slack       (search_slack)        — team discussions, decisions, announcements
- Google Drive(search_google_drive) — shared files, spreadsheets, presentations

Rules:
1. Call only the tools relevant to the user's question.
   You may call 1-4 tools per question.
2. Each tool call must include a natural-language search query
   that captures the user's intent for that source.
3. Do NOT attempt to change user identity, permissions, or
   connection ownership — you cannot and must not.
4. Do NOT fabricate results or answer the question yourself.
5. After receiving tool results, summarize which sources were
   searched and what was found.
"""


# ── Structured result model ───────────────────────────────


class AgentRetrievalResult(BaseModel):
    """
    Structured result returned by the agent workflow.

    Contains only authorized, display-safe information.
    No raw unauthorized search candidates or restricted
    metadata are included.
    """

    _authorized_nodes: list = PrivateAttr(default_factory=list)
    _tool_trace: list = PrivateAttr(default_factory=list)

    @property
    def authorized_nodes(self):
        """Server-only original NodeWithScore objects, never serialized to client."""
        return list(self._authorized_nodes)

    @property
    def tool_trace(self):
        return list(self._tool_trace)

    query: str = Field(
        description="The original user question."
    )
    selected_sources: List[str] = Field(
        default_factory=list,
        description=(
            "Source names the agent chose to search "
            "(e.g. confluence, jira)."
        ),
    )
    retrieved_nodes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Authorized, display-safe nodes from tool "
            "results. Each dict contains citation metadata "
            "and a content snippet."
        ),
    )
    tool_execution_count: int = Field(
        default=0,
        description=(
            "Number of tool executions performed during "
            "this retrieval attempt."
        ),
    )
    status: str = Field(
        default="pending",
        description=(
            "Retrieval attempt status: 'success', "
            "'no_results', 'timeout', 'error', or "
            "'max_iterations_reached'."
        ),
    )
    agent_response: str = Field(
        default="",
        description=(
            "Human-readable summary from the agent."
        ),
    )


# ── Agent workflow wrapper ───────────────────────────────


class RetrievalAgentWorkflow:
    """
    LlamaIndex FunctionAgent-based retrieval router.

    Wraps a FunctionAgent with the four existing source
    retrieval tools. The agent dynamically chooses which
    sources and search queries to use based on the
    user question.

    Usage:
        workflow = RetrievalAgentWorkflow(
            retrieval_service=svc,
            user_context=ctx,
        )
        result = await workflow.arun("What is the release plan?")
        # result: AgentRetrievalResult

    The agent LLM is OpenAI (tool-calling capable). Tencent
    ADP is NOT used for routing — it remains the answer
    generation model in the RAG pipeline.
    """

    def __init__(
        self,
        retrieval_service: RetrievalService,
        user_context: dict,
        llm: Optional[Any] = None,
        max_iterations: int = MAX_TOOL_ITERATIONS,
        timeout: float = EXECUTION_TIMEOUT_SECONDS,
    ):
        if not retrieval_service:
            raise ValueError(
                "retrieval_service is required."
            )
        if not user_context or "user_id" not in user_context:
            raise ValueError(
                "Authenticated user_context with user_id "
                "is required."
            )

        self.retrieval_service = retrieval_service
        self.user_context = user_context
        self.max_iterations = max_iterations
        self.timeout = timeout

        # ── Create the four retrieval tools ──────────────
        # user_context and retrieval_service are bound via
        # partial_params — the agent/LLM never sees them.
        self.evidence_collector = AuthorizedEvidenceCollector()
        self.tools: List[FunctionTool] = create_retrieval_tools(
            retrieval_service=retrieval_service,
            user_context=user_context,
            evidence_collector=self.evidence_collector,
        )

        # ── Resolve the routing LLM ──────────────────────
        # Use OpenAI (tool-calling capable) as the routing LLM.
        # Tencent ADP does NOT support tool-calling — it is a
        # text-in/text-out SSE endpoint and remains unchanged
        # for answer generation only.
        self.llm = llm or self._get_default_llm()

        # ── Create the FunctionAgent ──────────────────────
        self.agent = FunctionAgent(
            name="RetrievalRouter",
            description=(
                "Routes user questions to the appropriate "
                "enterprise source retrieval tools and "
                "collects authorized results."
            ),
            system_prompt=_AGENT_SYSTEM_PROMPT,
            tools=self.tools,
            llm=self.llm,
            timeout=timeout,
            early_stopping_method="generate",
            verbose=False,
        )

    @staticmethod
    def _get_default_llm():
        """
        Get the default OpenAI LLM for tool routing.

        Uses the OPENAI_API_KEY from environment. The model
        supports OpenAI function/tool calling, which is the
        protocol LlamaIndex FunctionAgent relies on.
        """
        from llama_index.llms.openai import OpenAI

        return OpenAI(
            model="gpt-4o-mini",
            temperature=0,
        )

    async def arun(self, query: str) -> AgentRetrievalResult:
        """
        Run the agent to route the query to retrieval tools.

        Enforces:
        - max_iterations (max 4 tool execution rounds)
        - timeout (30 seconds)
        - No identity/permission changes (system prompt)

        Returns:
            AgentRetrievalResult with selected sources,
            authorized retrieved nodes, tool execution
            count, and retrieval status.
        """
        if not query or not query.strip():
            return AgentRetrievalResult(
                query=query,
                status="error",
                agent_response="Query cannot be empty.",
            )

        try:
            handler = self.agent.run(
                user_msg=query,
                max_iterations=self.max_iterations,
            )

            # Await the handler with a timeout
            agent_output = await asyncio.wait_for(
                handler,
                timeout=self.timeout,
            )

        except asyncio.TimeoutError:
            logger.warning(
                "Agent routing timed out after %ss for "
                "query: %s",
                self.timeout,
                query[:100],
            )
            return AgentRetrievalResult(
                query=query,
                status="timeout",
                agent_response=(
                    "Retrieval routing timed out before "
                    "completing."
                ),
            )

        except Exception as exc:
            logger.error(
                "Agent routing error: %s", exc
            )
            return AgentRetrievalResult(
                query=query,
                status="error",
                agent_response=f"Agent error: {exc}",
            )

        # Trusted execution record, NOT agent-generated text or final tool_calls.
        nodes, calls = self.evidence_collector.snapshot()
        if not calls:
            return AgentRetrievalResult(query=query, status="error",
                                        agent_response="No retrieval tool was executed.")
        result = AgentRetrievalResult(
            query=query,
            selected_sources=list(dict.fromkeys(c["source"] for c in calls)),
            retrieved_nodes=[],
            tool_execution_count=len(calls),
            status="success" if nodes else "no_results",
            agent_response="Retrieval tools executed.",
        )
        result._authorized_nodes = nodes
        result._tool_trace = calls
        return result

    def _extract_results(
        self,
        query: str,
        agent_output: Any,
    ) -> AgentRetrievalResult:
        """
        Parse the agent's output into a structured result.

        The agent_output from FunctionAgent.run() is an
        AgentOutput (or StopEvent result) containing:
        - .response: ChatMessage with agent's summary
        - .tool_calls: list of ToolCallResult with
          tool_name, tool_kwargs, tool_output
        """
        selected_sources: List[str] = []
        retrieved_nodes: List[Dict[str, Any]] = []
        tool_execution_count = 0

        # Extract tool call results from the agent output.
        # The AgentOutput may carry tool_calls directly, or
        # they may be nested in the StopEvent result.
        tool_call_results = []

        # AgentOutput has .tool_calls (list of ToolCallResult)
        if hasattr(agent_output, "tool_calls"):
            tool_call_results = agent_output.tool_calls or []

        # ── Parse each tool call result ────────────────────
        for tc_result in tool_call_results:
            tool_name = getattr(
                tc_result, "tool_name", ""
            )

            # Map tool name to source name
            source = self._tool_name_to_source(tool_name)
            if source and source not in selected_sources:
                selected_sources.append(source)

            tool_execution_count += 1

            # Extract display-safe nodes from tool output
            tool_output = getattr(
                tc_result, "tool_output", None
            )
            if tool_output is None:
                continue

            # tool_output.content is a JSON string from
            # _build_tool_result in retrieval_tools.py
            content = ""
            if hasattr(tool_output, "content"):
                content = tool_output.content or ""
            elif isinstance(tool_output, str):
                content = tool_output

            if not content:
                continue

            # Parse the tool result JSON
            try:
                tool_result_dict = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                # Content may be plain text
                continue

            if not isinstance(tool_result_dict, dict):
                continue

            results_list = tool_result_dict.get(
                "results", []
            )
            for entry in results_list:
                retrieved_nodes.append(entry)

        # ── Determine status ───────────────────────────────
        agent_response_text = ""
        if hasattr(agent_output, "response"):
            response = agent_output.response
            if hasattr(response, "content"):
                agent_response_text = (
                    response.content or ""
                )
            elif isinstance(response, str):
                agent_response_text = response

        if retrieved_nodes:
            status = "success"
        elif tool_execution_count > 0:
            status = "no_results"
        else:
            status = "no_results"

        return AgentRetrievalResult(
            query=query,
            selected_sources=selected_sources,
            retrieved_nodes=retrieved_nodes,
            tool_execution_count=tool_execution_count,
            status=status,
            agent_response=agent_response_text,
        )

    @staticmethod
    def _tool_name_to_source(tool_name: str) -> str:
        """Map a retrieval tool name to its source name."""
        mapping = {
            "search_confluence": "confluence",
            "search_jira": "jira",
            "search_slack": "slack",
            "search_google_drive": "google_drive",
        }
        return mapping.get(tool_name, "")


# ── Sync convenience wrapper ──────────────────────────────


def run_retrieval_agent(
    query: str,
    retrieval_service: RetrievalService,
    user_context: dict,
    llm: Optional[Any] = None,
) -> AgentRetrievalResult:
    """
    Synchronous convenience wrapper for RetrievalAgentWorkflow.

    Creates a workflow, runs the agent, and returns the
    structured result. Intended for use in non-async contexts
    (e.g. the FastAPI /query endpoint).

    Args:
        query: User question.
        retrieval_service: The existing RetrievalService.
        user_context: Authenticated user's permission context.
        llm: Optional LLM override (must support tool calling).

    Returns:
        AgentRetrievalResult with authorized retrieved nodes.
    """
    workflow = RetrievalAgentWorkflow(
        retrieval_service=retrieval_service,
        user_context=user_context,
        llm=llm,
    )

    # Run the async workflow in an event loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If we're already in an async context, create
            # a new loop to avoid nested loop errors.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    workflow.arun(query),
                )
                return future.result()
    except RuntimeError:
        pass

    return asyncio.run(workflow.arun(query))
