import asyncio
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .messages import sheet_button
from .models import CATEGORIES, money

log = logging.getLogger(__name__)


def summarize_rows(rows: list[list], day: date, tz):
    totals = {category: Decimal(0) for category in CATEGORIES}
    invalid = []
    count = 0
    for index, row in enumerate(rows, start=2):
        if not any(value not in ("", None) for value in row):
            continue
        try:
            when = datetime.fromisoformat(str(row[0]))
            if when.tzinfo is None:
                when = when.replace(tzinfo=tz)
            if when.astimezone(tz).date() != day:
                continue
            if len(row) < 5 or not str(row[1]).strip() or row[3] not in CATEGORIES:
                raise ValueError("Invalid row")
            if isinstance(row[2], bool):
                raise ValueError("Invalid amount")
            amount = Decimal(str(row[2]))
            if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999.99"):
                raise ValueError("Invalid amount")
            if amount != amount.quantize(Decimal("0.01")):
                raise ValueError("More than two decimal places")
            totals[row[3]] += amount
            count += 1
        except (ValueError, InvalidOperation, IndexError, TypeError):
            invalid.append(index)
    return totals, count, invalid


def latest_report_day(now: datetime, settings) -> date:
    scheduled = now.replace(
        hour=settings.report_hour, minute=settings.report_minute, second=0, microsecond=0
    )
    if now < scheduled:
        scheduled -= timedelta(days=1)
    return scheduled.date() - timedelta(days=int(settings.report_previous_day))


class Reporter:
    def __init__(self, settings, store, sheets, bot):
        self.settings, self.store, self.sheets, self.bot = settings, store, sheets, bot
        self.lock = asyncio.Lock()

    async def build(self, day: date) -> str:
        rows = await self.sheets.rows()
        totals, count, invalid = summarize_rows(rows, day, self.settings.tz)
        total = sum(totals.values(), Decimal(0))
        currency = self.settings.currency
        lines = [
            f"📊 Расходы за {day:%d.%m.%Y}",
            f"Общая сумма за день: {money(total)} {currency}",
            "",
            "Детализация по категориям:",
            f"Личные: {money(totals['Личные расходы'])} {currency}",
            f"Мебельный завод: {money(totals['Мебельный завод'])} {currency}",
            f"Другие: {money(totals['Другие расходы'])} {currency}",
            "",
            f"Учтено записей: {count}",
            f"По данным таблицы на {datetime.now(self.settings.tz):%d.%m.%Y %H:%M} "
            f"({self.settings.timezone}).",
        ]
        if invalid:
            lines.append(
                "⚠️ Итог может быть неполным. Некорректные строки пропущены: "
                + ", ".join(map(str, invalid[:15]))
                + (" …" if len(invalid) > 15 else "")
            )
        unfinished = await self.store.unfinished()
        pending = sum(
            datetime.fromisoformat(j["occurred_at"]).astimezone(self.settings.tz).date() == day
            for j in unfinished
        )
        if pending:
            lines.append(
                f"⚠️ Заявок без подтвержденной записи за этот день: {pending}. "
                "Проверьте /status; итог может быть неполным."
            )
        return "\n".join(lines)

    async def scheduled(self):
        # Cron + interval retry + startup share the same lock and durable sent log.
        async with self.lock:
            day = latest_report_day(datetime.now(self.settings.tz), self.settings)
            recipients = [
                chat_id
                for chat_id in self.settings.report_ids
                if not await self.store.report_sent(day.isoformat(), chat_id)
            ]
            if not recipients:
                return
            try:
                text = await self.build(day)
            except Exception as exc:
                log.warning("Report read failed: %s", type(exc).__name__)
                return
            for chat_id in recipients:
                try:
                    await self.bot.send_message(
                        chat_id, text, reply_markup=sheet_button(self.settings.sheet_url)
                    )
                    await self.store.mark_report(day.isoformat(), chat_id)
                except Exception as exc:
                    log.warning("Report send failed: %s", type(exc).__name__)

    def scheduler(self) -> AsyncIOScheduler:
        scheduler = AsyncIOScheduler(timezone=self.settings.tz)
        scheduler.add_job(
            self.scheduled,
            CronTrigger(
                hour=self.settings.report_hour,
                minute=self.settings.report_minute,
                timezone=self.settings.tz,
            ),
            id="daily-report",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        scheduler.add_job(
            self.scheduled,
            "interval",
            seconds=60,
            id="report-retry",
            max_instances=1,
            coalesce=True,
        )
        return scheduler
