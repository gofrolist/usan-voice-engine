# Plan MON-1: Metrics Pipeline (agent → API → Postgres) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Instrument every call so it writes per-turn latency + per-call cost/usage rows to Postgres, via a JWT-authenticated agent→API flush at call end.

**Architecture:** The livekit-agents worker taps LiveKit's built-in `metrics_collected` events into a `MetricsAccumulator`, then POSTs a compact `{turns, usage}` payload to a new `POST /v1/tools/log_metrics` API endpoint at session shutdown (mirroring the existing transcript flush). The API authenticates with the per-call service JWT, computes modeled cost server-side from versioned pricing constants and the call's own `duration_seconds`, and persists two new tables (`turn_metrics`, `call_metrics`). No Prometheus/Grafana in this plan — that is Plans MON-2 and MON-3.

**Tech Stack:** Python 3.14 (apps/api: FastAPI, SQLAlchemy 2.0 async, Alembic, Pydantic v2, pytest+testcontainers). Python 3.12 (services/agent: livekit-agents ≥1.5.14, httpx, pyjwt, pytest). uv, ruff (line-length 100).

**Spec:** `docs/superpowers/specs/2026-06-05-monitoring-dashboard-design.md` (phases 1–2).

---

## Deviations from spec (deliberate, discovered during planning)

- **Endpoint path:** spec §5.1 says `POST /v1/calls/{call_id}/metrics`. This plan uses `POST /v1/tools/log_metrics` (call_id in body) to match the established `log_transcript`/`log_wellness` tools-router pattern (`require_service_token` + `_authorize_call` + handler-owned `db.commit()`). Same auth and semantics.
- **Telephony-duration race:** the metrics flush can arrive before the `room_finished` webhook sets `calls.duration_seconds`. The agent therefore also sends a measured `session_duration_seconds`; the API uses `call.duration_seconds` when present, else the agent value. The chosen value is stored in `call_metrics.duration_seconds`.

## Open verification items (carried from spec; resolve while executing)

- **V1** — confirm LiveKit metric class names (`EOUMetrics`, `STTMetrics`, `LLMMetrics`, `TTSMetrics`) and field names (`end_of_utterance_delay`, `transcription_delay`, `duration`, `audio_duration`, `ttft`, `prompt_tokens`, `completion_tokens`, `ttfb`, `characters_count`) and event ordering against the pinned `livekit-agents` (Task 6). The accumulator dispatches on class **name** and reads fields via `getattr(..., default)`, so it is decoupled from livekit imports and unit-testable regardless; only the live wiring (Task 8) needs the real names to line up.
- **V2** — fill LLM/STT/TTS/GCS pricing constants from current vendor pricing. Defaults ship as `0` (except Telnyx `0.008` from spec §2), so cost is telephony-only until filled. Documented in `.env`/settings.
- **V3** — validate the `response_latency_ms` composite (`transcription_delay_ms + llm_ttft_ms + tts_ttfb_ms`) against LiveKit's definitions (no double-counting). Raw components are stored, so the composite is recomputable without a migration.

## File structure

**apps/api (create):**
- `src/usan_api/cost.py` — pure cost model: `Pricing` dataclass + `compute_costs(...)`.
- `src/usan_api/repositories/metrics.py` — `response_latency_ms(...)` helper + `create_metrics(...)` + `get_call_metrics(...)`.
- `migrations/versions/0008_metrics_tables.py` — `turn_metrics` + `call_metrics`.
- `tests/test_cost.py`, `tests/test_metrics.py`.

**apps/api (modify):**
- `src/usan_api/settings.py` — add pricing `Decimal` fields + `pricing_version`.
- `src/usan_api/db/models.py` — add `Numeric` import + `TurnMetrics`, `CallMetrics` models.
- `src/usan_api/schemas/tools.py` — add `TurnMetricIn`, `MetricsUsageIn`, `LogMetricsRequest`, `MetricsAcceptedResponse`.
- `src/usan_api/routers/tools.py` — add `log_metrics` handler.

**services/agent (create):**
- `src/usan_agent/metrics_hooks.py` — `MetricsAccumulator` + `register_metrics_flush(...)`.
- `tests/test_metrics_hooks.py`.

**services/agent (modify):**
- `src/usan_agent/api_client.py` — add `post_metrics(...)`.
- `src/usan_agent/worker.py` — call `register_metrics_flush(...)` in both branches.
- `tests/test_api_client.py` — add `post_metrics` test.

---

## Task 1: Cost model + pricing settings (apps/api)

**Files:**
- Create: `apps/api/src/usan_api/cost.py`
- Modify: `apps/api/src/usan_api/settings.py`
- Test: `apps/api/tests/test_cost.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_cost.py`:

