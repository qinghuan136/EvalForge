"""Built-in scoring rules adapted from Phoenix's server evaluators.

Copyright 2025 Arize AI, Inc. All Rights Reserved.
Modified for EvalForge from src/phoenix/server/api/evaluators.py at 14c193cfa.
See third_party/phoenix-server/LICENSE and IP_NOTICE for the upstream terms.
"""

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, JsonValue

from evalforge.models import EvaluationResult

# Phoenix casts mapped values for string fields before validating the inputs.
TextInput = Annotated[str, BeforeValidator(str)]


class BuiltinEvaluator(BaseModel, ABC):
    """Validate resolved inputs and score them without external services."""

    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    key: ClassVar[str]
    description: ClassVar[str]
    output_type: ClassVar[Literal["CATEGORICAL", "CONTINUOUS"]]

    @classmethod
    def output_configs(cls) -> list[dict[str, JsonValue]]:
        if cls.output_type == "CONTINUOUS":
            return [{"name": cls.key, "type": cls.output_type,
                     "optimization_direction": "MINIMIZE", "lower_bound": 0.0}]
        return [{
            "name": cls.key,
            "type": cls.output_type,
            "optimization_direction": "MAXIMIZE",
            "values": [{"label": "true", "score": 1.0}, {"label": "false", "score": 0.0}],
        }]

    @classmethod
    def evaluate(cls, inputs: Mapping[str, JsonValue]) -> EvaluationResult:
        return cls.model_validate(dict(inputs))._score()

    @abstractmethod
    def _score(self) -> EvaluationResult:
        ...


def _match_result(matched: bool, explanation: str) -> EvaluationResult:
    return EvaluationResult(score=float(matched), label="true" if matched else "false",
                            explanation=explanation)


class ContainsEvaluator(BuiltinEvaluator):
    key = "contains"
    description = "Checks whether the text contains any or all of the comma-separated words."
    output_type = "CATEGORICAL"

    words: TextInput
    text: TextInput
    case_sensitive: bool = False
    require_all: bool = False

    def _score(self) -> EvaluationResult:
        words = [word.strip() for word in self.words.split(",") if word.strip()]
        if not words:
            return _match_result(False, "No valid words were provided to check.")

        match_fn = all if self.require_all else any
        if self.case_sensitive:
            matched = match_fn(word in self.text for word in words)
        else:
            text_lower = self.text.lower()
            matched = match_fn(word.lower() in text_lower for word in words)
        condition = "All words" if self.require_all else "At least one word"
        outcome = "found" if matched else "not found"
        return _match_result(matched, f"{condition} required ({outcome}).")


class ExactMatchEvaluator(BuiltinEvaluator):
    key = "exact_match"
    description = "Checks whether the actual text exactly matches the expected text."
    output_type = "CATEGORICAL"

    expected: TextInput
    actual: TextInput
    case_sensitive: bool = True

    def _score(self) -> EvaluationResult:
        if self.case_sensitive:
            matched = self.expected == self.actual
        else:
            matched = self.expected.lower() == self.actual.lower()
        return _match_result(matched, f"expected {'matches' if matched else 'does not match'} actual")


class RegexEvaluator(BuiltinEvaluator):
    key = "regex"
    description = "Checks whether the text matches a regular expression."
    output_type = "CATEGORICAL"

    pattern: TextInput
    text: TextInput
    full_match: bool = False

    def _score(self) -> EvaluationResult:
        match_fn = re.fullmatch if self.full_match else re.search
        try:
            matched = match_fn(self.pattern, self.text) is not None
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc
        match_type = "full match" if self.full_match else "search"
        outcome = "matched" if matched else "did not match"
        return _match_result(matched, f"pattern {outcome} ({match_type})")


def _levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if len(s2) == 0:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


class LevenshteinDistanceEvaluator(BuiltinEvaluator):
    key = "levenshtein_distance"
    description = "Calculates the Levenshtein edit distance between two strings."
    output_type = "CONTINUOUS"

    expected: TextInput
    actual: TextInput
    case_sensitive: bool = True

    def _score(self) -> EvaluationResult:
        expected, actual = self.expected, self.actual
        if not self.case_sensitive:
            expected, actual = expected.lower(), actual.lower()
        distance = _levenshtein_distance(expected, actual)
        return EvaluationResult(score=float(distance), explanation=f"Edit distance is {distance}.")


def _json_diff_count(expected: JsonValue, actual: JsonValue) -> int:
    if type(expected) is not type(actual):
        return 1

    if isinstance(expected, dict) and isinstance(actual, dict):
        diff = 0
        all_keys = set(expected.keys()) | set(actual.keys())
        for key in all_keys:
            if key not in expected:
                diff += 1
            elif key not in actual:
                diff += 1
            else:
                diff += _json_diff_count(expected[key], actual[key])
        return diff

    if isinstance(expected, list) and isinstance(actual, list):
        diff = abs(len(expected) - len(actual))
        for i in range(min(len(expected), len(actual))):
            diff += _json_diff_count(expected[i], actual[i])
        return diff

    return 0 if expected == actual else 1


class JSONDistanceEvaluator(BuiltinEvaluator):
    key = "json_distance"
    description = "Counts structural and value differences between two JSON values."
    output_type = "CONTINUOUS"

    expected: JsonValue
    actual: JsonValue
    parse_strings: bool = True

    def _parse(self, value: JsonValue, parameter: str) -> JsonValue:
        if self.parse_strings and isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in '{parameter}': {exc}") from exc
        return value

    def _score(self) -> EvaluationResult:
        expected = self._parse(self.expected, "expected")
        actual = self._parse(self.actual, "actual")
        distance = _json_diff_count(expected, actual)
        return EvaluationResult(score=float(distance),
                                explanation=f"JSON structures have {distance} difference(s).")


_REGISTRY = {
    evaluator.key: evaluator
    for evaluator in (ContainsEvaluator, ExactMatchEvaluator, RegexEvaluator,
                      LevenshteinDistanceEvaluator, JSONDistanceEvaluator)
}


def list_builtin_evaluators() -> tuple[type[BuiltinEvaluator], ...]:
    return tuple(_REGISTRY.values())


def get_builtin_evaluator(key: str) -> type[BuiltinEvaluator]:
    return _REGISTRY[key]
