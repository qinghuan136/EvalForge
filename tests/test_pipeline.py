"""验证实验身份、执行记录关联，以及不加载数据库依赖的占位闭环。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evalforge import Dataset, Example, run_experiment, summarize
from examples.run_pipeline import placeholder_evaluator, placeholder_task

ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def dataset(self):
        return Dataset("cases", [Example("one", {"text": "hello"}, "hello")])

    def run_placeholder(self):
        return run_experiment(
            dataset=self.dataset(), task=placeholder_task,
            evaluators={"placeholder": placeholder_evaluator}, experiment_name="same-name",
        )

    def test_same_configuration_creates_distinct_experiments_and_runs(self):
        first, second = self.run_placeholder(), self.run_placeholder()
        self.assertNotEqual(first.experiment.id, second.experiment.id)
        self.assertNotEqual(first.task_runs[0].id, second.task_runs[0].id)
        self.assertNotEqual(first.evaluation_runs[0].id, second.evaluation_runs[0].id)
        for result in (first, second):
            self.assertEqual(result.experiment.name, "same-name")
            self.assertEqual(result.experiment.task_name, "placeholder_task")
            self.assertEqual(result.experiment.evaluator_names, ("placeholder",))
            self.assertEqual(result.experiment_name, result.experiment.name)
            self.assertEqual(result.dataset_name, result.experiment.dataset_name)
            self.assertEqual(result.task_runs[0].experiment_id, result.experiment.id)
            self.assertEqual(result.evaluation_runs[0].task_run_id, result.task_runs[0].id)

    def test_placeholder_runs_scoring_without_inventing_quality_scores(self):
        result = self.run_placeholder()
        self.assertEqual(result.task_runs[0].output, {"received": {"text": "hello"}})
        self.assertEqual(result.evaluation_runs[0].status, "ok")
        self.assertEqual(result.evaluation_runs[0].result.label, "placeholder")
        metric = summarize(result)["metrics"]["placeholder"]
        self.assertEqual(metric["evaluated"], 1)
        self.assertEqual(metric["scored"], 0)
        self.assertIsNone(metric["mean_score"])

    def test_errors_and_skips_still_link_to_their_own_task_runs(self):
        dataset = Dataset("errors", [
            Example("bad", {"fail": True}, None),
            Example("good", {"fail": False}, None),
        ])

        def task(input):
            if input["fail"]:
                raise RuntimeError("task failed")
            return input

        def broken(output):
            raise RuntimeError("evaluation failed")

        result = run_experiment(
            dataset=dataset, task=task,
            evaluators={"broken": broken, "placeholder": placeholder_evaluator},
        )
        by_id = {run.id: run for run in result.task_runs}
        self.assertEqual([run.status for run in result.evaluation_runs], [
            "skipped", "skipped", "error", "ok",
        ])
        for evaluation in result.evaluation_runs:
            task_run = by_id[evaluation.task_run_id]
            self.assertEqual(evaluation.example_id, task_run.example_id)
            self.assertEqual(task_run.experiment_id, result.experiment.id)

    def test_invalid_experiment_name_fails_before_task_executes(self):
        calls = []
        for name in ("", "  ", None):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Experiment name"):
                run_experiment(
                    dataset=self.dataset(), task=lambda input: calls.append(input),
                    evaluators={"placeholder": placeholder_evaluator}, experiment_name=name,
                )
        self.assertEqual(calls, [])

    def test_placeholder_cli_runs_without_site_packages_and_writes_linked_report(self):
        # -S 不加载 site-packages，证明此链路不需要 SQLAlchemy / asyncpg 等依赖。
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "pipeline.json"
            completed = subprocess.run(
                [sys.executable, "-S", "-X", "utf8", "-m", "examples.run_pipeline",
                 "--output", str(report)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
            )
            document = json.loads(report.read_text(encoding="utf-8"))
        self.assertIn('"mean_score": null', completed.stdout)
        self.assertEqual(document["summary"]["task_succeeded"], 4)
        self.assertEqual(len(document["evaluation_runs"]), 4)
        task_runs = {run["id"]: run for run in document["task_runs"]}
        for evaluation in document["evaluation_runs"]:
            task_run = task_runs[evaluation["task_run_id"]]
            self.assertEqual(task_run["experiment_id"], document["experiment"]["id"])
            self.assertEqual(evaluation["example_id"], task_run["example_id"])

    def test_placeholder_cli_refuses_to_overwrite_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "cases.json"
            source.write_text("[]", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-S", "-m", "examples.run_pipeline", "--dataset", str(source),
                 "--output", str(source)],
                cwd=ROOT, capture_output=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()
