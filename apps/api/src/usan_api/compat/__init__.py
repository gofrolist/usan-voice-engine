"""RetellAI-compatible public API surface (feature 003-retellai-api-parity).

An additive, isolated subpackage: a mounted FastAPI sub-application that exposes
RetellAI-shaped endpoints so a CRM built on RetellAI migrates by repointing its base
URL + API key with no integration-code changes. It authenticates a static Bearer key
against the global ``compat_api_keys`` table, opens the org-scoped Postgres RLS session,
and reuses the native call / contact / DNC / agent-profile / batch / webhook services —
translating identifiers, timestamps (-> ms), status values, and the error envelope at
the edge. The native ``/v1`` plane is never modified. See specs/003-retellai-api-parity/.
"""
