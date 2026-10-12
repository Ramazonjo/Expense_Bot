import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from expense_bot.ai import Recognizer
from expense_bot.handlers import AccessMiddleware
from expense_bot.models import InvalidExtraction


async def test_real_aiogram_dispatch_persists_and_deduplicates_update(settings):
    from datetime import UTC, datetime

    from aiogram import Bot, Dispatcher
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Chat, Message, Update

    from expense_bot.handlers import make_router
    from expense_bot.storage import Store

    class LocalSession(BaseSession):
        def __init__(self):
            super().__init__()
            self.sent = []

        async def close(self):
            pass

        async def make_request(self, bot, method, timeout=None):
            self.sent.append(method)
            return Message(
                message_id=1000,
                date=datetime.now(UTC),
                chat=Chat(id=100, type="private"),
                text=method.text,
            )

        async def stream_content(self, *args, **kwargs):
            yield b""

    store = Store(settings.sqlite_path)
    await store.open("dispatch-test")
    session = LocalSession()
    bot = Bot(settings.telegram_bot_token.get_secret_value(), session=session)
    dp = Dispatcher()
    dp.include_router(make_router(settings, store, AsyncMock()))
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 22,
                "date": 1790357400,
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 100, "is_bot": False, "first_name": "Фаррух"},
                "text": "Клей 800 — мебельный завод",
            },
        }
    )
    try:
        await dp.feed_update(bot, update)
        await dp.feed_update(bot, update)
        jobs = await store.recent(100)
        assert len(jobs) == 1 and jobs[0]["state"] == "queued"
        assert session.sent[0].text.startswith("Распознаю данные и записываю в таблицу...")
        assert "уже принято" in session.sent[1].text
        job = await store.next_job()
        assert job["author"] == "Фаррух [id:100]"
        assert job["occurred_at"].endswith("+05:00")
    finally:
        await store.close()
        await bot.session.close()


@pytest.mark.parametrize("status", ["completed", "incomplete"])
async def test_actual_openai_sdk_payload_with_mock_transport(settings, monkeypatch, status):
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "model": "gpt-4o",
                "status": status,
                "error": None,
                "output": [
                    {
                        "type": "message",
                        "id": "msg_test",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "annotations": [],
                                "text": '[{"item":"Клей","amount":10,"category":"Мебельный завод"}]',
                            }
                        ],
                    }
                ],
            },
        )

    client = AsyncOpenAI(
        api_key="test-only",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr("expense_bot.ai.AsyncOpenAI", lambda **kwargs: client)
    recognizer = Recognizer(settings)
    try:
        if status == "incomplete":
            with pytest.raises(InvalidExtraction):
                await recognizer.extract("для завода", b"fake-photo", "image/jpeg")
        else:
            result = await recognizer.extract("для завода", b"fake-photo", "image/jpeg")
            assert result[0].amount == 10
        assert seen[0]["model"] == "gpt-4o"
        assert seen[0]["store"] is False
        assert seen[0]["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
        assert "Личные расходы" in seen[0]["instructions"]
    finally:
        await recognizer.close()


async def test_access_does_not_reach_ai_for_unauthorized_or_groups(settings):
    middleware = AccessMiddleware(settings)
    handler = AsyncMock()
    event = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=999),
        text="клей 50",
        answer=AsyncMock(),
    )
    await middleware(handler, event, {})
    handler.assert_not_awaited()
    event.from_user.id = 100
    event.chat.type = "group"
    await middleware(handler, event, {})
    handler.assert_not_awaited()
    event.chat.type = "private"
    await middleware(handler, event, {})
    handler.assert_awaited_once()
