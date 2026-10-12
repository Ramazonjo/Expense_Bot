import json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Category = Literal["Личные расходы", "Мебельный завод", "Другие расходы"]
CATEGORIES = ("Личные расходы", "Мебельный завод", "Другие расходы")
HEADERS = ["Дата и Время", "Название расхода", "Сумма", "Категория", "Кто добавил"]


class InvalidExtraction(ValueError):
    """Reject the entire model result; never silently save only part of a page."""


class Expense(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str = Field(min_length=1, max_length=250)
    amount: Decimal = Field(gt=0, le=Decimal("999999999.99"), decimal_places=2)
    category: Category

    @field_validator("item", mode="before")
    @classmethod
    def clean_item(cls, value):
        if not isinstance(value, str):
            raise ValueError("item must be a string")
        return " ".join(value.split())

    @field_validator("amount", mode="before")
    @classmethod
    def numeric_amount(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("amount must be a JSON number")
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise ValueError("amount must be finite")
        return amount


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON key")
        obj[key] = value
    return obj


def _reject_constant(value):
    raise ValueError("Nonstandard JSON number")


def parse_expenses(raw: str, limit: int = 50) -> list[Expense]:
    try:
        data = json.loads(
            raw,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
        if not isinstance(data, list) or not 1 <= len(data) <= limit:
            raise ValueError("Expected a nonempty bounded array")
        return [Expense.model_validate(item) for item in data]
    except (ValueError, TypeError, ValidationError) as exc:
        raise InvalidExtraction("Не удалось надежно распознать все расходы") from exc


def money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ")
