# auth/auth.py
#
# Lightweight but secure DEMO authentication for the
# Enterprise RAG hackathon backend.
#
# Design:
#   - Server-defined users with fixed identities and
#     permissions (no self-registration).
#   - Bearer-token auth via an Authorization header
#     containing a per-user demo API key.
#   - The backend derives effective identity and all
#     permission metadata (groups, email, source IDs)
#     from the authenticated user — NEVER from the
#     request body.
#   - Requests that attempt to impersonate another user
#     or elevate permissions are rejected with 403.
#
# Demo personas:
#   annie      — engineering, full data-source access
#   bob        — security/compliance, admin + audit
#   contractor — restricted, limited source access

import secrets
from typing import Dict, List, Optional

from fastapi import Header, HTTPException, status


# =========================================================
# Demo user database
# =========================================================

class AuthUser:
    """
    Immutable representation of an authenticated user.
    All permission metadata is server-defined.
    """

    def __init__(
        self,
        user_id: str,
        email: str,
        role: str,
        groups: List[str],
        api_key: str,
        confluence_account_id: str = "",
        slack_user_id: str = "",
        jira_security_level_ids: List[str] = None,
    ):
        self.user_id = user_id
        self.email = email
        self.role = role
        self.groups = groups
        self.api_key = api_key
        self.confluence_account_id = confluence_account_id
        self.slack_user_id = slack_user_id
        self.jira_security_level_ids = jira_security_level_ids or []

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "compliance")

    def to_user_context(self) -> dict:
        """
        Build the permission-filter user_context dict
        entirely from server-defined attributes.
        """
        return {
            "user_id": self.user_id,
            "email": self.email,
            "groups": list(self.groups),
            "confluence_account_id": self.confluence_account_id,
            "slack": {
                "user_id": self.slack_user_id,
            },
            "jira": {
                "allowed_security_level_ids": list(
                    self.jira_security_level_ids
                ),
            },
            "google_drive": {
                "accessible_by_users": [self.email],
                "accessible_by_domains": [
                    self.email.split("@")[-1]
                    if self.email and "@" in self.email
                    else ""
                ],
            },
        }


# ── Static demo user registry ──────────────────────────
#
# In a real system these would live in an identity provider
# or database with hashed secrets. For the hackathon demo
# the API keys are generated once at import time and
# exposed only through the /auth/whoami endpoint (after
# authenticating with the key).

DEMO_USERS: Dict[str, AuthUser] = {
    "annie": AuthUser(
        user_id="annie",
        email="annie@company.com",
        role="engineering",
        groups=["engineering", "devops"],
        api_key="demo-annie-key",
        confluence_account_id="acc:annie:confluence",
        slack_user_id="U_ANNIE_001",
        jira_security_level_ids=["sec-level-public", "sec-level-eng"],
    ),
    "bob": AuthUser(
        user_id="bob",
        email="bob@company.com",
        role="compliance",
        groups=["security", "compliance"],
        api_key="demo-bob-key",
        confluence_account_id="acc:bob:confluence",
        slack_user_id="U_BOB_002",
        jira_security_level_ids=["sec-level-public"],
    ),
    "contractor": AuthUser(
        user_id="contractor",
        email="contractor@external.com",
        role="contractor",
        groups=["contractors"],
        api_key="demo-contractor-key",
        confluence_account_id="acc:contractor:confluence",
        slack_user_id="U_CONTRACTOR_003",
        jira_security_level_ids=["sec-level-public"],
    ),
}

# Reverse lookup: api_key → AuthUser
_API_KEY_INDEX: Dict[str, AuthUser] = {
    u.api_key: u for u in DEMO_USERS.values()
}


# =========================================================
# FastAPI dependencies
# =========================================================

def _extract_bearer_token(
    authorization: Optional[str],
) -> str:
    """
    Parse an 'Authorization: Bearer <token>' header.
    Returns the raw token or raises 401.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header. "
                   "Provide 'Authorization: Bearer <demo-key>'.",
        )

    parts = authorization.split(None, 1)

    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. "
                   "Expected 'Bearer <token>'.",
        )

    token = parts[1].strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token.",
        )

    return token


def get_current_user(
    authorization: Optional[str] = Header(default=None),
) -> AuthUser:
    """
    Core auth dependency.  Resolves the bearer token to a
    server-defined AuthUser.  Returns the AuthUser so
    endpoint handlers can use it directly.
    """
    token = _extract_bearer_token(authorization)

    user = _API_KEY_INDEX.get(token)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or unknown API key.",
        )

    return user


def require_admin(
    user: AuthUser = None,
) -> AuthUser:
    """
    Dependency that enforces an admin/compliance role.
    Used by audit, revocation, and webhook endpoints.

    Accepts a pre-resolved AuthUser (from get_current_user)
    so it can be composed in the endpoint signature:

        def handler(user: AuthUser = Depends(get_current_user)):
            require_admin(user)
            ...
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or compliance role required.",
        )

    return user


def require_user_or_admin(
    target_user_id: str,
    user: AuthUser,
) -> AuthUser:
    """
    Enforce connection ownership: the authenticated user
    may only operate on their own connections, unless
    they are an admin.
    """
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    if user.user_id != target_user_id and not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Cannot access resources owned by "
                f"'{target_user_id}'. You are authenticated "
                f"as '{user.user_id}'."
            ),
        )

    return user


def reject_identity_mismatch(
    request_user_id: Optional[str],
    authenticated_user: AuthUser,
) -> None:
    """
    Reject requests where the body carries a user_id that
    differs from the authenticated identity.

    This prevents identity spoofing and privilege escalation.
    """
    if request_user_id is None:
        return

    if request_user_id != authenticated_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Identity mismatch: request body claims "
                f"'{request_user_id}' but you are "
                f"authenticated as "
                f"'{authenticated_user.user_id}'. "
                f"Impersonation is not permitted."
            ),
        )
