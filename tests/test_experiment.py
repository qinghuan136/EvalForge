import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evalforge import (
    Dataset, EvaluationResult, Example, load_dataset, normalize_evaluation,
    run_experiment, summarize, write_report,
)

ROOT = Path(__file__).resolve().parents[1]


class ExperimentTests(unittest.TestCase):
    def dataset(self):
        return Dataset("small", [
            Example("one", {"value": 1}, 2, {"tag": "original"}),
            Example("two", {"value": 2}, 4),
        ])

    def test_local_dataset_task_evaluator_report_end_to_end(self):
        dataset = load_dataset(ROOT / "examples/text_cases.json")
        result = run_experiment(
            dataset=dataset, task=lambda input: " ".join(input["text"].lower().split()),
            evaluators={"exact": lambda output, expected: output == expected},
        )
        self.assertEqual([run.example_id for run in result.task_runs], [ex.id for ex in dataset.examples])
        self.assertEqual([e.result.score for e in result.evaluation_runs], [1, 1, 1, 0])
        self.assertEqual(summarize(result)["metrics"]["exact"]["mean_score"], 0.75)
        with tempfile.TemporaryDirectory() as tmp:
            report = write_report(result, Path(tmp) / "nested/result.json")
            document = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(document["task_runs"][2]["output"], "你好 世界")
            self.assertEqual(document["evaluation_runs"][3]["result"]["score"], 0)

    def test_task_phase_finishes_before_evaluation_phase(self):
        calls = []

        def task(input):
            calls.append("task")
            return input["value"] * 2

        def evaluator(output):
            calls.append("evaluation")
            return output

        run_experiment(dataset=self.dataset(), task=task, evaluators={"metric": evaluator})
        self.assertEqual(calls, ["task", "task", "evaluation", "evaluation"])

    def test_named_alias_keyword_only_and_default_parameters(self):
        def task(input, *, reference, metadata, example, suffix="!"):
            self.assertEqual(reference, example.expected)
            self.assertEqual(metadata, example.metadata)
            self.assertEqual(suffix, "!")
            return input["value"] * 2

        def evaluator(output, *, reference, input, example, metadata):
            self.assertEqual(reference, input["value"] * 2)
            self.assertEqual(example.metadata, metadata)
            return output == reference

        result = run_experiment(dataset=self.dataset(), task=task, evaluators={"match": evaluator})
        self.assertEqual(summarize(result)["metrics"]["match"]["mean_score"], 1)

    def test_single_arbitrary_parameter_and_positional_only(self):
        def task(value, /):
            return value["value"] * 2

        result = run_experiment(dataset=self.dataset(), task=task, evaluators={"double": lambda value: value})
        self.assertEqual([e.result.score for e in result.evaluation_runs], [2, 4])

    def test_positional_only_defaults_do_not_shift_later_parameters(self):
        def task(optional=3, input=None, /):
            return input["value"] + optional

        result = run_experiment(dataset=self.dataset(), task=task, evaluators={"value": lambda output: output})
        self.assertEqual([run.output for run in result.task_runs], [4, 5])

    def test_task_failure_skips_its_evaluations_and_continues(self):
        evaluated = []

        def task(input):
            if input["value"] == 1:
                raise RuntimeError("task failed")
            return 4

        def evaluator(output):
            evaluated.append(output)
            return 0

        result = run_experiment(dataset=self.dataset(), task=task, evaluators={"zero": evaluator})
        self.assertIn("RuntimeError: task failed", result.task_runs[0].error)
        self.assertEqual(evaluated, [4])
        summary = summarize(result)
        self.assertEqual(summary["task_failed"], 1)
        self.assertEqual(summary["metrics"]["zero"], {
            "evaluated": 1, "errors": 0, "skipped": 1, "scored": 1, "mean_score": 0,
        })

    def test_evaluator_failure_does_not_block_other_evaluators(self):
        def broken(output):
            raise RuntimeError("judge failed")

        result = run_experiment(
            dataset=self.dataset(), task=lambda input: input["value"],
            evaluators={"broken": broken, "good": lambda output: 1},
        )
        metrics = summarize(result)["metrics"]
        self.assertEqual(metrics["broken"]["errors"], 2)
        self.assertIsNone(metrics["broken"]["mean_score"])
        self.assertEqual(metrics["good"]["mean_score"], 1)

    def test_callbacks_cannot_mutate_dataset_outputs_or_other_evaluators(self):
        dataset = self.dataset()

        def task(input, metadata, expected):
            input["value"] = -99
            metadata["tag"] = "changed"
            return {"answer": expected}

        def mutating(output, example):
            output["answer"] = -1
            example.input["value"] = -1
            return True

        def check(output, expected, input):
            return output["answer"] == expected == input["value"] * 2

        result = run_experiment(dataset=dataset, task=task, evaluators={"mutating": mutating, "check": check})
        self.assertEqual(dataset.examples[0].input, {"value": 1})
        self.assertEqual(dataset.examples[0].metadata, {"tag": "original"})
        self.assertEqual(result.task_runs[0].output, {"answer": 2})
        self.assertEqual(summarize(result)["metrics"]["check"]["mean_score"], 1)

    def test_signature_errors_are_detected_before_any_task_executes(self):
        calls = []

        def task(input):
            calls.append(input)
            return 1

        for evaluator in (lambda: 1, lambda output, typo: 1, lambda *args: 1):
            with self.subTest(evaluator=evaluator), self.assertRaises(ValueError):
                run_experiment(dataset=self.dataset(), task=task, evaluators={"bad": evaluator})
        self.assertEqual(calls, [])

    def test_async_functions_are_rejected_without_running_task(self):
        async def asynchronous(input):
            return input

        with self.assertRaisesRegex(ValueError, "synchronous"):
            run_experiment(dataset=self.dataset(), task=asynchronous, evaluators={"score": lambda output: 1})
        with self.assertRaisesRegex(ValueError, "synchronous"):
            run_experiment(dataset=self.dataset(), task=lambda input: input, evaluators={"score": asynchronous})

    def test_awaitable_returned_by_sync_wrapper_is_a_task_error(self):
        result = run_experiment(
            dataset=self.dataset(), task=lambda input: asyncio.sleep(0),
            evaluators={"score": lambda output: 1},
        )
        self.assertEqual(summarize(result)["task_failed"], 2)
        self.assertIn("awaitable", result.task_runs[0].error)

    def test_non_json_and_non_finite_task_outputs_are_errors(self):
        for output in ({1, 2}, float("nan"), float("inf")):
            with self.subTest(output=output):
                result = run_experiment(
                    dataset=self.dataset(), task=lambda input: output,
                    evaluators={"score": lambda output: 1},
                )
                self.assertEqual(summarize(result)["task_failed"], 2)

    def test_keyboard_interrupt_is_not_swallowed(self):
        def task(input):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run_experiment(dataset=self.dataset(), task=task, evaluators={"score": lambda output: 1})

    def test_empty_evaluators_are_rejected(self):
        with self.assertRaises(ValueError):
            run_experiment(dataset=self.dataset(), task=lambda input: input, evaluators={})


