import copy
from unittest.mock import AsyncMock

import pytest
import requests

from expense_bot.models import HEADERS, parse_expenses
from expense_bot.sheets import SheetConflict, Sheets
from expense_bot.storage import QueueFull, Store
from expense_bot.worker import Worker


class FakeWorksheet:
    row_count = 1000

    def __init__(self):
        self.data = [HEADERS.copy()]
        self.updates = []
        self.lose_next_response = False

    def row_values(self, row):
        return self.data[row - 1]

    def get(self, target, **kwargs):
        if target == "A:E":
            return copy.deepcopy(self.data)
        first, last = target.split(":")
        return copy.deepcopy(self.data[int(first[1:]) - 1 : int(last[1:])])

    def update(self, *, range_name, values, value_input_option):
        assert value_input_option == "RAW"
        start = int(range_name.split(":")[0][1:]) - 1
        while len(self.data) < start + len(values):
            self.data.append([])
        self.data[start : start + len(values)] = copy.deepcopy(values)
        self.updates.append(range_name)
        if self.lose_next_response:
            self.lose_next_response = False
            raise requests.Timeout("Server committed, HTTP response lost")


async def enqueue(store, message_id=1):
    return await store.enqueue(
        chat_id=100,
        message_id=message_id,
        author="Farrukh [id:100]",
        occurred_at="2026-09-25T22:30:00+05:00",
        text="клей 50",
        photo=b"photo",
        mime="image/jpeg",
        limit=10,
    )


async def test_lost_google_response_then_restart_does_not_duplicate(settings):
    store = Store(settings.sqlite_path)
    await store.open("test-ledger")
    sheets = Sheets(settings)
    sheets.ws = FakeWorksheet()
    sheets.ws.lose_next_response = True
    ai = AsyncMock()
    ai.extract.return_value = parse_expenses(
        '[{"item":"Клей","amount":50,"category":"Мебельный завод"}]'
    )
    worker = Worker(settings, store, sheets, ai, AsyncMock())
    job_id, created = await enqueue(store)
    assert created
    await worker.process(await store.next_job())
    assert len(sheets.ws.data) == 2  # Google already committed.
    await store.close()

    store = Store(settings.sqlite_path)
    await store.open("test-ledger")
    duplicate_id, created = await enqueue(store)
    assert duplicate_id == job_id and not created
    await store._write("UPDATE jobs SET next_try=0 WHERE id=?", (job_id,))
    worker.store = store
    await worker.process(await store.next_job())
    assert len(sheets.ws.data) == 2
    assert len(sheets.ws.updates) == 1  # No append, no second update needed.
    assert ai.extract.await_count == 1  # Persisted parsed rows reused after restart.
    assert (await store.recent(100))[0]["state"] == "done"
    record = await store._one("SELECT photo,text FROM jobs WHERE id=?", (job_id,))
    assert record == {"photo": None, "text": ""}
    await sheets.close()
    await store.close()


async def test_conflicting_sheet_row_is_never_overwritten(settings):
    sheets = Sheets(settings)
    sheets.ws = FakeWorksheet()
    existing = ["2026-09-25T00:00:00+05:00", "Other", 7, "Другие расходы", "user"]
    sheets.ws.data.append(existing.copy())
    with pytest.raises(SheetConflict):
        await sheets.write(2, [["2026-09-25T00:00:00+05:00", "New", 8, "Другие расходы", "user"]])
    assert sheets.ws.data[1] == existing
    assert not sheets.ws.updates
    await sheets.close()


async def test_raw_formula_text_and_reserved_ranges(settings):
    store = Store(settings.sqlite_path)
    await store.open("test")
    first, _ = await enqueue(store)
    second, _ = await enqueue(store, message_id=2)
    assert await store.reserve(first, 2, 2) == 2
    assert await store.reserve(second, 2, 1) == 4
    assert await store.reserve(first, 100, 2) == 2
    sheets = Sheets(settings)
    sheets.ws = FakeWorksheet()
    formula = '=IMPORTXML("https://example.invalid","//a")'
    await sheets.write(2, [["2026-09-25T00:00:00+05:00", formula, 1, "Другие расходы", "user"]])
    assert sheets.ws.data[1][1] == formula
    await sheets.close()
    await store.close()


async def test_invalid_ai_response_writes_nothing_and_retry_is_owner_scoped(settings):
    store = Store(settings.sqlite_path)
    await store.open("test")
    job_id, _ = await enqueue(store)
    ai, sheets = AsyncMock(), AsyncMock()
    from expense_bot.models import InvalidExtraction

    ai.extract.side_effect = InvalidExtraction("Unclear")
    worker = Worker(settings, store, sheets, ai, AsyncMock())
    await worker.process(await store.next_job())
    sheets.write.assert_not_awaited()
    assert (await store.recent(100))[0]["state"] == "failed"
    assert not await store.retry(200, job_id)
    assert await store.retry(100, job_id)
    await store.close()


async def test_queue_limit_and_binding(settings):
    store = Store(settings.sqlite_path)
    await store.open("one-ledger")
    await enqueue(store)
    with pytest.raises(QueueFull):
        await store.enqueue(
            chat_id=100,
            message_id=2,
            author="u",
            occurred_at="2026",
            text="x",
            photo=None,
            mime=None,
            limit=1,
        )
    await store.close()
    second = Store(settings.sqlite_path)
    try:
        with pytest.raises(RuntimeError):
            await second.open("different-ledger")
    finally:
        await second.close()
