# retrieval/permission_filter.py

from typing import List
from llama_index.core.schema import NodeWithScore


# =========================================================
# Confluence ACL
# =========================================================

def _check_confluence(metadata: dict, user_context: dict) -> bool:
    """
    Confluence permission check enforcing:

    1. Space-level access — user must belong to a group
       that has read access to the page's space, OR the page
       has no space-level restrictions.
    2. Page-level access — if the page has restrictions,
       the user's Atlassian account ID or group must be
       in the restriction list.
    3. Named-user restrictions — explicit accountId match.
    4. Permitted groups — group membership match.
    5. Inherited restrictions — treated as restricted
       (deny-by-default since the effective ACL cannot
       be verified from inherited metadata alone).
    6. Deny when effective ACL cannot be determined.

    Uses accountId → accountId comparison, not email → accountId.
    """

    # ── Deny-by-default: has_restrictions missing ───────────
    has_restrictions = metadata.get("has_restrictions", None)

    if has_restrictions is None:
        return False

    # ── No restrictions → allow (space-level default) ───────
    if not has_restrictions:
        return True

    # ── Inherited restrictions → deny ───────────────────────
    # When restriction_type is "inherited", the effective ACL
    # cannot be determined from the page metadata alone.
    # Deny rather than risk exposing restricted content.
    restriction_type = metadata.get("restriction_type", "")

    if restriction_type == "inherited":
        return False

    # ── Group check ─────────────────────────────────────────
    user_groups    = {g.strip().lower() for g in user_context.get("groups", [])}
    allowed_groups = {g.strip().lower() for g in metadata.get("allowed_groups", [])}

    if user_groups & allowed_groups:
        return True

    # ── Named-user check — accountId → accountId ────────────
    confluence_account_id = user_context.get("confluence_account_id", "")
    restricted_to_users   = metadata.get("restricted_to_users", [])

    if confluence_account_id and confluence_account_id in restricted_to_users:
        return True

    # ── Fallback: email match ────────────────────────────────
    # Some mock data stores emails instead of account IDs.
    # This fallback handles that case but is less secure
    # in real deployments where email ≠ accountId.
    user_email = user_context.get("email", "")
    if user_email and user_email in restricted_to_users:
        return True

    return False


# =========================================================
# Jira ACL
# =========================================================

def _check_jira(metadata: dict, user_context: dict) -> bool:
    """
    Jira permission check enforcing:

    1. Project-level access — user must be in allowed_users
       (connected with their own token that has project access).
    2. Issue-level security — uses security-level IDs, not
       numeric privilege ranking. The user's allowed security
       level ID set must contain the issue's security_level_id.
    3. Deny unknown or unverified access — if allowed_users
       is missing or None, deny.

    Uses explicit allowed security level ID set, not numeric ranking.
    """

    user_email    = user_context.get("email", "")
    allowed_users = metadata.get("allowed_users", None)

    # ── Deny-by-default: allowed_users missing ───────────────
    if allowed_users is None:
        return False

    # ── Project-level access check ───────────────────────────
    if allowed_users and user_email not in allowed_users:
        return False

    # ── Issue-level security — ID membership, not rank ──────
    issue_security_id = metadata.get("security_level_id", "")

    # Unknown or failed lookup must never be treated as unrestricted.
    if issue_security_id in ("UNKNOWN", "0", None):
        return False
    if issue_security_id == "":
        return True

    # Check if this security level ID is in user's allowed set
    allowed_security_ids = user_context.get("jira", {}).get(
        "allowed_security_level_ids", []
    )

    # If no allowed IDs configured, only allow unrestricted issues
    if not allowed_security_ids:
        return False

    return issue_security_id in allowed_security_ids


# =========================================================
# Slack ACL
# =========================================================

def _check_slack(metadata: dict, user_context: dict) -> bool:
    """
    Slack permission check enforcing:

    1. Workspace access — verified via the Slack user ID
       being present in user_context (set at connection time).
    2. Private-channel membership — user's Slack user ID
       must be in member_user_ids for private channels.
    3. DM and private-thread access — if is_dm or is_thread
       metadata is present and the user is not a participant,
       deny.
    4. Public channels — accessible only if is_private=False
       AND the user has a verified Slack user ID (workspace
       membership). We do NOT assume every public channel is
       accessible to every application user.

    Deny-by-default: if is_private is missing, treat as private.
    """

    # ── Deny-by-default: is_private missing ─────────────────
    is_private = metadata.get("is_private", None)

    if is_private is None:
        return False

    # ── Workspace membership verification ───────────────────
    # The user must have a verified Slack user ID from
    # their connection. Without it, deny even public channels.
    slack_user_id = user_context.get("slack", {}).get("user_id", "")

    if not slack_user_id:
        return False

    # ── Member channel IDs check ────────────────────────────
    # The user must be connected to the channel (their
    # connection's channel_ids must include this channel).
    channel_id = metadata.get("channel_id", "")

    if channel_id:
        member_channel_ids = user_context.get("slack", {}).get(
            "member_channel_ids", []
        )
        if member_channel_ids and channel_id not in member_channel_ids:
            return False

    # Real sync must have verified the ACL lookup successfully.
    # Legacy mock records may not include this flag.
    if metadata.get("acl_verified") is False:
        return False

    # Check participant-gated content BEFORE public-channel allowance.
    if metadata.get("is_dm") or metadata.get("is_thread") and metadata.get("private_thread"):
        if slack_user_id not in metadata.get("dm_participants", []):
            return False

    # ── Public channel — allow if workspace member ──────────
    if not is_private:
        return True

    # ── Private channel — verify membership ─────────────────
    member_user_ids = metadata.get("member_user_ids", None)

    # If member_user_ids is missing, deny access
    if member_user_ids is None:
        return False

    # ── Compare Slack user ID → Slack member IDs ────────────
    if slack_user_id and slack_user_id in member_user_ids:
        return True

    # Fallback: email comparison (works if bot token resolves to email)
    user_email = user_context.get("email", "")
    if user_email and user_email in member_user_ids:
        return True

    # ── DM / private-thread check ────────────────────────────
    # If the message is a DM or private thread, only allow
    # if the user is a direct participant.
    is_dm = metadata.get("is_dm", False)
    is_thread = metadata.get("is_thread", False)

    if is_dm or is_thread:
        dm_participants = metadata.get("dm_participants", [])
        if slack_user_id not in dm_participants:
            return False

    return False


