import asyncio

from app.tasks import call_tasks, campaign_tasks, knowledge_tasks, webhook_tasks
from app.tasks.async_runner import run_async


async def _running_loop() -> asyncio.AbstractEventLoop:
    await asyncio.sleep(0)
    return asyncio.get_running_loop()


def test_task_domains_share_one_process_event_loop():
    first = run_async(_running_loop())
    second = run_async(_running_loop())

    assert first is second
    assert call_tasks._run_async is run_async
    assert campaign_tasks._run_async is run_async
    assert knowledge_tasks._run_async is run_async
    assert webhook_tasks._run_async is run_async
