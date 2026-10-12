from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .models import money


def sheet_button(url: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Перейти в Google Таблицу", url=url)]]
    )


def summary(job_id: int, rows: list[list], currency: str) -> str:
    total = sum((Decimal(str(row[2])) for row in rows), Decimal(0))
    lines = [f"✅ Записано расходов: {len(rows)}. Заявка №{job_id}."]
    # Telegram has a message length limit; full details always remain in Sheets.
    for row in rows[:12]:
        item = str(row[1])[:100]
        lines.append(f"• {item} — {money(Decimal(str(row[2])))} {currency} ({row[3]})")
    if len(rows) > 12:
        lines.append(f"Еще {len(rows) - 12} записей — в таблице.")
    lines.append(f"Итого добавлено: {money(total)} {currency}")
    return "\n".join(lines)
