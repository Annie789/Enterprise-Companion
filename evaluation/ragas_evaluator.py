"""RAGAS collections metrics; only caller-authorized text may be passed here.

Requires: ragas (collections API), openai, OPENAI_API_KEY.
Scores are quality signals, NEVER authorization decisions.
"""
import os
from typing import Any, Optional
from openai import AsyncOpenAI
import logging

from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    raise RuntimeError(
        "OPENAI_API_KEY not found. "
        "Check your companion_app/.env file."
    )

client = AsyncOpenAI(api_key=api_key)

logger = logging.getLogger(__name__)

class RagasEvaluator:
    def __init__(self, llm_model: Optional[str] = None, embedding_model: Optional[str] = None):
        from ragas.llms import llm_factory
        from ragas.embeddings.base import embedding_factory
        from ragas.metrics.collections import ContextRelevance, Faithfulness, AnswerRelevancy
        client = AsyncOpenAI()
        llm = llm_factory(llm_model or os.getenv('RAGAS_LLM_MODEL','gpt-4o-mini'), client=client)
        embeddings = embedding_factory('openai', model=embedding_model or os.getenv('RAGAS_EMBED_MODEL','text-embedding-3-small'), client=client)
        self._context = ContextRelevance(llm=llm)
        self._faithfulness = Faithfulness(llm=llm)
        self._answer = AnswerRelevancy(llm=llm, embeddings=embeddings)

    @staticmethod
    def _score(result: Any) -> Optional[float]:
        try:
            value = float(result.value)
            return value if 0.0 <= value <= 1.0 else None
        except (AttributeError, TypeError, ValueError):
            return None

    async def context_relevance(self, question: str, contexts: list[str]) -> dict:
        if not contexts:
            return {'score': None, 'status': 'no_context'}
        try:
            result = await self._context.ascore(user_input=question, retrieved_contexts=contexts)
            score = self._score(result)
            return {'score': score, 'status': 'ok' if score is not None else 'invalid_score'}
        except Exception as exc:
            return {'score':None, 'status':'error', 'error_type':type(exc).__name__}

    async def answer_quality(self, question: str, contexts: list[str], answer: str) -> dict:
        if not contexts or not answer:
            return {'faithfulness':None, 'answer_relevancy':None, 'status':'no_context'}
        try:
            faith = await self._faithfulness.ascore(user_input=question, retrieved_contexts=contexts, response=answer)
        except Exception:
            logger.exception("RAGAS Faithfulness evaluation failed")
            faith = None
        try:
            relevancy = await self._answer.ascore(user_input=question, response=answer)
        except Exception:
            logger.exception("RAGAS Answer Relevancy evaluation failed")
            relevancy = None
        f, r = self._score(faith), self._score(relevancy)
        return {'faithfulness':f, 'answer_relevancy':r, 'status':'ok' if f is not None and r is not None else 'partial_failure'}