```python
from decimal import Decimal

from usan_api.cost import Pricing, compute_costs

_ZERO = dict(
    llm_in_per_1k=Decimal("0"), llm_out_per_1k=Decimal("0"),
    stt_per_min=Decimal("0"), tts_per_1k_chars=Decimal("0"),
    gcs_per_gb_month=Decimal("0"), version="t",
)


def test_telephony_only():
    pricing = Pricing(telnyx_per_min=Decimal("0.008"), **_ZERO)
    costs = compute_costs(
        duration_seconds=120, llm_prompt_tokens=0, llm_completion_tokens=0,
        tts_characters=0, stt_audio_seconds=0, recording_bytes=0, pricing=pricing,
    )
    assert costs["telephony"] == Decimal("0.016000")
    assert costs["total"] == Decimal("0.016000")


def test_all_components_sum():
    pricing = Pricing(
        telnyx_per_min=Decimal("0.006"), llm_in_per_1k=Decimal("0.10"),
        llm_out_per_1k=Decimal("0.40"), stt_per_min=Decimal("0.02"),
        tts_per_1k_chars=Decimal("0.05"), gcs_per_gb_month=Decimal("0"), version="t",
    )
    costs = compute_costs(
        duration_seconds=60, llm_prompt_tokens=1000, llm_completion_tokens=500,
        tts_characters=2000, stt_audio_seconds=60, recording_bytes=0, pricing=pricing,
    )
    assert costs["telephony"] == Decimal("0.006000")
    assert costs["llm"] == Decimal("0.300000")
    assert costs["stt"] == Decimal("0.020000")
    assert costs["tts"] == Decimal("0.100000")
    assert costs["total"] == Decimal("0.426000")


def test_none_duration_is_zero():
    pricing = Pricing(telnyx_per_min=Decimal("0.008"), **_ZERO)
    costs = compute_costs(
        duration_seconds=None, llm_prompt_tokens=0, llm_completion_tokens=0,
        tts_characters=0, stt_audio_seconds=0.0, recording_bytes=0, pricing=pricing,
    )
    assert costs["total"] == Decimal("0.000000")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_cost.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.cost'`

- [ ] **Step 3: Write the cost module**

Create `apps/api/src/usan_api/cost.py`:

```python
"""Modeled per-call cost: usage × versioned pricing constants (design spec §6).

Pure and DB-free so it unit-tests without a database. All money is Decimal,
quantized to 6 dp to match the NUMERIC(12,6) columns.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_Q = Decimal("0.000001")


@dataclass(frozen=True)
class Pricing:
    telnyx_per_min: Decimal
    llm_in_per_1k: Decimal
    llm_out_per_1k: Decimal
    stt_per_min: Decimal
    tts_per_1k_chars: Decimal
    gcs_per_gb_month: Decimal
    version: str

    @classmethod
    def from_settings(cls, settings: Any) -> "Pricing":
        return cls(
            telnyx_per_min=settings.telnyx_per_min_usd,
            llm_in_per_1k=settings.llm_input_per_1k_usd,
            llm_out_per_1k=settings.llm_output_per_1k_usd,
            stt_per_min=settings.cartesia_stt_per_min_usd,
            tts_per_1k_chars=settings.cartesia_tts_per_1k_chars_usd,
            gcs_per_gb_month=settings.gcs_storage_per_gb_month_usd,
            version=settings.pricing_version,
        )


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def compute_costs(
    *,
    duration_seconds: int | None,
    llm_prompt_tokens: int,
    llm_completion_tokens: int,
    tts_characters: int,
    stt_audio_seconds: float,
    recording_bytes: int,
    pricing: Pricing,
) -> dict[str, Decimal]:
    telephony = _d(duration_seconds) / Decimal(60) * pricing.telnyx_per_min
    llm = (
        _d(llm_prompt_tokens) / Decimal(1000) * pricing.llm_in_per_1k
        + _d(llm_completion_tokens) / Decimal(1000) * pricing.llm_out_per_1k
    )
    stt = _d(stt_audio_seconds) / Decimal(60) * pricing.stt_per_min
    tts = _d(tts_characters) / Decimal(1000) * pricing.tts_per_1k_chars
    storage = _d(recording_bytes) / Decimal(1_000_000_000) * pricing.gcs_per_gb_month
    parts = {
        "telephony": telephony.quantize(_Q),
        "llm": llm.quantize(_Q),
        "stt": stt.quantize(_Q),
        "tts": tts.quantize(_Q),
        "storage": storage.quantize(_Q),
    }
    parts["total"] = sum(parts.values()).quantize(_Q)
    return parts
```

- [ ] **Step 4: Add pricing settings fields**

In `apps/api/src/usan_api/settings.py`, add `from decimal import Decimal` to the imports, then add these fields inside the `Settings` class (next to the other `Field(...)` declarations):

