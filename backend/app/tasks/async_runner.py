"""Run async task bodies on one event loop per Celery worker process."""

import asyncio
import atexit
import os
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

_runner: asyncio.Runner | None = None
_runner_pid: int | None = None


def _process_runner() -> asyncio.Runner:
    """Return the runner owned by the current prefork worker process."""
    global _runner, _runner_pid
    pid = os.getpid()
    if _runner is None or _runner_pid != pid:
        # A runner inherited across fork must never be used in the child. Each
        # process owns its event loop and the async database pool bound to it.
        _runner = asyncio.Runner()
        _runner_pid = pid
    return _runner


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run a task coroutine without moving pooled DB connections between loops."""
    return _process_runner().run(coro)


@atexit.register
def _close_process_runner() -> None:
    global _runner, _runner_pid
    if _runner is not None and _runner_pid == os.getpid():
        _runner.close()
    _runner = None
    _runner_pid = None
