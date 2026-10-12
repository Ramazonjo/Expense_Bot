import asyncio
import json
import logging

import openai
import requests
from aiogram.exceptions import TelegramForbiddenError
from google.auth.exceptions import TransportError
from gspread.exceptions import APIError

from .messages import sheet_button, summary
from .models import InvalidExtraction
from .sheets import SheetConflict

log = logging.getLogger(__name__)


def classify_error(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, InvalidExtraction):
        return (
            "Не удалось надежно прочитать весь ввод. Возможны неясные цифры, другая валюта, "
            "слишком много строк или отсутствие расходов. Пришлите более четкое фото "
            "одной страницы либо расходы текстом. Ничего из этого ввода не записано.",
            False,
        )
    if isinstance(exc, SheetConflict):
        return (
            "Обнаружен конфликт строк или заголовков Google Таблицы. Запись остановлена. "
            "Администратору нужно сверить диапазон с очередью перед /retry.",
            False,
        )
    if isinstance(exc, (openai.APIConnectionError, requests.RequestException, TransportError)):
        return "Сетевая ошибка при обращении к AI или Google Sheets.", True
    if isinstance(exc, (openai.APIStatusError, APIError)):
        status = getattr(exc, "status_code", None)
        if status is None and getattr(exc, "response", None) is not None:
            status = exc.response.status_code
        if status in (408, 409, 429) or (status is not None and status >= 500):
            return "Сервис временно недоступен или превышен лимит запросов.", True
        return "Проверьте API-ключ, модель, баланс API и права доступа к таблице.", False
    return "Внутренняя ошибка. Администратору нужно проверить журнал приложения.", False


class Worker:
    def __init__(self, settings, store, sheets, recognizer, bot):
        self.settings = settings
        self.store = store
        self.sheets = sheets
        self.recognizer = recognizer
        self.bot = bot
        self.stop = asyncio.Event()

    async def process(self, job: dict):
        try:
            if job["rows_json"]:
                rows = json.loads(job["rows_json"])
            else:
                expenses = await self.recognizer.extract(job["text"], job["photo"], job["mime"])
                rows = [
                    [job["occurred_at"], e.item, float(e.amount), e.category, job["author"]]
                    for e in expenses
                ]
                await self.store.save_rows(job["id"], rows)
                job["rows_json"] = json.dumps(rows, ensure_ascii=False)
            start = job["start_row"]
            if start is None:
                start = await self.store.reserve(job["id"], await self.sheets.next_row(), len(rows))
            await self.sheets.write(start, rows)
            await self.store.complete(job["id"])
        except Exception as exc:
            # Do not log exceptions containing HTTP URLs, secrets or receipt text.
            log.error("Job %s failed: %s", job["id"], type(exc).__name__)
            reason, retry = classify_error(exc)
            await self.store.failure(job, reason, retry, self.settings.max_job_attempts)

    async def send_notices(self):
        for job in await self.store.notices():
            if job["state"] == "done":
                text = summary(job["id"], json.loads(job["rows_json"]), self.settings.currency)
            else:
                text = (
                    f"⚠️ Заявка №{job['id']} требует внимания.\n{job['error']}\n"
                    f"После устранения технической ошибки: /retry {job['id']}.\n"
                    "При сетевом сбое Google запись уже могла произойти: "
                    "используйте /retry, не отправляйте тот же расход новым сообщением."
                )
            try:
                await self.bot.send_message(
                    job["chat_id"], text, reply_markup=sheet_button(self.settings.sheet_url)
                )
                await self.store.notice_sent(job["id"])
            except TelegramForbiddenError:
                await self.store.notice_sent(job["id"])
                log.warning("Notification blocked for job %s", job["id"])
            except Exception as exc:
                log.warning("Notification failed: %s", type(exc).__name__)
                await self.store.notice_later(job["id"])

    async def run(self):
        while not self.stop.is_set():
            job = await self.store.next_job()
            if job:
                await self.process(job)
            await self.send_notices()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1)
            except TimeoutError:
                pass
