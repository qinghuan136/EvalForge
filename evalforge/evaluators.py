"""借鉴 Phoenix 的评分规范化契约，保留单个评分结果的常用返回形式。

上游定位及本地差异见 docs/phoenix-experiment-chain.md。
"""

from math import isfinite
from typing import Any

from .models import EvaluationResult


def normalize_evaluation(value: Any) -> EvaluationResult:
    if isinstance(value, EvaluationResult):
        result = value
    elif isinstance(value, bool):
        result = EvaluationResult(score=float(value), label=str(value))
    elif isinstance(value, (int, float)):
        result = EvaluationResult(score=float(value))
    elif isinstance(value, str):
        result = EvaluationResult(label=value)
    elif isinstance(value, tuple) and len(value) in (2, 3):
        # Phoenix 的二元组第二项是 label；三元组才有 explanation。
        result = EvaluationResult(*value)
    elif isinstance(value, dict):
        if set(value) - {"score", "label", "explanation"}:
            raise ValueError("Evaluation result only supports score, label and explanation")
        result = EvaluationResult(**value)
    else:
        raise ValueError(f"Unsupported evaluation result type: {type(value).__name__}")

    score = result.score
    if score is not None:
        if not isinstance(score, (int, float)) or not isfinite(score):
            raise ValueError("Evaluation score must be a finite number")
        score = float(score)
    for name in ("label", "explanation"):
        item = getattr(result, name)
        if item is not None and not isinstance(item, str):
            raise ValueError(f"Evaluation {name} must be a string")
    if score is None and result.label is None:
        raise ValueError("Evaluation must contain a score or label")
    return EvaluationResult(score, result.label, result.explanation)
