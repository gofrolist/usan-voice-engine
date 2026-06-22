"""Repository for compat_api_keys — the RetellAI-compatible bearer keys (feature 003).

GLOBAL, non-RLS table (like invitations / admin_users): rows are filtered by app code on
organization_id, and a key is resolved BEFORE any org context exists. The token is shown
once at create and stored only as a sha256 hash + an 8-char plaintext prefix for lookup.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import CompatApiKey

_TOKEN_BYTES = 32
_PREFIX_LEN = 8
_TOKEN_PREFIX = "key_"  # noqa: S105 - a public token prefix (display/lookup), not a secret


def hash_token(token: str) -> str:
    """sha256-hex of the full token (the stored secret). No KDF: the token is 256-bit random."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class IssuedKey:
    """A freshly created key row + its one-time plaintext token (never re-readable)."""

    row: CompatApiKey
    token: str


async def create(db: AsyncSession, *, organization_id: uuid.UUID, label: str | None) -> IssuedKey:
    """Issue a new active key for an org; returns the row + plaintext token (once)."""
    token = _TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
    row = CompatApiKey(
        organization_id=organization_id,
        key_prefix=token[:_PREFIX_LEN],
        key_hash=hash_token(token),
        status="active",
        label=label,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return IssuedKey(row=row, token=token)


async def list_for_org(db: AsyncSession, organization_id: uuid.UUID) -> list[CompatApiKey]:
    """All keys for an org, newest first (plaintext tokens are never returned by list)."""
    result = await db.execute(
        select(CompatApiKey)
        .where(CompatApiKey.organization_id == organization_id)
        .order_by(CompatApiKey.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke(
    db: AsyncSession, key_id: uuid.UUID, organization_id: uuid.UUID
) -> CompatApiKey | None:
    """Revoke a key (one-way). Org-scoped so an admin can't revoke another org's key;
    returns None when the key is missing / already revoked / belongs to another org."""
    row = await db.get(CompatApiKey, key_id)
    if row is None or row.organization_id != organization_id or row.status != "active":
        return None
    row.status = "revoked"
    row.revoked_at = datetime.now(UTC)
    await db.flush()
    return row


async def lookup_active_by_prefix(db: AsyncSession, prefix: str) -> list[CompatApiKey]:
    """Active candidates sharing a token prefix — the O(1) auth lookup (hash-compared next)."""
    result = await db.execute(
        select(CompatApiKey).where(
            CompatApiKey.key_prefix == prefix,
            CompatApiKey.status == "active",
        )
    )
    return list(result.scalars().all())


async def touch_last_used(db: AsyncSession, key_id: uuid.UUID) -> None:
    """Best-effort last-used stamp. The caller owns the commit."""
    row = await db.get(CompatApiKey, key_id)
    if row is not None:
        row.last_used_at = datetime.now(UTC)
        await db.flush()
