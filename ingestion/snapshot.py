# ingestion/snapshot.py

"""
Snapshot utility for saving documents to JSON.
"""
import json
from pathlib import Path


def save_documents_to_json(
    documents,
    output_file
):

    output_path = Path(output_file)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    data = []

    for document in documents:
        data.append({
            "id": document.id_,
            "text": document.text,
            "metadata": document.metadata
        })

    with open(output_path, "w", encoding="utf-8") as file:

        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False,
            default=str
        )