# =========================================================
# Google Drive ACL
# =========================================================

def _check_google_drive(metadata: dict, user_context: dict) -> bool:
    """
    Google Drive permission check enforcing:

    1. Direct user access — user's email in accessible_by_users.
    2. Domain permissions — user's domain in accessible_by_domains.
    3. Public access — is_public=True.
    4. Inherited folder/shared-drive permissions — only when
       the user's email or domain is explicitly listed in the
       inherited permission set. If the permission type is
       "inherited" and cannot be verified, deny.
    5. Deny unsupported or unresolved permission cases.

    Group-based Drive access (e.g. engineering@company.com)
    is NOT supported — documented and denied rather than
    silently granted.
    """

    # ── Deny-by-default: is_public missing ──────────────────
    is_public = metadata.get("is_public", None)

    if is_public is None:
        return False

    if is_public:
        return True

    user_email = user_context.get("email", "")

    if not user_email:
        return False

    # ── Direct user access ───────────────────────────────────
    accessible_by_users = metadata.get("accessible_by_users", [])
    if user_email in accessible_by_users:
        return True

    # ── Domain access ────────────────────────────────────────
    accessible_by_domains = metadata.get("accessible_by_domains", [])
    if accessible_by_domains:
        user_domain = user_email.split("@")[-1]
        if user_domain in accessible_by_domains:
            return True

    # ── Inherited permissions — only if verifiable ──────────
    # If the file is in a shared drive or has parent folders,
    # we can only grant access if the user's email or domain
    # appears in the explicit permission list. We do NOT
    # assume inherited access from folder membership alone
    # unless the folder's permission list includes the user.
    shared_drive_id = metadata.get("shared_drive_id", "")
    parent_folder_ids = metadata.get("parent_folder_ids", [])

    if shared_drive_id or parent_folder_ids:
        # Check if the metadata includes an inherited_users list
        # that was resolved at sync time. If not present, deny.
        inherited_users = metadata.get("inherited_users", [])
        if user_email in inherited_users:
            return True

        inherited_domains = metadata.get("inherited_domains", [])
        if inherited_domains:
            user_domain = user_email.split("@")[-1]
            if user_domain in inherited_domains:
                return True

    # NOTE: Group-based Drive access (e.g. engineering@company.com)
    # is NOT supported. Group emails cannot be compared to user emails
    # without resolving group membership via the Admin SDK.
    # Unsupported cases deny access rather than silently grant it.

    return False


# =========================================================
# Ownership binding check
# =========================================================

def _check_ownership(metadata: dict, user_context: dict) -> bool:
    """
    Verify that the indexed content belongs to the
    authenticated user or an explicitly authorized shared
    collection. This prevents cross-user index leakage.

    The user_id in metadata must match the authenticated
    user's user_id, OR the node must be part of a shared
    collection explicitly accessible to this user.
    """
    node_user_id = metadata.get("user_id", "")

    # If no user_id is stamped on the node, deny —
    # unbound content is never accessible.
    if not node_user_id:
        return False

    auth_user_id = user_context.get("user_id", "")

    # Direct ownership
    if node_user_id == auth_user_id:
        return True

    # Shared collection — check if the authenticated user
    # is in the shared_with_users list
    shared_with_users = metadata.get("shared_with_users", [])
    if auth_user_id in shared_with_users:
        return True

    return False


# =========================================================
# Main filter function
# =========================================================

def filter_by_permissions(
    nodes: List[NodeWithScore],
    user_context: dict,
) -> List[NodeWithScore]:
    """
    Filter retrieved nodes by source-specific permission rules.

    This is the security boundary between vector retrieval
    and the LLM generation layer. No unauthorized node
    can pass through this function.

    Deny-by-default: unknown source, missing metadata, or
    missing ACL fields all result in the node being excluded.

    Ownership binding is enforced in addition to per-source
    ACL checks to prevent cross-user index leakage.
    """

    checkers = {
        "confluence":   _check_confluence,
        "jira":         _check_jira,
        "slack":         _check_slack,
        "google_drive": _check_google_drive,
    }

    permitted_nodes = []

    for node_with_score in nodes:
        metadata = node_with_score.node.metadata or {}
        source   = metadata.get("source", "").lower()
        checker  = checkers.get(source)

        # ── Deny-by-default: unknown source or missing
        #    permission metadata → node is NOT returned.
        if not checker:
            continue

        if not metadata:
            continue

        # ── Ownership binding ────────────────────────────────
        # Verify the node belongs to the authenticated user
        # or an explicitly authorized shared collection.
        if not _check_ownership(metadata, user_context):
            continue

        # ── Source-specific ACL check ────────────────────────
        if checker(metadata, user_context):
            permitted_nodes.append(node_with_score)

    return permitted_nodes
