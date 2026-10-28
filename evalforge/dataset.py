"""V0 使用 JSON 数组代替 Phoenix 的远端 Dataset。"""

import json
from pathlib import Path
from typing import Any

from .models import Dataset, Example


def _reject_constant(value: str) -> Any:
    raise ValueError(f"Non-finite JSON number is not supported: {value}")


def load_dataset(path: str | Path) -> Dataset:
    source = Path(path)
    try:
        rows = json.loads(source.read_text(encoding="utf-8-sig"), parse_constant=_reject_constant)
        # 1e999 这样的合法数字文本也可能溢出为 inf，parse_constant 无法拦截它。
        json.dumps(rows, allow_nan=False)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Invalid dataset {source}: {exc}") from exc
    if not isinstance(rows, list):
        raise ValueError(f"Dataset {source} must be a JSON array")

    examples = []
    for index, row in enumerate(rows, start=1):
        try:
            if not isinstance(row, dict):
                raise ValueError("example must be an object")
            examples.append(Example(
                id=row["id"], input=row["input"], expected=row["expected"],
                metadata=row.get("metadata", {}),
            ))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid dataset {source}, example #{index}: {exc}") from exc
    return Dataset(name=source.stem, examples=examples)
