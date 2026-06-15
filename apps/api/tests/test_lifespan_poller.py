import asyncio

import pytest
from fastapi.testclient import TestClient

from usan_api import retry_orchestrator, schedule_orchestrator, webhook_delivery
from usan_api.main import create_app
from usan_api.settings import get_settings

TEST_SECRET = "a" * 32


@pytest.fixture(autouse=True)
def webhook_poller_state(monkeypatch):
    """Fake webhook_delivery.run_poller for EVERY lifespan entry in this module.

    The webhook delivery poller is started unconditionally by lifespan, so
    without this fake each test here would run the REAL poller against the
    bogus postgresql://u:p@host/db URL and ERROR-spam via its per-cycle
    try/except. The two webhook-poller tests below assert against the
    recorded state.
    """
    state: dict = {"started": False, "stop": None}

    async def _fake_run_poller(settings, stop):
        state["started"] = True
        state["stop"] = stop
        await stop.wait()  # block until shutdown signals stop

    monkeypatch.setattr(webhook_delivery, "run_poller", _fake_run_poller)
    return state


def _set_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)


@pytest.fixture(autouse=True)
def _clear_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_lifespan_starts_and_stops_poller(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "true")
    state: dict = {"started": False, "stop": None}

    async def _fake_run_poller(settings, stop):
        state["started"] = True
        state["stop"] = stop
        await stop.wait()  # block until shutdown signals stop

    monkeypatch.setattr(retry_orchestrator, "run_poller", _fake_run_poller)
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert state["started"] is True

    assert isinstance(state["stop"], asyncio.Event)
    assert state["stop"].is_set()  # shutdown set the stop event


def test_lifespan_skips_poller_when_disabled(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "false")
    started = {"v": False}

    async def _fake_run_poller(settings, stop):
        started["v"] = True
        await stop.wait()

    monkeypatch.setattr(retry_orchestrator, "run_poller", _fake_run_poller)
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200

    assert started["v"] is False  # poller never started


def test_lifespan_starts_scheduler_poller_when_enabled(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_POLLER_ENABLED", "true")
    # Settings invariant (spec §10.3): the scheduler requires the gate.
    monkeypatch.setenv("CONCURRENCY_GATE_ENABLED", "true")
    retry_state: dict = {"started": False, "stop": None}
    sched_state: dict = {"started": False, "stop": None}

    async def _fake_retry_poller(settings, stop):
        retry_state["started"] = True
        retry_state["stop"] = stop
        await stop.wait()

    async def _fake_scheduler_poller(settings, stop):
        sched_state["started"] = True
        sched_state["stop"] = stop
        await stop.wait()  # block until shutdown signals stop

    monkeypatch.setattr(retry_orchestrator, "run_poller", _fake_retry_poller)
    monkeypatch.setattr(schedule_orchestrator, "run_poller", _fake_scheduler_poller)
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert sched_state["started"] is True

    assert isinstance(sched_state["stop"], asyncio.Event)
    assert sched_state["stop"] is retry_state["stop"]  # all pollers share one stop event
    assert sched_state["stop"].is_set()  # shutdown set the stop event


def test_lifespan_skips_scheduler_poller_by_default(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("SCHEDULER_POLLER_ENABLED", raising=False)  # inert by default
    started = {"v": False}

    async def _fake_scheduler_poller(settings, stop):
        started["v"] = True
        await stop.wait()

    monkeypatch.setattr(schedule_orchestrator, "run_poller", _fake_scheduler_poller)
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200

    assert started["v"] is False  # scheduler poller never started


def test_lifespan_always_starts_webhook_poller(monkeypatch, webhook_poller_state):
    """The webhook delivery poller starts unconditionally — no flag env set at all."""
    _set_env(monkeypatch)
    monkeypatch.delenv("WEBHOOK_DELIVERY_ENABLED", raising=False)
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "true")
    retry_state: dict = {"stop": None}

    async def _fake_retry_poller(settings, stop):
        retry_state["stop"] = stop
        await stop.wait()

    monkeypatch.setattr(retry_orchestrator, "run_poller", _fake_retry_poller)
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert webhook_poller_state["started"] is True

    assert isinstance(webhook_poller_state["stop"], asyncio.Event)
    assert webhook_poller_state["stop"] is retry_state["stop"]  # all pollers share one stop event
    assert webhook_poller_state["stop"].is_set()  # shutdown set the stop event


def test_webhook_poller_starts_even_with_flag_off(monkeypatch, webhook_poller_state):
    """WEBHOOK_DELIVERY_ENABLED=false gates delivery only — the task still starts (§5.1)."""
    _set_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_DELIVERY_ENABLED", "false")
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "false")
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert webhook_poller_state["started"] is True  # flag-off still starts the task

    assert webhook_poller_state["stop"].is_set()  # shutdown set the stop event


def test_lifespan_starts_clara_pollers_when_enabled(monkeypatch):
    # M8: the three Clara Care Parity pollers start when their flags are enabled.
    from usan_api import callback_dialer, family_report_job, notification_outbox

    _set_env(monkeypatch)
    monkeypatch.setenv("NOTIFICATION_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("CALLBACK_DIALER_POLLER_ENABLED", "true")
    monkeypatch.setenv("FAMILY_REPORT_POLLER_ENABLED", "true")
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "false")
    started = {"outbox": False, "callback": False, "report": False}

    def _faker(key: str):
        async def _run(settings, stop):
            started[key] = True
            await stop.wait()

        return _run

    monkeypatch.setattr(notification_outbox, "run_poller", _faker("outbox"))
    monkeypatch.setattr(callback_dialer, "run_poller", _faker("callback"))
    monkeypatch.setattr(family_report_job, "run_poller", _faker("report"))
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert started == {"outbox": True, "callback": True, "report": True}


def test_lifespan_skips_clara_pollers_by_default(monkeypatch):
    # Ship-inert: with the flags at their defaults (off), none of the three start.
    from usan_api import callback_dialer, family_report_job, notification_outbox

    _set_env(monkeypatch)
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "false")
    started = {"v": False}

    def _faker():
        async def _run(settings, stop):
            started["v"] = True
            await stop.wait()

        return _run

    monkeypatch.setattr(notification_outbox, "run_poller", _faker())
    monkeypatch.setattr(callback_dialer, "run_poller", _faker())
    monkeypatch.setattr(family_report_job, "run_poller", _faker())
    get_settings.cache_clear()

    app = create_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
    assert started["v"] is False
