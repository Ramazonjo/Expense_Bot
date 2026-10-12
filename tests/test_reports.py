from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from expense_bot.reports import Reporter, latest_report_day, summarize_rows
from expense_bot.storage import Store


def test_decimal_totals_and_dushanbe_midnight(settings):
    rows = [
        ["2026-09-24T19:00:00+00:00", "a", 0.1, "Личные расходы", "u"],
        ["2026-09-25T18:59:59+00:00", "b", 0.2, "Личные расходы", "u"],
        ["2026-09-25T19:00:00+00:00", "c", 900, "Другие расходы", "u"],
        ["2026-09-25T10:00:00+05:00", "d", 50, "Мебельный завод", "u"],
        ["2026-09-25T11:00:00+05:00", "e", "bad", "Другие расходы", "u"],
        ["unreadable-date", "f", 10, "Личные расходы", "u"],
        [],
    ]
    totals, count, invalid = summarize_rows(rows, date(2026, 9, 25), settings.tz)
    assert totals["Личные расходы"] == Decimal("0.3")
    assert totals["Мебельный завод"] == 50
    assert totals["Другие расходы"] == 0
    assert count == 3 and invalid == [6, 7]


def test_schedule_before_after_cutoff_and_full_day_mode(settings):
    before = datetime(2026, 9, 25, 22, 59, tzinfo=settings.tz)
    after = datetime(2026, 9, 25, 23, 0, tzinfo=settings.tz)
    assert latest_report_day(before, settings) == date(2026, 9, 24)
    assert latest_report_day(after, settings) == date(2026, 9, 25)
    settings.report_hour = 0
    settings.report_minute = 5
    settings.report_previous_day = True
    assert latest_report_day(datetime(2026, 9, 26, 0, 6, tzinfo=settings.tz), settings) == date(
        2026, 9, 25
    )


async def test_report_delivery_retries_only_unsent_recipients(settings):
    store = Store(settings.sqlite_path)
    await store.open("test")
    bot = AsyncMock()
    bot.send_message.side_effect = [None, RuntimeError("temporary"), None]
    reporter = Reporter(settings, store, AsyncMock(), bot)
    reporter.build = AsyncMock(return_value="Report from Google Sheets")
    await reporter.scheduled()
    await reporter.scheduled()
    assert [c.args[0] for c in bot.send_message.await_args_list] == [100, 200, 200]
    await reporter.scheduled()
    assert bot.send_message.await_count == 3
    await store.close()
