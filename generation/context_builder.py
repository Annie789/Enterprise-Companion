# generation/context_builder.py

from typing import List
from llama_index.core.schema import NodeWithScore

# Fields that must NEVER appear in LLM context
# — these are internal ACL/permission fields
_ACL_FIELDS = {
    "allowed_groups",
    "restricted_to_users",
    "has_restrictions",
    "restriction_type",
    "connected_user_email",
    "security_level",
    "security_level_name",
    "security_level_id",
    "allowed_users",
    "channel_id",
    "is_private",
    "member_user_ids",
    "is_dm",
    "is_thread",
    "dm_participants",
    "accessible_by_users",
    "accessible_by_domains",
    "shared_drive_id",
    "parent_folder_ids",
    "is_public",
    "inherited_users",
    "inherited_domains",
    "user_id",
    "shared_with_users",
    "chunk_index",
    "total_chunks",
    "content_hash",
    # Source ID is included in the ACL block below — it must
    # not also appear in the display block.
    "source_id",
}

# Fields safe to include in the LLM context.
# Only display-relevant metadata is exposed.
_DISPLAY_FIELDS = {
    "source",
    "title",
    "url",
    "updated_at",
}


def build_context(
    nodes: List[NodeWithScore],
) -> str:
    """
    Build grounded context from retrieved nodes.

    ACL metadata is explicitly excluded from the
    context string — it must never reach the LLM.

    Only display fields are included:
    source, title, url, updated_at, content.

    Retrieval scores and source IDs are NOT included
    to avoid side-channel information leakage.
    """

    if not nodes:
        return ""

    context_blocks = []

    for index, result in enumerate(nodes, start=1):

        node     = result.node
        metadata = node.metadata or {}

        source     = metadata.get("source",     "unknown")
        title      = metadata.get("title",      "Untitled")
        updated_at = metadata.get("updated_at", "unknown")
        url        = metadata.get("url",        "")
        content    = node.get_content()

        # Build URL line only if URL exists
        url_line = f"URL: {url}\n" if url else ""

        block = f"""
[Source {index}]
Source: {source}
Title: {title}
{url_line}Updated At: {updated_at}

Content:
{content}
""".strip()

        context_blocks.append(block)

    return "\n\n".join(context_blocks)