```python
    telnyx_per_min_usd: Decimal = Field(
        default=Decimal("0.008"), ge=0, alias="TELNYX_PER_MIN_USD"
    )
    llm_input_per_1k_usd: Decimal = Field(
        default=Decimal("0"), ge=0, alias="LLM_INPUT_PER_1K_USD"
    )
    llm_output_per_1k_usd: Decimal = Field(
        default=Decimal("0"), ge=0, alias="LLM_OUTPUT_PER_1K_USD"
    )
    cartesia_stt_per_min_usd: Decimal = Field(
        default=Decimal("0"), ge=0, alias="CARTESIA_STT_PER_MIN_USD"
    )
    cartesia_tts_per_1k_chars_usd: Decimal = Field(
        default=Decimal("0"), ge=0, alias="CARTESIA_TTS_PER_1K_CHARS_USD"
    )
    gcs_storage_per_gb_month_usd: Decimal = Field(
        default=Decimal("0"), ge=0, alias="GCS_STORAGE_PER_GB_MONTH_USD"
    )
    pricing_version: str = Field(default="2026-06-05", alias="PRICING_VERSION")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_cost.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Lint**

Run: `cd apps/api && uv run ruff check . && uv run ruff format .`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/cost.py apps/api/src/usan_api/settings.py apps/api/tests/test_cost.py
git commit -m "feat(api): modeled cost model + pricing settings"
```

---

## Task 2: Migration + ORM models for metrics tables (apps/api)

**Files:**
- Create: `apps/api/migrations/versions/0008_metrics_tables.py`
- Modify: `apps/api/src/usan_api/db/models.py`

- [ ] **Step 1: Write the migration**

Create `apps/api/migrations/versions/0008_metrics_tables.py`:

```python
"""metrics tables: turn_metrics (per-turn latency), call_metrics (per-call cost/usage)

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-05

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE turn_metrics (
            id                     BIGSERIAL PRIMARY KEY,
            call_id                UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            turn_index             INTEGER NOT NULL,
            eou_delay_ms           INTEGER,
            transcription_delay_ms INTEGER,
            stt_duration_ms        INTEGER,
            llm_ttft_ms            INTEGER,
            tts_ttfb_ms            INTEGER,
            llm_completion_tokens  INTEGER,
            tts_characters         INTEGER,
            response_latency_ms    INTEGER,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_turn_metrics_call ON turn_metrics(call_id)")
    op.execute("CREATE INDEX idx_turn_metrics_created ON turn_metrics(created_at)")

    op.execute(
        """
        CREATE TABLE call_metrics (
            call_id               UUID PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
            llm_prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            llm_completion_tokens INTEGER NOT NULL DEFAULT 0,
            llm_total_tokens      INTEGER NOT NULL DEFAULT 0,
            tts_characters        INTEGER NOT NULL DEFAULT 0,
            stt_audio_seconds     NUMERIC(10,2) NOT NULL DEFAULT 0,
            duration_seconds      INTEGER,
            cost_telephony_usd    NUMERIC(12,6) NOT NULL DEFAULT 0,
            cost_llm_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
            cost_stt_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
            cost_tts_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
            cost_storage_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
            cost_total_usd        NUMERIC(12,6) NOT NULL DEFAULT 0,
            pricing_version       TEXT NOT NULL,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS turn_metrics")
    op.execute("DROP TABLE IF EXISTS call_metrics")
```

- [ ] **Step 2: Sanity-check imports still load**

Run: `cd apps/api && uv run pytest tests/test_cost.py -v`
Expected: PASS. The migration itself is exercised by Task 5's container-backed tests (the test DB is built by `alembic upgrade head` against a `pgvector/pgvector:pg18` testcontainer — see `tests/conftest.py` — so the new tables become visible to tests only once this file exists, which it now does).

- [ ] **Step 3: Add ORM models**

In `apps/api/src/usan_api/db/models.py`: add `Numeric` to the existing `from sqlalchemy import ...` line, and add `from decimal import Decimal` to the imports. Then append these two models at the end of the file:

```python
class TurnMetrics(Base):
    __tablename__ = "turn_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    eou_delay_ms: Mapped[int | None] = mapped_column(Integer)
    transcription_delay_ms: Mapped[int | None] = mapped_column(Integer)
    stt_duration_ms: Mapped[int | None] = mapped_column(Integer)
    llm_ttft_ms: Mapped[int | None] = mapped_column(Integer)
    tts_ttfb_ms: Mapped[int | None] = mapped_column(Integer)
    llm_completion_tokens: Mapped[int | None] = mapped_column(Integer)
    tts_characters: Mapped[int | None] = mapped_column(Integer)
    response_latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CallMetrics(Base):
    __tablename__ = "call_metrics"

    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    llm_prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    llm_completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    llm_total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tts_characters: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    stt_audio_seconds: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, server_default=text("0")
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    cost_telephony_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    cost_llm_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    cost_stt_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    cost_tts_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    cost_storage_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    cost_total_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    pricing_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 4: Verify models import cleanly**

Run: `cd apps/api && uv run python -c "from usan_api.db.models import TurnMetrics, CallMetrics; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Lint**

Run: `cd apps/api && uv run ruff check . && uv run ruff format .`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add apps/api/migrations/versions/0008_metrics_tables.py apps/api/src/usan_api/db/models.py
git commit -m "feat(api): turn_metrics + call_metrics tables and models"
```

---

## Task 3: Metrics repository + latency helper (apps/api)

**Files:**
- Create: `apps/api/src/usan_api/repositories/metrics.py`
- Test: `apps/api/tests/test_metrics.py` (latency helper unit tests; endpoint tests added in Task 5)

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_metrics.py`:

