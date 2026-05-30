"""Tracked fire-and-forget asyncio tasks.

asyncio holds only a weak reference to bare create_task() results, so a task can
be garbage-collected before it finishes. Keep a strong reference in a set and
drain on shutdown.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def active_tasks() -> set[asyncio.Task[Any]]:
    return set(_tasks)


async def drain(timeout: float = 30.0) -> None:  # noqa: ASYNC109
    pending = active_tasks()
    if not pending:
        return
    await asyncio.wait(pending, timeout=timeout)
