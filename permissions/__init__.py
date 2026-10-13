# permissions/__init__.py
"""
Task 4 — Live Permission Change Handling

Public API:
  - PermissionReconciliationService (reconcile)
  - mock_acl_store (ACL modification + revocation registry)
"""

from permissions.mock_acl_store import (
    register_revocation,
    clear_revocation,
    is_revoked,
    get_all_revoked,
    get_mock_document,
    get_all_mock_documents,
    revoke_confluence_access,
    grant_confluence_access,
    revoke_jira_access,
    grant_jira_access,
    revoke_slack_access,
    grant_slack_access,
    revoke_drive_access,
    grant_drive_access,
    delete_mock_document,
    restore_mock_document,
)
from permissions.permission_reconciliation import (
    reconciliation_service,
    PermissionReconciliationService,
)
