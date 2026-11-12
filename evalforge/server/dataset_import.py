"""将上传的 CSV 或 JSONL 解析为待保存的样本。"""

import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import chain

from pydantic import JsonValue

from .errors import APIError
from .schemas.common import validate_database_json
from .schemas.datasets import MAX_CSV_COLUMNS, MAX_DATASET_EXAMPLES, DatasetImportOptions


@dataclass(frozen=True)
class ExampleDraft:
    input: dict[str, JsonValue]
    output: dict[str, JsonValue]
    metadata: dict[str, JsonValue]


def _invalid(line: int, message: str) -> APIError:
    return APIError(422, "invalid_dataset_file", f"第 {line} 行：{message}")


def _parse_csv(file: io.StringIO, options: DatasetImportOptions) -> Iterator[ExampleDraft]:
    reader = csv.DictReader(file, strict=True)
    groups = options.input_keys, options.output_keys, options.metadata_keys
    selected = list(chain.from_iterable(groups))
    if not options.input_keys or len(set(selected)) != len(selected):
        raise APIError(422, "invalid_dataset_columns", "至少选择一列作为输入，且同一列只能选择一次")
    try:
        headers = reader.fieldnames
        if not headers or len(set(headers)) != len(headers) or any(not key.strip() or "\x00" in key for key in headers):
            raise _invalid(1, "CSV 表头必须有非空且不重复的有效列名")
        if len(headers) > MAX_CSV_COLUMNS:
            raise _invalid(1, f"CSV 最多支持 {MAX_CSV_COLUMNS} 列")
        if set(selected).difference(headers):
            raise APIError(422, "invalid_dataset_columns", "选中的列不存在于 CSV 表头")

        for row in reader:
            if None in row or None in row.values():
                raise _invalid(reader.line_num, "CSV 数据列数与表头不一致")
            if any("\x00" in row[key] for key in selected):
                raise _invalid(reader.line_num, "CSV 字段不能包含空字符")
            yield ExampleDraft(
                input={key: row[key] for key in options.input_keys},
                output={key: row[key] for key in options.output_keys},
                metadata={key: row[key] for key in options.metadata_keys},
            )
    except csv.Error:
        raise _invalid(reader.line_num + 1, "CSV 格式错误或单元格过长") from None


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate JSON keys")
    return obj


def _parse_jsonl(file: io.StringIO) -> Iterator[ExampleDraft]:
    fields = ("input", "output", "metadata")
    for line_number, line in enumerate(file, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise _invalid(line_number, "不是合法的 JSON，或包含重复字段") from None
        if not isinstance(row, dict) or row.keys() - fields:
            raise _invalid(line_number, "样本必须是仅包含 input、output、metadata 的 JSON 对象")
        for field in fields:
            value = row.get(field, {} if field != "input" else None)
            if not isinstance(value, dict):
                raise _invalid(line_number, f"{field} 必须是 JSON 对象")
            try:
                validate_database_json(value)
            except ValueError:
                raise _invalid(line_number, f"{field} 包含无效文本或非有限数值") from None
        yield ExampleDraft(row["input"], row.get("output", {}), row.get("metadata", {}))


def parse_dataset_upload(content: bytes, options: DatasetImportOptions) -> list[ExampleDraft]:
    try:
        file = io.StringIO(content.decode("utf-8-sig"), newline="")
    except UnicodeDecodeError:
        raise APIError(422, "invalid_dataset_encoding", "请上传 UTF-8 编码的文件") from None

    if options.format == "csv":
        rows = _parse_csv(file, options)
    else:
        if options.input_keys or options.output_keys or options.metadata_keys:
            raise APIError(422, "invalid_dataset_columns", "JSONL 直接读取样本字段，无需选择列")
        rows = _parse_jsonl(file)

    examples = []
    for example in rows:
        if len(examples) >= MAX_DATASET_EXAMPLES:
            raise APIError(413, "dataset_too_many_examples", f"每次最多导入 {MAX_DATASET_EXAMPLES} 条样本")
        examples.append(example)
    if not examples:
        raise APIError(422, "empty_dataset_file", "文件中没有可导入的样本")
    return examples
