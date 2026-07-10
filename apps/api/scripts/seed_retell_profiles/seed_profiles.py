"""Seed + publish the migrated Retell agent profiles into the voice engine.

Bucket B apply step: reads the documents emitted by ``build_profiles.py`` and, for each,
creates (or reuses, by name) an ``agent_profiles`` row, writes the draft config, and
publishes a version. Idempotent — a profile whose live draft already equals the generated
config is left untouched, so re-running does not churn the version history.

Run against a DB (from ``apps/api``, with the API's env / ``DATABASE_URL`` in scope)::

    uv run python scripts/seed_retell_profiles/seed_profiles.py            # dry run (default)
    uv run python scripts/seed_retell_profiles/seed_profiles.py --apply    # write + publish
    uv run python scripts/seed_retell_profiles/seed_profiles.py --apply --set-defaults

``--set-defaults`` also points the direction defaults at the migrated agents (companion →
outbound, inbound → inbound). Leave it off during canary so the cutover controls routing
(see the cutover runbook §3). This opens sessions through the app's own session factory, so
the default-org tenant context is applied exactly as it is for the running API.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.session import get_engine, get_session_factory
from usan_api.repositories import agent_profiles as repo

_PROFILES_DIR = Path(__file__).parent / "profiles"

# Which migrated agent should back each direction default when --set-defaults is given.
# The other agents (sales, betty) are reached by per-contact assignment / profile override,
# never as a direction fallback.
_DIRECTION_DEFAULTS: dict[str, Literal["inbound", "outbound"]] = {
    "companion": "outbound",
    "inbound": "inbound",
}


async def _find_by_name(db: AsyncSession, name: str) -> Any | None:
    for p in await repo.list_profiles(db, channel="voice"):
        if p.name == name:
            return p
    return None


async def _seed_one(
    db: AsyncSession, doc: dict[str, Any], *, actor: str, apply: bool, set_defaults: bool
) -> str:
    name = doc["name"]
    config = doc["config"]
    existing = await _find_by_name(db, name)

    if existing is not None and existing.draft_config == config and existing.published_version:
        return f"= {doc['key']:<10} unchanged (v{existing.published_version})"
    if not apply:
        verb = "create+publish" if existing is None else "update+publish"
        return f"~ {doc['key']:<10} would {verb} ({len(config['tools']['external_tools'])} tools)"

    if existing is None:
        existing = await repo.create_profile(
            db, name=name, description=doc["description"], actor_email=actor
        )
    await repo.update_draft(
        db, existing.id, config=config, description=doc["description"], actor_email=actor
    )
    version = await repo.publish(db, existing.id, note="retell migration seed", actor_email=actor)
    tail = ""
    if set_defaults and doc["key"] in _DIRECTION_DEFAULTS:
        direction = _DIRECTION_DEFAULTS[doc["key"]]
        await repo.set_default(db, existing.id, direction=direction)
        tail = f"  default={direction}"
    return f"✓ {doc['key']:<10} published v{version.version}{tail}"


async def _run(actor: str, apply: bool, set_defaults: bool) -> None:
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(_PROFILES_DIR.glob("*.json"))]
    if not docs:
        raise SystemExit(f"no profile documents in {_PROFILES_DIR} — run build_profiles.py first")
    async with get_session_factory()() as db:
        for doc in docs:
            line = await _seed_one(db, doc, actor=actor, apply=apply, set_defaults=set_defaults)
            print(line)
        if apply:
            await db.commit()
    await get_engine().dispose()
    if not apply:
        print("\n(dry run — re-run with --apply to write. Add --set-defaults to flip routing.)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Write + publish (default is a dry run).")
    ap.add_argument(
        "--set-defaults",
        action="store_true",
        help="Also point direction defaults at the migrated agents (companion/inbound).",
    )
    ap.add_argument(
        "--actor",
        default="retell-migration-seed@usan.local",
        help="actor_email recorded on the created/published rows.",
    )
    args = ap.parse_args()
    asyncio.run(_run(args.actor, args.apply, args.set_defaults))


if __name__ == "__main__":
    main()