```python
from usan_api.repositories.metrics import response_latency_ms


def test_response_latency_sums_present_components():
    assert response_latency_ms(120, 210, 80) == 410


def test_response_latency_ignores_none():
    assert response_latency_ms(None, 210, 80) == 290


def test_response_latency_all_none_is_none():
    assert response_latency_ms(None, None, None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.repositories.metrics'`

- [ ] **Step 3: Write the repository**

Create `apps/api/src/usan_api/repositories/metrics.py`:

```python
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import CallMetrics, TurnMetrics


def response_latency_ms(
    transcription_delay_ms: int | None,
    llm_ttft_ms: int | None,
    tts_ttfb_ms: int | None,
) -> int | None:
    """User-perceived end-of-turn responsiveness (design spec §7). NULL if no parts."""
    parts = [p for p in (transcription_delay_ms, llm_ttft_ms, tts_ttfb_ms) if p is not None]
    return sum(parts) if parts else None


async def get_call_metrics(db: AsyncSession, call_id: uuid.UUID) -> CallMetrics | None:
    return await db.get(CallMetrics, call_id)


async def create_metrics(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    turns: list[Any],
    usage: Any,
    costs: dict[str, Decimal],
    duration_seconds: int | None,
    pricing_version: str,
) -> CallMetrics:
    """Insert per-turn rows and one call_metrics row. Handler owns the commit."""
    for t in turns:
        db.add(
            TurnMetrics(
                call_id=call_id,
                turn_index=t.turn_index,
                eou_delay_ms=t.eou_delay_ms,
                transcription_delay_ms=t.transcription_delay_ms,
                stt_duration_ms=t.stt_duration_ms,
                llm_ttft_ms=t.llm_ttft_ms,
                tts_ttfb_ms=t.tts_ttfb_ms,
                llm_completion_tokens=t.llm_completion_tokens,
                tts_characters=t.tts_characters,
                response_latency_ms=response_latency_ms(
                    t.transcription_delay_ms, t.llm_ttft_ms, t.tts_ttfb_ms
                ),
            )
        )
    row = CallMetrics(
        call_id=call_id,
        llm_prompt_tokens=usage.llm_prompt_tokens,
        llm_completion_tokens=usage.llm_completion_tokens,
        llm_total_tokens=usage.llm_prompt_tokens + usage.llm_completion_tokens,
        tts_characters=usage.tts_characters,
        stt_audio_seconds=Decimal(str(usage.stt_audio_seconds)),
        duration_seconds=duration_seconds,
        cost_telephony_usd=costs["telephony"],
        cost_llm_usd=costs["llm"],
        cost_stt_usd=costs["stt"],
        cost_tts_usd=costs["tts"],
        cost_storage_usd=costs["storage"],
        cost_total_usd=costs["total"],
        pricing_version=pricing_version,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_metrics.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Lint + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format .
git add apps/api/src/usan_api/repositories/metrics.py apps/api/tests/test_metrics.py
git commit -m "feat(api): metrics repository + response-latency helper"
```

---

## Task 4: Request/response schemas (apps/api)

**Files:**
- Modify: `apps/api/src/usan_api/schemas/tools.py` (the module that defines `LogWellnessRequest`/`LoggedResponse`; confirm path before editing)

- [ ] **Step 1: Add the schemas**

Append to `apps/api/src/usan_api/schemas/tools.py` (ensure `import uuid`, `from decimal import Decimal`, and `from pydantic import BaseModel, Field` are imported at the top — add any that are missing):

```python
class TurnMetricIn(BaseModel):
    turn_index: int
    eou_delay_ms: int | None = None
    transcription_delay_ms: int | None = None
    stt_duration_ms: int | None = None
    llm_ttft_ms: int | None = None
    tts_ttfb_ms: int | None = None
    llm_completion_tokens: int | None = None
    tts_characters: int | None = None


class MetricsUsageIn(BaseModel):
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    tts_characters: int = 0
    stt_audio_seconds: float = 0.0
    session_duration_seconds: float | None = None


class LogMetricsRequest(BaseModel):
    call_id: uuid.UUID
    turns: list[TurnMetricIn] = Field(default_factory=list, max_length=500)
    usage: MetricsUsageIn = Field(default_factory=MetricsUsageIn)


class MetricsAcceptedResponse(BaseModel):
    call_id: uuid.UUID
    cost_total_usd: Decimal
```

- [ ] **Step 2: Verify import**

Run: `cd apps/api && uv run python -c "from usan_api.schemas.tools import LogMetricsRequest, MetricsAcceptedResponse; print('ok')"`
Expected: `ok` (if the path differs, locate the module defining `LoggedResponse` and add there, updating imports in Task 5 accordingly)

