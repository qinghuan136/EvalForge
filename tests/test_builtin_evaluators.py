import unittest

from pydantic import ValidationError

from evalforge.server.evaluators.builtin import get_builtin_evaluator, list_builtin_evaluators


class BuiltinEvaluatorTests(unittest.TestCase):
    def test_registered_rules_expose_input_and_output_definitions(self):
        expected = {
            "contains": ("CATEGORICAL", "MAXIMIZE"),
            "exact_match": ("CATEGORICAL", "MAXIMIZE"),
            "regex": ("CATEGORICAL", "MAXIMIZE"),
            "levenshtein_distance": ("CONTINUOUS", "MINIMIZE"),
            "json_distance": ("CONTINUOUS", "MINIMIZE"),
        }
        self.assertEqual({rule.key for rule in list_builtin_evaluators()}, expected.keys())
        for key, (output_type, direction) in expected.items():
            with self.subTest(key=key):
                rule = get_builtin_evaluator(key)
                self.assertTrue(rule.model_json_schema()["required"])
                config, = rule.output_configs()
                self.assertEqual((config["name"], config["type"], config["optimization_direction"]),
                                 (key, output_type, direction))

    def test_text_match_options_and_string_coercion(self):
        cases = (
            ("contains", {"words": "YES, no", "text": "yesterday"}, True),
            ("contains", {"words": "yes,no", "text": "yes", "require_all": True}, False),
            ("contains", {"words": " , ", "text": "yes", "require_all": True}, False),
            ("exact_match", {"actual": "Hello", "expected": "hello"}, False),
            ("exact_match", {"actual": "Hello", "expected": "hello", "case_sensitive": False}, True),
            ("exact_match", {"actual": " hello", "expected": "hello"}, False),
            ("exact_match", {"actual": 123, "expected": "123"}, True),
            ("regex", {"pattern": r"\d+", "text": "ID 42"}, True),
            ("regex", {"pattern": r"\d+", "text": "ID 42", "full_match": True}, False),
        )
        for key, inputs, matched in cases:
            with self.subTest(key=key, inputs=inputs):
                result = get_builtin_evaluator(key).evaluate(inputs)
                self.assertEqual(result.score, float(matched))
                self.assertEqual(result.label, str(matched).lower())

    def test_distance_rules_keep_phoenix_scoring(self):
        cases = (
            ("levenshtein_distance", {"expected": "kitten", "actual": "sitting"}, 3.0),
            ("levenshtein_distance", {"expected": "", "actual": "ab"}, 2.0),
            ("json_distance", {"expected": {"items": [True, 1]},
                               "actual": '{"items": [1, 1.0, null]}'}, 3.0),
            ("json_distance", {"expected": "plain", "actual": "plain", "parse_strings": False}, 0.0),
        )
        for key, inputs, score in cases:
            with self.subTest(key=key, inputs=inputs):
                result = get_builtin_evaluator(key).evaluate(inputs)
                self.assertEqual(result.score, score)
                self.assertIsNone(result.label)

    def test_invalid_input_raises_instead_of_returning_a_score(self):
        with self.assertRaises(ValidationError):
            get_builtin_evaluator("exact_match").evaluate({"expected": "missing actual"})
        with self.assertRaisesRegex(ValueError, "Invalid regex pattern"):
            get_builtin_evaluator("regex").evaluate({"pattern": "[", "text": "hello"})
        with self.assertRaisesRegex(ValueError, "Invalid JSON in 'actual'"):
            get_builtin_evaluator("json_distance").evaluate({"expected": {}, "actual": "plain"})
