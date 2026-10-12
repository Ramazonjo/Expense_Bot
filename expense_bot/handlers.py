import io
import logging
from datetime import date, datetime, timedelta

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from .messages import sheet_button
from .storage import QueueFull

log = logging.getLogger(__name__)


class BoundedBuffer(io.BytesIO):
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise ValueError("Image is too large")
        return super().write(data)


def image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Only JPEG, PNG and WebP are supported")


class AccessMiddleware(BaseMiddleware):
    def __init__(self, settings):
        self.settings = settings

    async def __call__(self, handler, event: Message, data):
        if event.chat.type != "private" or event.from_user is None:
            return
        command = (event.text or "").split(maxsplit=1)[0:1]
        if command and command[0].split("@")[0] == "/id":
            await event.answer(f"Ваш Telegram ID: {event.from_user.id}")
            return
        if event.from_user.id not in self.settings.user_ids:
            await event.answer("Доступ закрыт. Ваш ID: " + str(event.from_user.id))
            return
        return await handler(event, data)


def make_router(settings, store, reporter):
    router = Router()
    router.message.outer_middleware(AccessMiddleware(settings))

    @router.message(Command("start", "help"))
    async def help_message(message: Message):
        await message.answer(
            "Отправьте фото одной страницы с расходами или текст, например:\n"
            "Продукты 125.50 — личные\nКлей для завода 800\n\n"
            f"Валюта учета: {settings.currency}. Категории: Личные расходы, "
            "Мебельный завод, Другие расходы.\n"
            "Дата учета — время отправки сообщения; даты на фото не переносятся.\n"
            "Запись автоматическая: проверяйте сводку после распознавания.\n"
            "Фото и текст передаются в OpenAI API.\n\n"
            "/report — общий отчет за сегодня\n/report yesterday — за вчера\n"
            "/report 2026-09-25 — за указанную дату\n"
            "/sheet — открыть таблицу\n/status — последние 10 заявок\n"
            "/retry 12 — повторить неудачную заявку №12\n/id — ваш Telegram ID\n\n"
            "Все разрешенные пользователи видят общий отчет. "
            "Повторная отправка фото новым сообщением считается новым расходом.",
            reply_markup=sheet_button(settings.sheet_url),
        )

    @router.message(Command("sheet"))
    async def sheet(message: Message):
        await message.answer("Таблица расходов", reply_markup=sheet_button(settings.sheet_url))

    @router.message(Command("status"))
    async def status(message: Message):
        names = {
            "queued": "ожидает распознавания",
            "ready": "ожидает записи",
            "done": "записана",
            "failed": "нужна проверка",
        }
        jobs = await store.recent(message.chat.id)
        text = "\n".join(f"№{j['id']} — {names[j['state']]}" for j in jobs)
        await message.answer(text or "Заявок пока нет.")

    @router.message(Command("retry"))
    async def retry(message: Message):
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 12:
            await message.answer("Укажите номер заявки из /status: /retry 12")
            return
        retried = await store.retry(message.chat.id, int(parts[1]))
        await message.answer(
            "Заявка возвращена в очередь."
            if retried
            else "Не найдена ваша неудачная заявка с таким номером."
        )

    @router.message(Command("report"))
    async def report(message: Message):
        args = (message.text or "").split(maxsplit=1)
        today = datetime.now(settings.tz).date()
        try:
            day = (
                today
                if len(args) == 1
                else (
                    today - timedelta(days=1)
                    if args[1].lower() in ("yesterday", "вчера")
                    else date.fromisoformat(args[1])
                )
            )
        except ValueError:
            await message.answer("Формат: /report, /report yesterday или /report 2026-09-25")
            return
        try:
            text = await reporter.build(day)
        except Exception as exc:
            log.warning("Manual report failed: %s", type(exc).__name__)
            await message.answer("Не удалось прочитать Google Таблицу. Попробуйте позже.")
            return
        await message.answer(text, reply_markup=sheet_button(settings.sheet_url))

    @router.message(F.text.startswith("/"))
    async def unknown_command(message: Message):
        await message.answer("Неизвестная команда. Список команд: /help")

    @router.message(F.photo | F.document | F.text)
    async def expense(message: Message):
        photo, mime = None, None
        text = message.text or message.caption or ""
        if len(text) > 8000:
            await message.answer("Сократите текст до 8000 символов.")
            return
        if message.photo or message.document:
            file = message.photo[-1] if message.photo else message.document
            limit = settings.max_image_mb * 1024 * 1024
            if (file.file_size or 0) > limit:
                await message.answer(
                    f"Изображение должно быть не больше {settings.max_image_mb} МБ."
                )
                return
            if message.document and message.document.mime_type not in (
                "image/jpeg",
                "image/png",
                "image/webp",
            ):
                await message.answer("Отправьте JPEG/PNG/WebP. PDF и HEIC не поддерживаются.")
                return
            try:
                with BoundedBuffer(limit) as buffer:
                    await message.bot.download(file, destination=buffer)
                    photo = buffer.getvalue()
                mime = image_mime(photo)
            except Exception as exc:
                log.warning("Image download failed: %s", type(exc).__name__)
                await message.answer(
                    "Не удалось загрузить изображение. Отправьте JPEG/PNG до "
                    f"{settings.max_image_mb} МБ еще раз."
                )
                return
        if not photo and not text.strip():
            await message.answer("Отправьте фото или текст с названием и суммой расхода.")
            return
        user = message.from_user
        username = f" @{user.username}" if user.username else ""
        author = f"{' '.join(user.full_name.split())[:100]}{username} [id:{user.id}]"
        try:
            job_id, created = await store.enqueue(
                chat_id=message.chat.id,
                message_id=message.message_id,
                author=author,
                occurred_at=message.date.astimezone(settings.tz).isoformat(timespec="seconds"),
                text=text,
                photo=photo,
                mime=mime,
                limit=settings.max_pending_jobs,
            )
        except QueueFull:
            await message.answer("Очередь заполнена. Ввод не принят; повторите позже.")
            return
        except Exception as exc:
            log.error("Queue write failed: %s", type(exc).__name__)
            await message.answer("Не удалось сохранить заявку. Проверьте /status перед повтором.")
            return
        if not created:
            await message.answer(f"Это сообщение уже принято: заявка №{job_id}. Проверьте /status.")
            return
        await message.answer(
            f"Распознаю данные и записываю в таблицу...\nЗаявка №{job_id} сохранена в очереди."
        )

    @router.message()
    async def unsupported(message: Message):
        await message.answer("Пришлите фотографию, файл JPEG/PNG/WebP или текст расходов.")

    return router
