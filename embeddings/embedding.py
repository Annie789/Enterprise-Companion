# embeddings/embedding.py

import os
from pathlib import Path

from dotenv import load_dotenv
from llama_index.embeddings.openai import OpenAIEmbedding


# Project root: enterprise_app/
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from enterprise_app/.env
load_dotenv(dotenv_path=BASE_DIR / ".env")


DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def get_embedding_model() -> OpenAIEmbedding:
    """
    Initialize and return the OpenAI embedding model.

    Required environment variable:
        OPENAI_API_KEY

    Optional environment variable:
        OPENAI_EMBEDDING_MODEL

    Default:
        text-embedding-3-small
    """

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError(
            "OPENAI_API_KEY is not configured. "
            "Add OPENAI_API_KEY to the project .env file."
        )

    model_name = os.getenv(
        "OPENAI_EMBEDDING_MODEL",
        DEFAULT_EMBEDDING_MODEL,
    )

    return OpenAIEmbedding(
        model=model_name,
        api_key=api_key,
    )


def test_embedding_model() -> None:
    """
    Test OpenAI embedding connectivity.

    Generates an embedding for a sample sentence
    and prints basic information without exposing
    the API key.
    """

    embed_model = get_embedding_model()

    test_text = "Network troubleshooting guide"

    embedding = embed_model.get_text_embedding(
        test_text
    )

    model_name = os.getenv(
        "OPENAI_EMBEDDING_MODEL",
        DEFAULT_EMBEDDING_MODEL,
    )

    print(
        f"Embedding model: {model_name}"
    )

    print(
        f"Embedding dimensions: {len(embedding)}"
    )

    print(
        f"First 5 values: {embedding[:5]}"
    )


if __name__ == "__main__":
    test_embedding_model()