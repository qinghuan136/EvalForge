"""把 Task 和 Evaluator 交给 Runner；它们不需要继承框架类。"""

import argparse
import json
from pathlib import Path

from evalforge import load_dataset, run_experiment, summarize, write_report


def normalize_text(input):
    return " ".join(input["text"].lower().split())


def exact_match(output, expected):
    return output == expected


def non_empty(output):
    return {"score": float(bool(output)), "explanation": "检查 Task 是否返回非空文本"}


def main():
    parser = argparse.ArgumentParser(description="Run the EvalForge V0 offline experiment")
    parser.add_argument("--dataset", type=Path, default=Path(__file__).with_name("text_cases.json"))
    parser.add_argument("--output", type=Path, default=Path("results/demo.json"))
    args = parser.parse_args()
    if args.dataset.resolve() == args.output.resolve():
        parser.error("--output must not overwrite the dataset")
    experiment = run_experiment(
        dataset=load_dataset(args.dataset), task=normalize_text,
        evaluators={"exact_match": exact_match, "non_empty": non_empty},
        experiment_name="text-normalization-v0",
    )
    report = write_report(experiment, args.output)
    print(json.dumps(summarize(experiment), ensure_ascii=False, indent=2))
    print(f"Report: {report.resolve()}")


if __name__ == "__main__":
    main()
