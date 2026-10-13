# agents/__init__.py
#
# LlamaIndex agent framework integration for the
# Enterprise RAG application.
#
# This package exposes four retrieval tools that an
# agent can select from. Each tool reuses the existing
# RetrievalService and permission filter — no separate
# vector indexes are created.
#
# Security model:
#   - User identity and authorization context are
#     injected by trusted application code via
#     FunctionTool partial_params. They are NEVER
#     accepted as model-selected tool arguments.
#   - Only display-safe citation metadata is returned.
#   - ACL details and unauthorized document information
#     are never exposed in tool results.

from agents.retrieval_tools import (
    create_retrieval_tools,
    search_confluence,
    search_jira,
    search_slack,
    search_google_drive,
)
from agents.agent_workflow import (
    RetrievalAgentWorkflow,
    AgentRetrievalResult,
    run_retrieval_agent,
)

__all__ = [
    "create_retrieval_tools",
    "search_confluence",
    "search_jira",
    "search_slack",
    "search_google_drive",
    "RetrievalAgentWorkflow",
    "AgentRetrievalResult",
    "run_retrieval_agent",
]