class DataAndScoreTests(unittest.TestCase):
    def test_score_normalization_matches_selected_phoenix_contracts(self):
        cases = [
            (True, EvaluationResult(1, "True")),
            (False, EvaluationResult(0, "False")),
            (0.75, EvaluationResult(0.75)),
            (-2, EvaluationResult(-2)),
            ("pass", EvaluationResult(label="pass")),
            ((0.9, "close"), EvaluationResult(0.9, "close")),
            ((1, "pass", "exact"), EvaluationResult(1, "pass", "exact")),
            ({"score": 0, "explanation": "wrong"}, EvaluationResult(0, explanation="wrong")),
            (EvaluationResult(1), EvaluationResult(1)),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_evaluation(raw), expected)

    def test_invalid_scores_and_multi_output_are_rejected(self):
        for value in (None, {}, [], [{"score": 1}], float("nan"), float("inf"),
                      {"score": "1"}, {"score": 1, "label": 3}, {"unknown": 1}, (1,)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_evaluation(value)

    def test_label_only_results_have_no_artificial_numeric_average(self):
        result = run_experiment(
            dataset=Dataset("labels", [Example("x", {}, None)]),
            task=lambda input: None, evaluators={"label": lambda output: "unknown"},
        )
        metric = summarize(result)["metrics"]["label"]
        self.assertEqual(metric["evaluated"], 1)
        self.assertEqual(metric["scored"], 0)
        self.assertIsNone(metric["mean_score"])

    def test_dataset_validation(self):
        documents = [
            "{}", "[]", "[null]", '[{"id":"x","input":{}}]',
            '[{"id":"","input":{},"expected":null}]',
            '[{"id":"x","input":[],"expected":null}]',
            '[{"id":"x","input":{},"expected":null,"metadata":null}]',
            '[{"id":"x","input":{},"expected":NaN}]',
            '[{"id":"x","input":{},"expected":1e999}]',
            '[{"id":"x","input":{},"expected":1},{"id":"x","input":{},"expected":2}]',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "invalid.json"
            for content in documents:
                source.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load_dataset(source)

    def test_windows_utf8_bom_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bom.json"
            source.write_text('[{"id":"中文","input":{},"expected":null}]', encoding="utf-8-sig")
            self.assertEqual(load_dataset(source).examples[0].id, "中文")

    def test_demo_cli_creates_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "result.json"
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "examples.run_demo", "--output", str(report)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
            )
            self.assertIn('"mean_score": 0.75', completed.stdout)
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["summary"]["task_succeeded"], 4)

    def test_demo_cli_cannot_overwrite_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.json"
            source.write_text("[]", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "examples.run_demo", "--dataset", str(source), "--output", str(source)],
                cwd=ROOT, capture_output=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()
