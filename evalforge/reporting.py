"""汇总只使用有效数值评分；失败、跳过、仅标签的结果不补成 0 分。"""

import json
from dataclasses import asdict
from pathlib import Path
from statistics import mean
from typing import Any

from .models import ExperimentResult


def summarize(experiment: ExperimentResult) -> dict[str, Any]:
    metrics = {}
    for evaluation in experiment.evaluation_runs:
        metric = metrics.setdefault(evaluation.evaluator, {
            "evaluated": 0, "errors": 0, "skipped": 0, "scores": [],
        })
        if evaluation.status == "skipped":
            metric["skipped"] += 1
        elif evaluation.status == "error":
            metric["errors"] += 1
        else:
            metric["evaluated"] += 1
            if evaluation.result is not None and evaluation.result.score is not None:
                metric["scores"].append(evaluation.result.score)
    for metric in metrics.values():
        scores = metric.pop("scores")
        metric["scored"] = len(scores)
        metric["mean_score"] = mean(scores) if scores else None
    failed = sum(run.error is not None for run in experiment.task_runs)
    return {
        "task_total": len(experiment.task_runs),
        "task_succeeded": len(experiment.task_runs) - failed,
        "task_failed": failed,
        "metrics": metrics,
    }


def write_report(experiment: ExperimentResult, path: str | Path) -> Path:
    document = {**asdict(experiment), "summary": summarize(experiment)}
    content = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
