from __future__ import annotations

import math
import random
from datetime import date
from itertools import product
from typing import Any, Iterable

from digital_twin.experiments.base import EngineBackedExperiment
from digital_twin.simulation.dto import ExperimentSnapshot


DEFAULT_INPUTS = [
    "moisture",
    "temperature",
    "rain",
]
DEFAULT_RANGES = {
    "moisture": (0.0, 100.0),
    "temperature": (-5.0, 40.0),
    "rain": (0.0, 15.0),
}
CATEGORY_THRESHOLDS = {
    "low": 0.30,
    "medium": 0.60,
}


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def gaussian(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if abs(x - mu) < 1e-9 else 0.0
    z = (x - mu) / sigma
    return math.exp(-0.5 * z * z)


def probability_category(probability: float) -> str:
    if probability < CATEGORY_THRESHOLDS["low"]:
        return "low"
    if probability < CATEGORY_THRESHOLDS["medium"]:
        return "medium"
    return "high"

def _compact_rule_indices(input_names: list[str]) -> tuple[tuple[int, ...], ...]:
    """Build a tractable ANFIS rule base for the selected feature set."""
    if len(input_names) <= 3:
        return tuple(product(range(3), repeat=len(input_names)))

    input_index = {name: index for index, name in enumerate(input_names)}
    neutral = tuple(1 for _ in input_names)
    rules: set[tuple[int, ...]] = {neutral}

    primary_names = [
        name
        for name in ("moisture", "temperature", "rain")
        if name in input_index
    ]
    for levels in product(range(3), repeat=len(primary_names)):
        rule = list(neutral)
        for name, level in zip(primary_names, levels):
            rule[input_index[name]] = level
        rules.add(tuple(rule))

    moisture_index = input_index.get("moisture")
    context_names = [name for name in input_names if name not in primary_names]
    if moisture_index is not None:
        for context_name in context_names:
            context_index = input_index[context_name]
            for moisture_level in range(3):
                for context_level in range(3):
                    rule = list(neutral)
                    rule[moisture_index] = moisture_level
                    rule[context_index] = context_level
                    rules.add(tuple(rule))

    for context_name in context_names:
        context_index = input_index[context_name]
        for context_level in range(3):
            rule = list(neutral)
            rule[context_index] = context_level
            rules.add(tuple(rule))

    return tuple(sorted(rules))

def target_probability(
    moisture: float,
    temperature: float,
    rain: float,
) -> float:
    """Irrigation probability target used for ANFIS supervision."""
    dryness_score = clamp((64.0 - moisture) / 40.0, 0.0, 1.0)
    heat_score = clamp((temperature - 24.0) / 12.0, 0.0, 1.0)
    rain_score = clamp(rain / 6.0, 0.0, 1.0)
    cold_score = clamp((12.0 - temperature) / 10.0, 0.0, 1.0)
    probability = 0.06 + 0.78 * dryness_score + 0.16 * heat_score
    probability -= 0.32 * rain_score + 0.10 * cold_score
    if moisture <= 35.0 and rain < 2.0:
        probability = max(probability, 0.78)
    elif moisture <= 35.0:
        probability = max(probability, 0.55)
    elif moisture <= 42.0 and temperature >= 27.0 and rain < 2.0:
        probability = max(probability, 0.64)
    elif moisture >= 68.0:
        probability = min(probability, 0.20)
    if rain >= 8.0:
        probability = min(probability, 0.12)
    elif rain >= 4.0:
        probability = min(probability, 0.25)
    if temperature <= 3.0:
        probability = min(probability, 0.06)
    return clamp(probability, 0.02, 0.98)


class ANFIS:
    """Compact first-order ANFIS classifier trained from recorded sensor examples."""

    def __init__(
        self,
        membership_params: list[float] | None = None,
        rule_outputs: list[float] | None = None,
        input_names: list[str] | None = None,
    ):
        self.input_names = list(input_names or DEFAULT_INPUTS)
        self.rule_indices = _compact_rule_indices(self.input_names)
        self.membership_params = membership_params or self._initial_membership_params()
        self.rule_outputs = rule_outputs or self._initial_rule_outputs()

    def _initial_rule_outputs(self) -> list[float]:
        outputs = []
        representatives = {
            "moisture": [25.0, 50.0, 78.0],
            "temperature": [10.0, 24.0, 34.0],
            "rain": [0.0, 3.0, 9.0],
        }
        for rule in self.rule_indices:
            inputs = {
                input_name: representatives[input_name][level]
                for input_name, level in zip(self.input_names, rule)
            }
            outputs.append(
                target_probability(
                    float(inputs.get("moisture", 50.0)),
                    float(inputs.get("temperature", 20.0)),
                    float(inputs.get("rain", 0.0)),
                )
            )
        return outputs

    def _initial_membership_params(self) -> list[float]:
        params = []
        for input_name in self.input_names:
            lo, hi = DEFAULT_RANGES[input_name]
            span = hi - lo
            params.extend(
                [
                    lo + span * 0.16,
                    span * 0.16,
                    lo + span * 0.50,
                    span * 0.20,
                    lo + span * 0.84,
                    span * 0.16,
                ]
            )
        return params

    def _membership_params_from_dataset(self, dataset: list[dict[str, float]]) -> list[float]:
        params = []
        for input_name in self.input_names:
            values = sorted(float(row[input_name]) for row in dataset if row.get(input_name) is not None)
            if len(values) < 3:
                lo, hi = DEFAULT_RANGES[input_name]
                values = [lo, (lo + hi) / 2.0, hi]
            q10 = _percentile(values, 0.10)
            q50 = _percentile(values, 0.50)
            q90 = _percentile(values, 0.90)
            default_span = DEFAULT_RANGES[input_name][1] - DEFAULT_RANGES[input_name][0]
            span = max(q90 - q10, default_span * 0.25, 1.0)
            sigma = max(span / 5.0, 0.05)
            params.extend([q10, sigma, q50, sigma, q90, sigma])
        return params

    def _rule_outputs_from_dataset(
        self,
        membership_params: list[float],
        dataset: list[dict[str, float]],
    ) -> list[float]:
        memberships = self._unpack_membership(membership_params)
        fallback_outputs = self._initial_rule_outputs()
        weighted_sums = [0.0 for _ in self.rule_indices]
        weight_totals = [0.0 for _ in self.rule_indices]
        for example in dataset:
            input_memberships = {
                input_name: [gaussian(float(example[input_name]), *params) for params in memberships[input_name]]
                for input_name in self.input_names
            }
            target = float(example["target_probability"])
            for rule_index, rule in enumerate(self.rule_indices):
                weight = 1.0
                for input_name, level in zip(self.input_names, rule):
                    weight *= input_memberships[input_name][level]
                weighted_sums[rule_index] += weight * target
                weight_totals[rule_index] += weight
        return [
            clamp(weighted_sums[index] / weight_totals[index], 0.0, 1.0)
            if weight_totals[index] > 1e-9
            else fallback_outputs[index]
            for index in range(len(self.rule_indices))
        ]

    def _dataset_ranges(self, dataset: list[dict[str, float]]) -> dict[str, tuple[float, float]]:
        ranges = {}
        for input_name in self.input_names:
            default_lo, default_hi = DEFAULT_RANGES[input_name]
            values = [float(row[input_name]) for row in dataset if row.get(input_name) is not None]
            if not values:
                ranges[input_name] = (default_lo, default_hi)
                continue
            lo = min(min(values), default_lo)
            hi = max(max(values), default_hi)
            if math.isclose(lo, hi):
                hi = lo + 1.0
            ranges[input_name] = (lo, hi)
        return ranges

    def _unpack_membership(self, params: list[float]) -> dict[str, list[tuple[float, float]]]:
        memberships = {}
        index = 0
        for input_name in self.input_names:
            memberships[input_name] = []
            for _ in range(3):
                mean = float(params[index])
                sigma = max(1e-3, float(params[index + 1]))
                memberships[input_name].append((mean, sigma))
                index += 2
        return memberships

    def predict(self, inputs: dict[str, float]) -> float:
        memberships = self._unpack_membership(self.membership_params)
        return self._predict_with_memberships(inputs, memberships, self.rule_outputs)

    def _predict_with_memberships(
        self,
        inputs: dict[str, float],
        memberships: dict[str, list[tuple[float, float]]],
        rule_outputs: list[float],
    ) -> float:
        input_memberships = {
            input_name: [gaussian(float(inputs[input_name]), *params) for params in memberships[input_name]]
            for input_name in self.input_names
        }
        total_weight = 0.0
        weighted_output = 0.0
        for rule_index, rule in enumerate(self.rule_indices):
            weight = 1.0
            for input_name, level in zip(self.input_names, rule):
                weight *= input_memberships[input_name][level]
            total_weight += weight
            weighted_output += weight * rule_outputs[rule_index]
        if total_weight <= 0.0:
            return float(sum(rule_outputs)) / max(len(rule_outputs), 1)
        return float(weighted_output / total_weight)

    def predict_category(self, inputs: dict[str, float]) -> str:
        return probability_category(self.predict(inputs))

    def score(self, dataset: Iterable[dict[str, float]]) -> float:
        rows = list(dataset)
        if not rows:
            return 0.0
        memberships = self._unpack_membership(self.membership_params)
        mse = 0.0
        for example in rows:
            predicted = self._predict_with_memberships(example, memberships, self.rule_outputs)
            mse += (predicted - float(example["target_probability"])) ** 2
        return mse / len(rows)

    def _score_candidate(
        self,
        candidate: tuple[list[float], list[float]],
        dataset: list[dict[str, float]],
    ) -> float:
        memberships = self._unpack_membership(candidate[0])
        rule_outputs = candidate[1]
        mse = 0.0
        for example in dataset:
            predicted = self._predict_with_memberships(example, memberships, rule_outputs)
            mse += (predicted - float(example["target_probability"])) ** 2
        return mse / max(len(dataset), 1)

    def _evaluate_population(
        self,
        candidates: list[tuple[list[float], list[float]]],
        dataset: list[dict[str, float]],
    ) -> list[tuple[float, tuple[list[float], list[float]]]]:
        return [(self._score_candidate(candidate, dataset), candidate) for candidate in candidates]

    def _random_candidate(
        self,
        rng: random.Random,
        input_ranges: dict[str, tuple[float, float]],
    ) -> tuple[list[float], list[float]]:
        membership_params = []
        for input_name in self.input_names:
            lo, hi = input_ranges[input_name]
            span = max(hi - lo, 1.0)
            means = sorted(rng.uniform(lo, hi) for _ in range(3))
            for mean in means:
                sigma = rng.uniform(span * 0.05, span * 0.35)
                membership_params.extend([mean, sigma])
        rule_outputs = [rng.uniform(0.0, 1.0) for _ in self.rule_indices]
        return membership_params, rule_outputs

    def _crossover(
        self,
        parent_a: tuple[list[float], list[float]],
        parent_b: tuple[list[float], list[float]],
        rng: random.Random,
    ) -> tuple[list[float], list[float]]:
        membership_a, rule_a = parent_a
        membership_b, rule_b = parent_b
        child_membership = [
            membership_a[index] if rng.random() < 0.5 else membership_b[index]
            for index in range(len(membership_a))
        ]
        child_rule = [
            rule_a[index] if rng.random() < 0.5 else rule_b[index]
            for index in range(len(rule_a))
        ]
        return child_membership, child_rule

    def _mutate(
        self,
        candidate: tuple[list[float], list[float]],
        rng: random.Random,
        input_ranges: dict[str, tuple[float, float]],
    ) -> tuple[list[float], list[float]]:
        membership_params = list(candidate[0])
        rule_outputs = list(candidate[1])
        for input_index, input_name in enumerate(self.input_names):
            lo, hi = input_ranges[input_name]
            span = max(hi - lo, 1.0)
            base_index = input_index * 6
            for level in range(3):
                mean_index = base_index + level * 2
                sigma_index = mean_index + 1
                if rng.random() < 0.14:
                    membership_params[mean_index] = clamp(
                        membership_params[mean_index] + rng.gauss(0.0, span * 0.08),
                        lo,
                        hi,
                    )
                if rng.random() < 0.14:
                    membership_params[sigma_index] = clamp(
                        membership_params[sigma_index] + rng.gauss(0.0, span * 0.04),
                        span * 0.02,
                        span * 0.55,
                    )
        for index, value in enumerate(rule_outputs):
            if rng.random() < 0.10:
                rule_outputs[index] = clamp(value + rng.gauss(0.0, 0.08), 0.0, 1.0)
        return membership_params, rule_outputs

    def fit(
        self,
        dataset: Iterable[dict[str, float]],
        generations: int = 80,
        population: int = 40,
        seed: int | None = None,
    ) -> None:
        training = list(dataset)
        if not training:
            return

        rng = random.Random(seed)
        population_size = max(4, int(population or 4))
        generation_count = max(1, int(generations or 1))
        input_ranges = self._dataset_ranges(training)
        candidates = [(list(self.membership_params), list(self.rule_outputs))]
        dataset_membership_params = self._membership_params_from_dataset(training)
        candidates.append(
            (
                dataset_membership_params,
                self._rule_outputs_from_dataset(dataset_membership_params, training),
            )
        )
        candidates.extend(
            self._random_candidate(rng, input_ranges)
            for _ in range(max(0, population_size - len(candidates)))
        )

        best_score = math.inf
        best_candidate = candidates[0]
        elite_count = max(2, population_size // 6)
        patience = max(8, generation_count // 4)
        generations_without_improvement = 0

        for _ in range(generation_count):
            scored = self._evaluate_population(candidates, training)
            scored.sort(key=lambda item: item[0])
            current_score, current_candidate = scored[0]
            if current_score + 1e-9 < best_score:
                best_score = current_score
                best_candidate = (list(current_candidate[0]), list(current_candidate[1]))
                generations_without_improvement = 0
            else:
                generations_without_improvement += 1

            elites = [candidate for _, candidate in scored[:elite_count]]
            next_generation = [
                (list(candidate[0]), list(candidate[1]))
                for candidate in elites
            ]
            while len(next_generation) < population_size:
                parent_a = rng.choice(elites)
                parent_b = rng.choice(elites)
                child = self._crossover(parent_a, parent_b, rng)
                child = self._mutate(child, rng, input_ranges)
                next_generation.append(child)
            candidates = next_generation

            if generations_without_improvement >= patience:
                break

        calibrated_candidate = (
            list(best_candidate[0]),
            self._rule_outputs_from_dataset(list(best_candidate[0]), training),
        )
        calibrated_score = self._score_candidate(calibrated_candidate, training)
        if calibrated_score <= best_score:
            best_candidate = calibrated_candidate

        self.membership_params, self.rule_outputs = best_candidate

    def serialize(self) -> dict[str, Any]:
        return {
            "input_names": self.input_names,
            "membership_params": self.membership_params,
            "rule_outputs": self.rule_outputs,
        }

    @classmethod
    def deserialize(cls, payload: dict[str, Any]) -> "ANFIS":
        return cls(
            input_names=payload.get("input_names") or DEFAULT_INPUTS,
            membership_params=payload["membership_params"],
            rule_outputs=payload["rule_outputs"],
        )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


class AnfisIrrigationExperiment(EngineBackedExperiment):
    """Runs the ANFIS controller against the irrigation simulation."""

    engine_runner_name = "run_daily_anfis_experiment"

    def __init__(
        self,
        start_date: date,
        end_date: date,
        seed: int | None = 2026,
        generations: int = 35,
        population: int = 24,
        persist: bool = False,
        snapshot: ExperimentSnapshot | None = None,
        baseline_result: dict[str, Any] | None = None,
        trained_model: Any | None = None,
        training_metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(start_date, end_date, persist, snapshot, baseline_result)
        self.seed = seed
        self.generations = generations
        self.population = population
        self.trained_model = trained_model
        self.training_metadata = training_metadata

    def engine_parameters(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "generations": self.generations,
            "population": self.population,
            "trained_model": self.trained_model,
            "training_metadata": self.training_metadata,
        }
