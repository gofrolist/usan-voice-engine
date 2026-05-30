import asyncio

import pytest

from usan_api import background


@pytest.mark.asyncio
async def test_spawn_tracks_and_drains():
    ran = []

    async def work():
        await asyncio.sleep(0)
        ran.append(True)

    background.spawn(work())
    assert len(background.active_tasks()) >= 1
    await background.drain(timeout=2)
    assert ran == [True]
    assert background.active_tasks() == set()


@pytest.mark.asyncio
async def test_drain_with_no_tasks_is_noop():
    await background.drain(timeout=1)
    assert background.active_tasks() == set()
