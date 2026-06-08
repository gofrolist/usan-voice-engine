"""Actor identity for admin mutations.

P3 (Google SSO) wires this to the authenticated session: the audit log records the
operator's verified email.
"""

from fastapi import Depends

from usan_api.auth import AdminPrincipal, require_admin_session


def get_actor_email(principal: AdminPrincipal = Depends(require_admin_session)) -> str:
    return principal.email
