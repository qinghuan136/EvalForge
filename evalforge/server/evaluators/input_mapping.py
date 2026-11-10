"""Single-value paths shared by configuration validation and evaluation."""

import re

from pydantic import JsonValue

from ..schemas.evaluators import InputMapping

_PATH = re.compile(r"\$\.(?:input|reference|output|metadata)(?:\.[^\W\d][\w-]*|\[(?:0|[1-9][0-9]*)\])*")
_SEGMENT = re.compile(r"\.([^\W\d][\w-]*)|\[([0-9]+)\]")


def is_single_value_path(path: str) -> bool:
    return len(path) <= 1024 and _PATH.fullmatch(path) is not None


def resolve_inputs(mapping: InputMapping, context: dict[str, JsonValue]) -> dict[str, JsonValue]:
    inputs = dict(mapping.literal_mapping)
    for parameter, path in mapping.path_mapping.items():
        if parameter in inputs or not is_single_value_path(path):
            raise ValueError("Invalid evaluator input mapping")
        value: JsonValue = context
        for segment in _SEGMENT.finditer(path, 1):
            field, index = segment.groups()
            if field is not None and isinstance(value, dict) and field in value:
                value = value[field]
            elif index is not None and isinstance(value, list) and int(index) < len(value):
                value = value[int(index)]
            else:
                raise ValueError(f"Evaluator input path not found: {path}")
        inputs[parameter] = value
    return inputs
