"""Operator-facing guarantees for immutable-knowledge backfill commands."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import backfill_knowledge_serving_revisions as serving_script
from scripts import backfill_speech_lexicons as lexicon_script


class _SessionContext:
    def __init__(self) -> None:
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "batch_function_name"),
    [
        (lexicon_script, "backfill_approved_speech_lexicons_batch"),
        (serving_script, "backfill_approved_serving_revisions_batch"),
    ],
)
async def test_backfill_run_accumulates_and_surfaces_quarantined_rows(
    monkeypatch,
    module,
    batch_function_name,
):
    sessions: list[_SessionContext] = []

    def session_factory() -> _SessionContext:
        session = _SessionContext()
        sessions.append(session)
        return session

    batches = AsyncMock(
        side_effect=[
            SimpleNamespace(selected=2, published=1, failed=1),
            SimpleNamespace(selected=1, published=1, failed=0),
            SimpleNamespace(selected=0, published=0, failed=0),
        ]
    )
    monkeypatch.setattr(module, "async_session_factory", session_factory)
    monkeypatch.setattr(module, batch_function_name, batches)

    result = await module._run(tenant_id=None, batch_size=25)

    assert result.published == 2
    assert result.quarantined == 1
    assert batches.await_count == 3
    assert all(session.commit.await_count == 1 for session in sessions)


@pytest.mark.parametrize("module", [lexicon_script, serving_script])
def test_backfill_command_exits_nonzero_when_any_row_was_quarantined(
    monkeypatch,
    capsys,
    module,
):
    monkeypatch.setattr(
        module,
        "_run",
        AsyncMock(return_value=module.BackfillRunSummary(published=4, quarantined=1)),
    )
    monkeypatch.setattr("sys.argv", ["backfill"])

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "Published 4" in output
    assert "quarantined 1 knowledge base(s)" in output


@pytest.mark.parametrize("module", [lexicon_script, serving_script])
def test_backfill_command_exits_zero_when_every_selected_row_published(
    monkeypatch,
    capsys,
    module,
):
    monkeypatch.setattr(
        module,
        "_run",
        AsyncMock(return_value=module.BackfillRunSummary(published=4, quarantined=0)),
    )
    monkeypatch.setattr("sys.argv", ["backfill"])

    module.main()

    output = capsys.readouterr().out
    assert "Published 4" in output
    assert "quarantined 0 knowledge base(s)" in output