- [ ] **Step 3: Lint + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format .
git add apps/api/src/usan_api/schemas/tools.py
git commit -m "feat(api): log_metrics request/response schemas"
```

---

## Task 5: `log_metrics` endpoint (apps/api)

**Files:**
- Modify: `apps/api/src/usan_api/routers/tools.py`
- Test: `apps/api/tests/test_metrics.py`

- [ ] **Step 1: Write the failing endpoint tests**

Append to `apps/api/tests/test_metrics.py`:

```python
import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, CallMetrics, TurnMetrics


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    import time

    import jwt

    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _make_completed_call(async_database_url: str, duration_seconds: int | None = 120) -> str:
    async def _run() -> str:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            call = Call(
                direction=CallDirection.OUTBOUND,
                status=CallStatus.COMPLETED,
                duration_seconds=duration_seconds,
            )
            s.add(call)
            await s.flush()
            cid = str(call.id)
            await s.commit()
        await engine.dispose()
        return cid

    return asyncio.run(_run())


def _count(async_database_url: str, model, call_id: str) -> int:
    async def _run() -> int:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            result = await s.execute(
                select(func.count()).select_from(model).where(model.call_id == call_id)
            )
            n = result.scalar_one()
        await engine.dispose()
        return n

    return asyncio.run(_run())


_BODY = {
    "turns": [
        {
            "turn_index": 0,
            "eou_delay_ms": 180,
            "transcription_delay_ms": 120,
            "stt_duration_ms": 90,
            "llm_ttft_ms": 210,
            "tts_ttfb_ms": 80,
            "llm_completion_tokens": 50,
            "tts_characters": 240,
        }
    ],
    "usage": {
        "llm_prompt_tokens": 100,
        "llm_completion_tokens": 50,
        "tts_characters": 240,
        "stt_audio_seconds": 3.0,
        "session_duration_seconds": 42.0,
    },
}


def test_log_metrics_requires_token(client, async_database_url):
    cid = _make_completed_call(async_database_url)
    r = client.post("/v1/tools/log_metrics", json={"call_id": cid, **_BODY})
    assert r.status_code == 401


def test_log_metrics_call_id_mismatch_403(client, async_database_url):
    cid = _make_completed_call(async_database_url)
    other = "00000000-0000-0000-0000-000000000999"
    r = client.post(
        "/v1/tools/log_metrics",
        json={"call_id": cid, **_BODY},
        headers={"Authorization": f"Bearer {_service_token(other)}"},
    )
    assert r.status_code == 403


def test_log_metrics_unknown_call_404(client):
    missing = "00000000-0000-0000-0000-0000000000aa"
    r = client.post(
        "/v1/tools/log_metrics",
        json={"call_id": missing, **_BODY},
        headers={"Authorization": f"Bearer {_service_token(missing)}"},
    )
    assert r.status_code == 404


def test_log_metrics_persists_and_costs_telephony(client, async_database_url):
    cid = _make_completed_call(async_database_url, duration_seconds=120)
    r = client.post(
        "/v1/tools/log_metrics",
        json={"call_id": cid, **_BODY},
        headers={"Authorization": f"Bearer {_service_token(cid)}"},
    )
    assert r.status_code == 200
    # default Telnyx rate 0.008, others 0 → 120s = 2 min × 0.008 = 0.016
    assert float(r.json()["cost_total_usd"]) == 0.016
    assert _count(async_database_url, CallMetrics, cid) == 1
    assert _count(async_database_url, TurnMetrics, cid) == 1


