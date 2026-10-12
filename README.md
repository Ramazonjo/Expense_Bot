# Expense Bot — Telegram → Google Sheets

Учет расходов по тексту и фотографиям рукописных листов. Три категории: личные расходы, мебельный завод и другие расходы. Вечерние отчеты по времени Душанбе.

**Запуск на Railway:** [RAILWAY_SETUP_RU.md](RAILWAY_SETUP_RU.md).

**Архитектура, Google Service Account и локальный запуск:** [README_RU.md](README_RU.md).

## Где хранятся данные

| Место | Содержимое |
|---|---|
| GitHub | Исходный код и настройки без секретных значений |
| Railway Variables | Токен Telegram, ключ OpenAI, JSON сервисного аккаунта Google |
| Railway Volume `/data` | SQLite с очередью, резервированием строк и историей отправки отчетов |
| Google Sheets | Основная таблица расходов |

`railway.json` задает сборку через Dockerfile и команду `python -m expense_bot`. Создайте Volume вручную, внесите Variables и оставьте один экземпляр сервиса. Без Volume штатный запуск на Railway остановится, чтобы очередь не попала во временную файловую систему.

Полный Google JSON передается через `GOOGLE_SERVICE_ACCOUNT_JSON` и обрабатывается в памяти. Для локального запуска по-прежнему поддерживается `GOOGLE_SERVICE_ACCOUNT_FILE`.

При первом подключении можно временно задать `BOT_MODE=id-only`: бот покажет ваш Telegram ID без Google/OpenAI. После получения ID укажите его в `ALLOWED_USER_IDS` и `REPORT_CHAT_IDS`, внесите остальные настройки и переключите `BOT_MODE=run`.

## Проверки

```bash
python -m pip install -r requirements.lock.txt
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Результаты и границы выполненных проверок: [VALIDATION.md](VALIDATION.md).
