from typing import List

from llama_index.core import Document
from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.node_parser import (
    SemanticSplitterNodeParser,
    SentenceSplitter,
)
from llama_index.core.schema import BaseNode


class TransformationService:

    def __init__(self, embed_model):

        self.semantic_pipeline = IngestionPipeline(
            transformations=[
                SemanticSplitterNodeParser(
                    embed_model=embed_model,
                    buffer_size=1,
                    breakpoint_percentile_threshold=95,
                )
            ]
        )

        self.sentence_pipeline = IngestionPipeline(
            transformations=[
                SentenceSplitter(
                    chunk_size=512,
                    chunk_overlap=50,
                )
            ]
        )

    def _use_semantic_splitter(
        self,
        document: Document,
    ) -> bool:

        source = document.metadata.get(
            "source",
            "",
        ).lower()

        return source in {
            "confluence",
            "google_drive",
        }

    def transform(
        self,
        documents: List[Document],
    ) -> List[BaseNode]:

        semantic_documents = []
        sentence_documents = []

        for document in documents:

            if self._use_semantic_splitter(document):
                semantic_documents.append(document)
            else:
                sentence_documents.append(document)

        nodes = []

        if semantic_documents:
            nodes.extend(
                self.semantic_pipeline.run(
                    documents=semantic_documents,
                    show_progress=True,
                )
            )

        if sentence_documents:
            nodes.extend(
                self.sentence_pipeline.run(
                    documents=sentence_documents,
                    show_progress=True,
                )
            )

        for i, node in enumerate(nodes):
            node.metadata["chunk_index"] = i

        return nodes