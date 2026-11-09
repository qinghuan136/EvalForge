"""Validation shared by browser API schemas that write to PostgreSQL."""

import math
from typing import Annotated

from pydantic import AfterValidator, Field, JsonValue

MAX_DB_ID = 2_147_483_647
PositiveDbId = Annotated[int, Field(strict=True, ge=1, le=MAX_DB_ID)]


def database_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("Text must not contain a null character")
    value.encode("utf-8")  # PostgreSQL 文本与 JSONB 不能保存孤立代理字符。
    return value


StoredText = Annotated[str, AfterValidator(database_text)]


def validate_database_json(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            database_text(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("JSON numbers must be finite")
    return value
