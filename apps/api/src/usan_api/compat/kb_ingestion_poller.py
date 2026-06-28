"""KB ingestion poller (Phase 5). Cross-org: a SECURITY DEFINER claim returns (kb_id, org_id)
across all orgs (the shared usan_app session is otherwise default-org-pinned); each KB is then
processed in its OWN short transaction under set_tenant_context(org_id). Each KB is processed in
its own short transaction; that session holds a pooled connection across the embed call —
acceptable for the single-replica, flag-gated deployment (a connection-release refactor is a
documented follow-up). Mirrors retry_orchestrator's loop discipline."""

from __future__ import annotations

import asyncio
import contextlib

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api.compat import kb_ingestion
from usan_api.db.session import get_session_factory
from usan_api.repositories import knowledge_bases as repo
from usan_api.settings import Settings
from usan_api.tenant_context import set_tenant_context


async def poll_once(factory: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    async with factory() as db:
        claimed = await repo.claim_pending(
            db,
            limit=settings.kb_ingestion_batch_size,
            lease_seconds=settings.kb_ingestion_lease_seconds,
        )
        await db.commit()
    processed = 0
    for kb_id, org_id in claimed:
        try:
            async with factory() as db:
                await set_tenant_context(db, org_id)
                await kb_ingestion.ingest_one_kb(db, kb_id, settings)
                await db.commit()
            processed += 1
        except Exception as exc:
            logger.bind(
                component="kb_ingestion_poller",
                kb_id=str(kb_id),
                exc_type=type(exc).__name__,
            ).error("KB ingestion failed for kb_id={kb_id} exc_type={exc_type}")
    return processed


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    log = logger.bind(component="kb_ingestion_poller")
    log.info("KB ingestion poller started (interval={i}s)", i=settings.kb_ingestion_poll_interval_s)
    factory = get_session_factory()
    while not stop.is_set():
        try:
            n = await poll_once(factory, settings)
            if n:
                log.info("Ingested {n} knowledge base(s)", n=n)
        except Exception:
            log.opt(exception=True).error("KB ingestion poll cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.kb_ingestion_poll_interval_s)
    log.info("KB ingestion poller stopped")
