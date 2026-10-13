"""Mandatory agent-orchestrated RAG, with fail-closed tools and RAGAS triad."""
import asyncio
import logging
import os
import uuid
from typing import Any, Dict

from audit.audit_logger import audit_logger
from generation.context_builder import build_context
from generation.citations import build_citations
from retrieval.permission_filter import filter_by_permissions
from retrieval.reranker import rerank_nodes
from agents.agent_workflow import run_retrieval_agent

logger = logging.getLogger(__name__)
_NO_RESULTS = "I don't have information on that topic in the available sources."
_ERROR = "Unable to process the query."


def _run(coro):
    return asyncio.run(coro)


class RAGPipeline:
    def __init__(self, retrieval_service, adp_client):
        self.retrieval_service = retrieval_service
        self.adp_client = adp_client

    def query(self, query: str, user_context: dict) -> Dict[str, Any]:
        user_id = user_context["user_id"]
        email = user_context.get("email", "")
        query_id = str(uuid.uuid4())
        info = {"enabled": True, "selected_sources": [], "tool_execution_count": 0,
                "tool_trace": [], "retrieval_attempts": 1, "retried": False,
                "context_relevance": None, "faithfulness": None,
                "answer_relevancy": None, "evaluation_status": "pending",
                "post_evaluation_status": "pending"}

        def record(outcome, answer, ids=None, decisions=None, citations=None, error_type=None):
            detail = {"retrieved_doc_ids": ids or [], "auth_decisions": decisions or [],
                      "final_answer": answer, "citations": citations or [],
                      "query_outcome": outcome.lower(), "agentic": info}
            if error_type:
                detail["error_type"] = error_type
            audit_logger.log(event_type="QUERY", user_id=user_id, user_email=email,
                             query=query, query_id=query_id, outcome=outcome, detail=detail)
            public_status = {"SUCCESS": "success", "DENIED": "no_context"}.get(outcome, "error")
            return {"query": query, "answer": answer, "citations": citations or [],
                    "agentic": info, "status": public_status}

        try:
            # Mandatory orchestration. Never fall back to non-agent retrieval.
            result = run_retrieval_agent(query, self.retrieval_service, user_context)
            info["selected_sources"] = result.selected_sources
            info["tool_execution_count"] = result.tool_execution_count
            info["tool_trace"] = result.tool_trace
            if result.status not in ("success", "no_results"):
                info["evaluation_status"] = "routing_failed"
                info["post_evaluation_status"] = "not_run"
                return record("ERROR", _ERROR, error_type="AgentRoutingFailure")

            # Recheck all evidence against current effective user permissions.
            nodes = filter_by_permissions(nodes=result.authorized_nodes,
                                          user_context=user_context)
            unique = {}
            for n in nodes:
                meta = n.node.metadata or {}
                key = (meta.get("source"), meta.get("source_id"), n.node.node_id)
                unique[key] = n
            nodes = rerank_nodes(list(unique.values()),
                                 top_k=self.retrieval_service.final_top_k)
            if not nodes:
                info["evaluation_status"] = "no_context"
                info["post_evaluation_status"] = "no_context"
                return record("DENIED", _NO_RESULTS)

            ids = []
            decisions = []
            for n in nodes:
                meta = n.node.metadata or {}
                source, sid = meta.get("source", ""), meta.get("source_id", "")
                ids.append(sid)
                decisions.append({"source": source, "source_id": sid, "decision": "ALLOWED"})
                audit_logger.log(event_type="QUERY", user_id=user_id, user_email=email,
                                 query=query, query_id=query_id, source=source,
                                 resource_id=sid, outcome="ALLOWED")

            contexts = [n.node.get_content() for n in nodes]

            evaluator = None

            if os.getenv(
                "RAG_EVALUATION_ENABLED", "true"
            ).lower() == "true":

                try:
                    from evaluation.ragas_evaluator import RagasEvaluator

                    evaluator = RagasEvaluator()

                    logger.info("RAGAS evaluator initialized successfully")

                except Exception:
                    logger.exception("RAGAS initialization failed")

                    info["evaluation_status"] = "unavailable"
                    info["post_evaluation_status"] = "unavailable"

                if evaluator is not None:
                    try:
                        metric = _run(
                            evaluator.context_relevance(query, contexts)
                        )

                        info["context_relevance"] = metric.get("score")
                        info["evaluation_status"] = metric.get(
                            "status", "error"
                        )

                        if metric.get("error_type"):
                            logger.error(
                                "Context evaluation error type: %s",
                                metric["error_type"]
                            )

                    except Exception:
                        logger.exception("RAGAS context evaluation failed")
                        info["evaluation_status"] = "unavailable"

            else:
                info["evaluation_status"] = "disabled"
                info["post_evaluation_status"] = "disabled"

            answer = self.adp_client.generate(query=query, context=build_context(nodes))
            citations = build_citations(nodes)
            if evaluator is not None:
                try:
                    metric = _run(evaluator.answer_quality(query, contexts, answer))
                    info["faithfulness"] = metric.get("faithfulness")
                    info["answer_relevancy"] = metric.get("answer_relevancy")
                    info["post_evaluation_status"] = metric.get("status", "error")
                except Exception as exc:
                    logger.warning("RAGAS answer evaluation failed: %s", type(exc).__name__)
                    info["post_evaluation_status"] = "unavailable"

            # Evaluation scores measure quality, never authorization.
            return record("SUCCESS", answer, ids, decisions, citations)
        except Exception as exc:
            logger.exception("Agentic query failed: %s", type(exc).__name__)
            info["evaluation_status"] = "error"
            info["post_evaluation_status"] = "not_run"
            return record("ERROR", _ERROR, error_type=type(exc).__name__)
