"""同步两阶段 Runner：先执行所有 Task，再对输出逐项评分。

参考 Phoenix Client 的参数绑定、数据隔离、TaskRun/EvaluationRun 分离设计。
这里仅保留本地执行，不调用 Phoenix，也不加载其服务端。
"""

import inspect
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from .evaluators import normalize_evaluation
from .models import Dataset, EvaluationRun, ExperimentResult, TaskRun

_TASK_PARAMS = {"input", "expected", "reference", "metadata", "example"}
_EVAL_PARAMS = _TASK_PARAMS | {"output"}


def _bind(signature: inspect.Signature, context: dict[str, Any], fallback: str):
    """单参函数可用任意名字；多参函数按约定名称获取数据。"""
    args, kwargs = [], {}
    for parameter in signature.parameters.values():
        name = parameter.name
        if name in context:
            value = deepcopy(context[name])
        elif len(signature.parameters) == 1:
            value = deepcopy(context[fallback])
        elif parameter.default is not inspect.Parameter.empty:
            # 位置专用参数也要补上默认值，避免后面的参数错位。
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                args.append(parameter.default)
            continue
        else:
            raise ValueError(f"Unsupported required parameter: {name}")
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            args.append(value)
        else:
            kwargs[name] = value
    return signature.bind(*args, **kwargs)


def _prepare(function: Callable[..., Any], names: set[str], fallback: str):
    if not callable(function):
        raise ValueError("Task and evaluators must be callable")
    if inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(function.__call__):
        raise ValueError("V0 only supports synchronous functions")
    signature = inspect.signature(function)
    if not signature.parameters:
        raise ValueError("Function must accept at least one parameter")
    if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in signature.parameters.values()):
        raise ValueError("V0 does not support *args or **kwargs")
    # 执行前检查签名；配置错误不应等到任务已产生副作用后才发现。
    _bind(signature, dict.fromkeys(names), fallback)
    return signature


def _invoke(function, signature, context, fallback):
    arguments = _bind(signature, context, fallback)
    value = function(*arguments.args, **arguments.kwargs)
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise ValueError("V0 cannot execute an awaitable returned by a synchronous function")
    return value


def run_experiment(
    *, dataset: Dataset, task: Callable[..., Any],
    evaluators: Mapping[str, Callable[..., Any]], experiment_name: str = "experiment",
) -> ExperimentResult:
    task_signature = _prepare(task, _TASK_PARAMS, "input")
    if not evaluators:
        raise ValueError("At least one evaluator is required")
    evaluation_functions = []
    for name, evaluator in evaluators.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Evaluator names must be non-empty strings")
        evaluation_functions.append((name, evaluator, _prepare(evaluator, _EVAL_PARAMS, "output")))

    # 拍下本次实验的样本快照；回调拿到各自独立的副本。
    examples = deepcopy(dataset.examples)
    examples_by_id = {example.id: example for example in examples}
    task_runs = []
    for example in examples:
        context = {
            "input": example.input, "expected": example.expected,
            "reference": example.expected, "metadata": example.metadata, "example": example,
        }
        output, error = None, None
        try:
            output = _invoke(task, task_signature, context, "input")
            # 提前发现不能写入报告的输出，并把它当作当前 Task 的错误。
            json.dumps(output, ensure_ascii=False, allow_nan=False)
            output = deepcopy(output)
        except Exception as exc:
            output, error = None, f"{type(exc).__name__}: {exc}"
        task_runs.append(TaskRun(
            example.id, deepcopy(example.input), deepcopy(example.expected),
            deepcopy(example.metadata), output, error,
        ))

    evaluation_runs = []
    for run in task_runs:
        example = examples_by_id[run.example_id]
        context = {
            "input": example.input, "expected": example.expected,
            "reference": example.expected, "metadata": example.metadata,
            "example": example, "output": run.output,
        }
        for name, evaluator, signature in evaluation_functions:
            if run.error is not None:
                evaluation_runs.append(EvaluationRun(run.example_id, name, "skipped"))
                continue
            try:
                value = _invoke(evaluator, signature, context, "output")
                result = normalize_evaluation(value)
                evaluation_runs.append(EvaluationRun(run.example_id, name, "ok", result))
            except Exception as exc:
                evaluation_runs.append(EvaluationRun(
                    run.example_id, name, "error", error=f"{type(exc).__name__}: {exc}",
                ))
    return ExperimentResult(experiment_name, dataset.name, task_runs, evaluation_runs)
