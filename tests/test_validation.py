from decimal import Decimal

import pytest
from pydantic import ValidationError

from expense_bot.config import Settings
from expense_bot.handlers import BoundedBuffer, image_mime
from expense_bot.models import InvalidExtraction, parse_expenses


def test_valid_array_preserves_decimal_and_tajik():
    result = parse_expenses(
        '[{"item":"  Нон   барои хона ","amount":0.10,"category":"Личные расходы"}]'
    )
    assert result[0].amount == Decimal("0.10")
    assert result[0].item == "Нон барои хона"


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "{}",
        "null",
        "I cannot read this photo",
        '```json\n[{"item":"x","amount":1,"category":"Личные расходы"}]\n```',
        '[{"item":"x","amount":-1,"category":"Личные расходы"}]',
        '[{"item":"x","amount":0,"category":"Личные расходы"}]',
        '[{"item":"x","amount":true,"category":"Личные расходы"}]',
        '[{"item":"x","amount":"100","category":"Личные расходы"}]',
        '[{"item":"x","amount":NaN,"category":"Личные расходы"}]',
        '[{"item":"x","amount":Infinity,"category":"Личные расходы"}]',
        '[{"item":"x","amount":1.001,"category":"Личные расходы"}]',
        '[{"item":"x","amount":1000000000,"category":"Личные расходы"}]',
        '[{"item":"x","amount":1,"category":"Продукты"}]',
        '[{"item":"x","amount":1,"category":"Личные расходы","date":"2026"}]',
        '[{"item":"  ","amount":1,"category":"Личные расходы"}]',
        '[{"item":"x","amount":1,"amount":2,"category":"Личные расходы"}]',
        '[{"item":"x","amount":1,"category":"Личные расходы"},{"item":"y","amount":null,"category":"Другие расходы"}]',
    ],
)
def test_no_partial_or_invalid_writes(raw):
    with pytest.raises(InvalidExtraction):
        parse_expenses(raw)


def test_limit_rejects_whole_page():
    raw = '[{"item":"x","amount":1,"category":"Личные расходы"},{"item":"y","amount":2,"category":"Другие расходы"}]'
    with pytest.raises(InvalidExtraction):
        parse_expenses(raw, limit=1)


def test_buffer_and_mime_guard():
    with BoundedBuffer(3) as buffer:
        buffer.write(b"123")
        with pytest.raises(ValueError):
            buffer.write(b"4")
    assert image_mime(b"\xff\xd8\xffabc") == "image/jpeg"
    with pytest.raises(ValueError):
        image_mime(b"<html>not a photo")


def test_report_recipient_must_be_authorized(settings):
    values = settings.model_dump()
    values["report_chat_ids"] = "999"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
