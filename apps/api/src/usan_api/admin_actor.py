"""Actor identity for admin mutations.

P1 has no per-user login (routes are operator-token-guarded), so attribution
records a fixed sentinel. P3 (Google SSO) swaps the body to return the
authenticated session email; callers and the audit log are unchanged.
"""

# Sentinel used until SSO lands (P3). Distinct from a real email so audit rows
# created pre-SSO are obvious.
OPERATOR_ACTOR = "operator-token"


def get_actor_email() -> str:
    return OPERATOR_ACTOR
