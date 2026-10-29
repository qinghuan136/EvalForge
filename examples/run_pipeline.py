"""用普通占位函数串起完整流程；之后只需替换这两个函数。"""

import argparse
import json
from pathlib import Path

from evalforge import EvaluationResult, load_dataset, run_experiment, summarize, write_report


def placeholder_task(input):
    """透传输入以验证数据流；后续在此调用自己的模型或业务函数。"""
    return {"received": input}


def placeholder_evaluator(output):
    """只标记评分步骤已执行，不把占位结果当作质量分数。"""
    return EvaluationResult(
        label="placeholder", explanation="占位评分已执行，尚未判断输出质量",
    )


def main():
    parser = argparse.ArgumentParser(description="Run the in-memory EvalForge pipeline")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).with_name("text_cases.json"))
    parser.add_argument("--output", type=Path, default=Path("results/pipeline.json"))
    args = parser.parse_args()
    if args.dataset.resolve() == args.output.resolve():
        parser.error("--output must not overwrite the dataset")

    # 1. Dataset：从 JSON 读取样本。
    dataset = load_dataset(args.dataset)

    # 2. Runner：创建 Experiment，依次生成 TaskRun、EvaluationRun 和 Result。
    result = run_experiment(
        dataset=dataset,
        task=placeholder_task,
        evaluators={"placeholder": placeholder_evaluator},
        experiment_name="pipeline-v0",
    )

    # 3. 输出层：将内存中的 Result 转成终端摘要和 JSON 报告。
    report = write_report(result, args.output)
    print(f"Experiment: {result.experiment.name} ({result.experiment.id})")
    print(json.dumps(summarize(result), ensure_ascii=False, indent=2))
    print(f"Report: {report.resolve()}")


if __name__ == "__main__":
    main()
