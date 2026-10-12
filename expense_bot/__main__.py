import argparse
import asyncio
import json
import logging
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher, Router
from aiogram.types import BotCommand, Message
from dotenv import load_dotenv
from filelock import FileLock

from .ai import Recognizer
from .config import RuntimeConfigurationError, Settings
from .handlers import make_router
from .reports import Reporter
from .sheets import Sheets
from .storage import Store
from .worker import Worker

log = logging.getLogger(__name__)


async def id_only():
    """Bootstrap numeric IDs without requiring Google or OpenAI credentials."""
    load_dotenv()
    bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"])
    dp, router = Dispatcher(), Router()

    @router.message()
    async def show_id(message: Message):
        if message.chat.type == "private" and message.from_user:
            await message.answer(f"Ваш Telegram ID: {message.from_user.id}")

    dp.include_router(router)
    try:
        if (await bot.get_webhook_info()).url:
            raise RuntimeError("Existing webhook must be removed before polling")
        await dp.start_polling(bot, allowed_updates=["message"], handle_as_tasks=False)
    finally:
        await bot.session.close()


async def run(settings: Settings, check: bool):
    settings.validate_runtime_storage()
    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    # A shared SQLite path is mandatory. Multiple hosts/independent queues are not supported.
    with FileLock(str(settings.sqlite_path) + ".lock", timeout=0):
        bot = Bot(settings.telegram_bot_token.get_secret_value())
        store, sheets, recognizer = (
            Store(settings.sqlite_path),
            Sheets(settings),
            Recognizer(settings),
        )
        scheduler = worker = worker_task = monitor_task = None
        dp = Dispatcher()
        try:
            me = await bot.get_me()
            if (await bot.get_webhook_info()).url:
                raise RuntimeError("Existing webhook must be removed before polling")
            binding = json.dumps(
                [
                    me.id,
                    settings.google_spreadsheet_id,
                    settings.google_worksheet_name,
                    settings.timezone,
                    settings.currency,
                ],
                ensure_ascii=False,
            )
            await store.open(binding)
            await sheets.connect()
            if check:
                await recognizer.client.models.retrieve(settings.openai_model)
                print("OK: Telegram token, Google worksheet and OpenAI model are accessible.")
                print("No recognition request was made; sending messages is not tested.")
                return
            reporter = Reporter(settings, store, sheets, bot)
            worker = Worker(settings, store, sheets, recognizer, bot)
            dp.include_router(make_router(settings, store, reporter))
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Как добавить расходы"),
                    BotCommand(command="report", description="Отчет за сегодня"),
                    BotCommand(command="sheet", description="Google Таблица"),
                    BotCommand(command="status", description="Состояние заявок"),
                    BotCommand(command="id", description="Ваш Telegram ID"),
                ]
            )
            worker_task = asyncio.create_task(worker.run(), name="expense-worker")
            scheduler = reporter.scheduler()
            scheduler.start()
            # A startup job catches up only the most recent due report.
            scheduler.add_job(reporter.scheduled, "date", id="startup-report")

            async def monitor_worker():
                try:
                    await asyncio.shield(worker_task)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    log.critical("Worker stopped: %s", type(exc).__name__)
                if not worker.stop.is_set():
                    with suppress(RuntimeError):
                        await dp.stop_polling()

            monitor_task = asyncio.create_task(monitor_worker())
            log.info("Bot started; timezone=%s", settings.timezone)
            # Persist each accepted update before aiogram advances its polling offset.
            # Recognition runs in the durable worker, so polling never waits for AI.
            await dp.start_polling(
                bot, allowed_updates=["message"], handle_as_tasks=False, close_bot_session=False
            )
            if worker_task.done():
                await worker_task
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)
            if monitor_task:
                monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor_task
            if worker:
                worker.stop.set()
            if worker_task:
                # Let in-flight Sheets calls finish before closing the database.
                with suppress(Exception):
                    await worker_task
            await recognizer.close()
            await sheets.close()
            await store.close()
            await bot.session.close()


def main():
    parser = argparse.ArgumentParser(description="Telegram expense bot")
    parser.add_argument("--check", action="store_true", help="Check configured integrations")
    parser.add_argument("--id-only", action="store_true", help="Bootstrap Telegram user IDs")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for name in ("httpx", "httpcore", "openai", "urllib3", "aiogram"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        load_dotenv()
        mode = os.getenv("BOT_MODE", "run").strip().lower()
        if mode not in ("run", "id-only"):
            raise RuntimeConfigurationError("BOT_MODE must be run or id-only.")
        if args.id_only or mode == "id-only":
            asyncio.run(id_only())
        else:
            asyncio.run(run(Settings(), args.check))
    except KeyboardInterrupt:
        pass
    except RuntimeConfigurationError as exc:
        log.error("Configuration: %s", exc)
        raise SystemExit(1) from None
    except Exception as exc:
        # Avoid echoing API credentials/response bodies in logs.
        log.error("Startup/runtime failed: %s. See README troubleshooting.", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