def test_log_metrics_is_idempotent(client, async_database_url):
    cid = _make_completed_call(async_database_url, duration_seconds=120)
    headers = {"Authorization": f"Bearer {_service_token(cid)}"}
    r1 = client.post("/v1/tools/log_metrics", json={"call_id": cid, **_BODY}, headers=headers)
    r2 = client.post("/v1/tools/log_metrics", json={"call_id": cid, **_BODY}, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert _count(async_database_url, CallMetrics, cid) == 1
    assert _count(async_database_url, TurnMetrics, cid) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_metrics.py -v`
Expected: FAIL — the 5 endpoint tests return 404 (route not registered) / assertion errors

- [ ] **Step 3: Implement the endpoint**

In `apps/api/src/usan_api/routers/tools.py`, add imports near the top:

```python
from usan_api import cost
from usan_api.repositories import metrics as metrics_repo
from usan_api.schemas.tools import LogMetricsRequest, MetricsAcceptedResponse
from usan_api.settings import Settings, get_settings
```

Then add the handler (place it next to the other tool handlers):

```python
@router.post("/log_metrics", response_model=MetricsAcceptedResponse)
async def log_metrics(
    body: LogMetricsRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
    settings: Settings = Depends(get_settings),
) -> MetricsAcceptedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    existing = await metrics_repo.get_call_metrics(db, call.id)
    if existing is not None:
        return MetricsAcceptedResponse(call_id=call.id, cost_total_usd=existing.cost_total_usd)
    pricing = cost.Pricing.from_settings(settings)
    duration = call.duration_seconds
    if duration is None and body.usage.session_duration_seconds is not None:
        duration = int(body.usage.session_duration_seconds)
    costs = cost.compute_costs(
        duration_seconds=duration,
        llm_prompt_tokens=body.usage.llm_prompt_tokens,
        llm_completion_tokens=body.usage.llm_completion_tokens,
        tts_characters=body.usage.tts_characters,
        stt_audio_seconds=body.usage.stt_audio_seconds,
        recording_bytes=0,
        pricing=pricing,
    )
    await metrics_repo.create_metrics(
        db,
        call_id=call.id,
        turns=body.turns,
        usage=body.usage,
        costs=costs,
        duration_seconds=duration,
        pricing_version=pricing.version,
    )
    await db.commit()
    logger.bind(call_id=str(call.id)).info("Logged call metrics: {n} turns", n=len(body.turns))
    return MetricsAcceptedResponse(call_id=call.id, cost_total_usd=costs["total"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_metrics.py -v`
Expected: PASS (8 passed — 3 helper + 5 endpoint)

- [ ] **Step 5: Run the full API suite to check for regressions**

Run: `cd apps/api && uv run pytest -v`
Expected: all pass

- [ ] **Step 6: Lint + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format .
git add apps/api/src/usan_api/routers/tools.py apps/api/tests/test_metrics.py
git commit -m "feat(api): POST /v1/tools/log_metrics ingestion endpoint"
```

---

## Task 6: MetricsAccumulator (services/agent)

**Files:**
- Create: `services/agent/src/usan_agent/metrics_hooks.py`
- Test: `services/agent/tests/test_metrics_hooks.py`

- [ ] **Step 1: Write the failing test**

Create `services/agent/tests/test_metrics_hooks.py`:

```python
from types import SimpleNamespace

from usan_agent.metrics_hooks import MetricsAccumulator


class EOUMetrics:
    def __init__(self, end_of_utterance_delay, transcription_delay):
        self.end_of_utterance_delay = end_of_utterance_delay
        self.transcription_delay = transcription_delay


class STTMetrics:
    def __init__(self, audio_duration, duration):
        self.audio_duration = audio_duration
        self.duration = duration


class LLMMetrics:
    def __init__(self, ttft, prompt_tokens, completion_tokens):
        self.ttft = ttft
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class TTSMetrics:
    def __init__(self, ttfb, characters_count):
        self.ttfb = ttfb
        self.characters_count = characters_count


def _ev(metric):
    return SimpleNamespace(metrics=metric)


def test_single_full_turn():
    acc = MetricsAccumulator()
    acc.handle(_ev(EOUMetrics(0.18, 0.12)))
    acc.handle(_ev(STTMetrics(3.0, 0.09)))
    acc.handle(_ev(LLMMetrics(0.21, 100, 50)))
    acc.handle(_ev(TTSMetrics(0.08, 240)))

    payload = acc.build_payload(session_duration_seconds=42.0)
    assert payload["turns"] == [
        {
            "turn_index": 0,
            "eou_delay_ms": 180,
            "transcription_delay_ms": 120,
            "stt_duration_ms": 90,
            "llm_ttft_ms": 210,
            "llm_completion_tokens": 50,
            "tts_ttfb_ms": 80,
            "tts_characters": 240,
        }
    ]
    assert payload["usage"] == {
        "llm_prompt_tokens": 100,
        "llm_completion_tokens": 50,
        "tts_characters": 240,
        "stt_audio_seconds": 3.0,
        "session_duration_seconds": 42.0,
    }


def test_two_turns_increment_index():
    acc = MetricsAccumulator()
    for _ in range(2):
        acc.handle(_ev(EOUMetrics(0.1, 0.1)))
        acc.handle(_ev(LLMMetrics(0.2, 10, 5)))
        acc.handle(_ev(TTSMetrics(0.05, 100)))
    payload = acc.build_payload(session_duration_seconds=10.0)
    assert [t["turn_index"] for t in payload["turns"]] == [0, 1]
    assert payload["usage"]["llm_prompt_tokens"] == 20
    assert payload["usage"]["tts_characters"] == 200


def test_trailing_incomplete_turn_is_flushed():
    acc = MetricsAccumulator()
    acc.handle(_ev(EOUMetrics(0.1, 0.1)))
    acc.handle(_ev(LLMMetrics(0.2, 10, 5)))  # no TTS this turn
    payload = acc.build_payload(session_duration_seconds=5.0)
    assert len(payload["turns"]) == 1
    assert payload["turns"][0]["llm_ttft_ms"] == 200
    assert "tts_ttfb_ms" not in payload["turns"][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agent && uv run pytest tests/test_metrics_hooks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_agent.metrics_hooks'`

- [ ] **Step 3: Write the accumulator**

Create `services/agent/src/usan_agent/metrics_hooks.py`:

```python
"""Tap LiveKit `metrics_collected` events into per-turn latency + per-session usage,
and flush them to the API at call end (design spec §4).

The accumulator dispatches on the metric class *name* and reads fields via getattr,
so it stays decoupled from livekit imports and is unit-testable with fakes. Confirm
the real class/field names against the pinned livekit-agents version (V1).
"""

import time
from typing import Any

from usan_agent import api_client
from usan_agent.settings import Settings


def _ms(seconds: float | None) -> int | None:
    if seconds is None:
        return None
    return max(0, round(float(seconds) * 1000))


class MetricsAccumulator:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self.llm_prompt_tokens = 0
        self.llm_completion_tokens = 0
        self.tts_characters = 0
        self.stt_audio_seconds = 0.0

    def _start_turn(self) -> None:
        if self._current is not None:
            self.turns.append(self._current)
        self._current = {"turn_index": len(self.turns)}

    def _ensure_turn(self) -> None:
        if self._current is None:
            self._start_turn()

    def _flush_turn(self) -> None:
        if self._current is not None:
            self.turns.append(self._current)
            self._current = None

    def handle(self, ev: Any) -> None:
        m = ev.metrics
        name = type(m).__name__
        if name == "EOUMetrics":
            self._start_turn()
            assert self._current is not None
            self._current["eou_delay_ms"] = _ms(getattr(m, "end_of_utterance_delay", None))
            self._current["transcription_delay_ms"] = _ms(getattr(m, "transcription_delay", None))
        elif name == "STTMetrics":
            self.stt_audio_seconds += float(getattr(m, "audio_duration", 0.0) or 0.0)
            self._ensure_turn()
            assert self._current is not None
            self._current["stt_duration_ms"] = _ms(getattr(m, "duration", None))
        elif name == "LLMMetrics":
            self.llm_prompt_tokens += int(getattr(m, "prompt_tokens", 0) or 0)
            self.llm_completion_tokens += int(getattr(m, "completion_tokens", 0) or 0)
            self._ensure_turn()
            assert self._current is not None
            self._current["llm_ttft_ms"] = _ms(getattr(m, "ttft", None))
            self._current["llm_completion_tokens"] = getattr(m, "completion_tokens", None)
        elif name == "TTSMetrics":
            self.tts_characters += int(getattr(m, "characters_count", 0) or 0)
            self._ensure_turn()
            assert self._current is not None
            self._current["tts_ttfb_ms"] = _ms(getattr(m, "ttfb", None))
            self._current["tts_characters"] = getattr(m, "characters_count", None)
            self._flush_turn()

    def build_payload(self, *, session_duration_seconds: float | None = None) -> dict[str, Any]:
        self._flush_turn()
        return {
            "turns": self.turns,
            "usage": {
                "llm_prompt_tokens": self.llm_prompt_tokens,
                "llm_completion_tokens": self.llm_completion_tokens,
                "tts_characters": self.tts_characters,
                "stt_audio_seconds": round(self.stt_audio_seconds, 2),
                "session_duration_seconds": session_duration_seconds,
            },
        }


def register_metrics_flush(
    ctx: Any, session: Any, call_id: str, settings: Settings
) -> MetricsAccumulator:
    """Attach a metrics_collected accumulator and a shutdown-callback flush (mirrors
    register_transcript_flush). Returns the accumulator (for tests)."""
    acc = MetricsAccumulator()
    session.on("metrics_collected", lambda ev: acc.handle(ev))
    started = time.monotonic()

    async def _flush() -> None:
        payload = acc.build_payload(session_duration_seconds=round(time.monotonic() - started, 2))
        await api_client.post_metrics(call_id, settings, payload)

    ctx.add_shutdown_callback(_flush)
    return acc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_metrics_hooks.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Lint + commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format .
git add services/agent/src/usan_agent/metrics_hooks.py services/agent/tests/test_metrics_hooks.py
git commit -m "feat(agent): MetricsAccumulator for LiveKit metrics_collected"
```

---

## Task 7: api_client.post_metrics (services/agent)

**Files:**
- Modify: `services/agent/src/usan_agent/api_client.py`
- Test: `services/agent/tests/test_api_client.py`

- [ ] **Step 1: Write the failing test**

Append to `services/agent/tests/test_api_client.py` (reuse the existing `_settings()` and `SECRET` at the top of that file):

```python
@pytest.mark.asyncio
async def test_post_metrics_posts_signed_request(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)

    payload = {"turns": [{"turn_index": 0}], "usage": {"llm_prompt_tokens": 5}}
    await api_client.post_metrics("call-123", _settings(), payload)

    assert captured["url"] == "http://api:8000/v1/tools/log_metrics"
    assert captured["json"]["call_id"] == "call-123"
    assert captured["json"]["turns"] == [{"turn_index": 0}]
    assert captured["json"]["usage"] == {"llm_prompt_tokens": 5}
    token = captured["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims["call_id"] == "call-123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agent && uv run pytest tests/test_api_client.py::test_post_metrics_posts_signed_request -v`
Expected: FAIL — `AttributeError: module 'usan_agent.api_client' has no attribute 'post_metrics'`

- [ ] **Step 3: Implement post_metrics**

Append to `services/agent/src/usan_agent/api_client.py` (mirrors `flush_transcript` — best-effort, never raises):

```python
async def post_metrics(call_id: str, settings: Settings, payload: dict[str, Any]) -> None:
    """Best-effort: POST per-turn latency + per-call usage at call end. Never raises."""
    try:
        call_id = _validate_call_id(call_id)
        url = f"{settings.api_base_url}/v1/tools/log_metrics"
        headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, json={"call_id": call_id, **payload}, headers=headers
            )
            response.raise_for_status()
        logger.bind(call_id=call_id).info(
            "Posted call metrics: {n} turns", n=len(payload.get("turns", []))
        )
    except Exception:
        logger.bind(call_id=call_id).warning("Failed to post call metrics to API")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_api_client.py -v`
Expected: PASS (existing + new)

- [ ] **Step 5: Lint + commit**

```bash
cd services/agent && uv run ruff check . && uv run ruff format .
git add services/agent/src/usan_agent/api_client.py services/agent/tests/test_api_client.py
git commit -m "feat(agent): api_client.post_metrics best-effort flush"
```

---

## Task 8: Wire metrics flush into the worker (services/agent)

**Files:**
- Modify: `services/agent/src/usan_agent/worker.py`

- [ ] **Step 1: Add the import**

In `services/agent/src/usan_agent/worker.py`, add near the other `usan_agent` imports:

```python
from usan_agent.metrics_hooks import register_metrics_flush
```

- [ ] **Step 2: Wire the outbound branch**

In the `entrypoint` outbound path, immediately after the existing
`register_transcript_flush(ctx, session, call_id, settings)` line and before `await session.start(...)`, add:

```python
        register_metrics_flush(ctx, session, call_id, settings)
```

- [ ] **Step 3: Wire the inbound branch**

In `_run_inbound`, immediately after its `register_transcript_flush(ctx, session, call_id, settings)` line (and before that branch's `await session.start(...)`), add the same line:

```python
        register_metrics_flush(ctx, session, call_id, settings)
```

- [ ] **Step 4: Verify both branches are wired**

Run: `cd services/agent && grep -n "register_metrics_flush" src/usan_agent/worker.py`
Expected: 3 lines — 1 import + 2 call sites (outbound + inbound)

- [ ] **Step 5: Run the full agent suite + lint**

Run: `cd services/agent && uv run pytest -v && uv run ruff check . && uv run ruff format .`
Expected: all pass, no lint errors

> **Integration note:** the one-line wiring mirrors the proven `register_transcript_flush` pattern and is exercised end-to-end during the V1 live-call verification (a real call should produce `turn_metrics` + a `call_metrics` row). Behavior of the accumulator, client, and endpoint is already unit-covered by Tasks 5–7.

- [ ] **Step 6: Commit**

```bash
git add services/agent/src/usan_agent/worker.py
git commit -m "feat(agent): flush per-call metrics on session shutdown"
```

---

## Self-review — spec coverage (phases 1–2)

| Spec requirement | Task |
|---|---|
| `turn_metrics` + `call_metrics` tables (§5.2) | Task 2 |
| Metrics endpoint, service-JWT auth, `_authorize_call` (§5.1) | Task 5 |
| Idempotent (one call_metrics per call) (§5.1) | Task 5 (guard) + test |
| Modeled cost server-side, versioned pricing (§6) | Task 1 + Task 5 |
| Telephony duration race fallback | Tasks 4–5 (`session_duration_seconds`) |
| `response_latency_ms` composite, raw components stored (§7) | Task 3 |
| Agent taps `metrics_collected` + usage totals (§4) | Task 6 |
| Agent POSTs at session end, best-effort (§4) | Tasks 7–8 |
| Tests: auth/validation/persistence/idempotency/cost; agent accumulation; POST client (§11) | Tasks 1,3,5,6,7 |
| 80%+ coverage on new code | all tasks (TDD) |

**Placeholder scan:** none — every step has complete code. The only intentional "placeholder" *values* are V2 pricing constants (default 0), flagged explicitly.

**Type consistency:** `MetricsUsageIn`/`TurnMetricIn` field names match `MetricsAccumulator.build_payload` keys and `create_metrics` reads; `compute_costs` keys (`telephony/llm/stt/tts/storage/total`) match `create_metrics` column writes; `post_metrics` URL matches the `log_metrics` route.

---

## Next plans (not in this plan)

- **Plan MON-2** — API Prometheus `/metrics` (instrumentator + custom counters; Caddy `/metrics` 403) and the Prometheus + Grafana containers, `grafana_ro` DB role, Caddy grafana subdomain + operator-CIDR, Terraform vars/secrets/DNS. (spec phases 3–4)
- **Plan MON-3** — the four Grafana dashboards-as-code (Latency, Cost, Business/Care, System). (spec phase 5)

These will be written as separate plan docs once MON-1 lands (MON-3 depends on MON-1's data and MON-2's Grafana).
