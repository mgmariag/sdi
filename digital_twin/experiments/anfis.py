import math
import os
import random
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Tuple

INPUTS = ["moisture", "humidity", "temperature"]
RULE_INDICES = tuple(
    (moisture_idx, humidity_idx, temperature_idx)
    for temperature_idx in range(3)
    for humidity_idx in range(3)
    for moisture_idx in range(3)
)
DEFAULT_RANGES = {
    "moisture": (0.0, 100.0),
    "humidity": (30.0, 90.0),
    "temperature": (18.0, 36.0),
}
CATEGORY_THRESHOLDS = {
    "low": 0.3,
    "medium": 0.6,
}
_PROCESS_TRAINING_DATA: List[Dict[str, float]] = []


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def gaussian(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if abs(x - mu) < 1e-9 else 0.0
    z = (x - mu) / sigma
    return math.exp(-0.5 * z * z)


def fuzzy_level(value: float, low_max: float, medium_max: float) -> str:
    if value <= low_max:
        return "low"
    if value <= medium_max:
        return "medium"
    return "high"


def irrigation_requirement_class(moisture: float, humidity: float, temperature: float) -> str:
    moisture_level = fuzzy_level(moisture, 35.0, 55.0)
    humidity_level = fuzzy_level(humidity, 45.0, 70.0)
    temperature_level = fuzzy_level(temperature, 24.0, 30.0)

    if moisture_level == "low":
        return "high"

    if moisture_level == "medium":
        if temperature_level == "high" and humidity_level != "high":
            return "high"
        if temperature_level == "low" and humidity_level == "high":
            return "low"
        return "medium"

    if temperature_level == "high" and humidity_level == "low":
        return "medium"
    return "low"


def category_probability(category: str) -> float:
    return {
        "low": 0.15,
        "medium": 0.45,
        "high": 0.8,
    }[category]


def target_probability(moisture: float, humidity: float, temperature: float) -> float:
    category = irrigation_requirement_class(moisture, humidity, temperature)
    return category_probability(category)


def probability_category(probability: float) -> str:
    if probability < CATEGORY_THRESHOLDS["low"]:
        return "low"
    if probability < CATEGORY_THRESHOLDS["medium"]:
        return "medium"
    return "high"


def generate_anfis_dataset(samples: int = 500, seed: Optional[int] = None):
    rng = random.Random(seed)
    dataset = []
    for _ in range(samples):
        moisture = float(rng.randint(0, 100))
        temperature = float(rng.randint(18, 36))
        humidity = float(rng.randint(30, 90))
        probability = target_probability(moisture, humidity, temperature)
        category = irrigation_requirement_class(moisture, humidity, temperature)
        dataset.append({
            "moisture": moisture,
            "temperature": temperature,
            "humidity": humidity,
            "target_probability": probability,
            "target_category": category,
        })
    return dataset


def _evaluate_candidate(args: Tuple[Tuple[List[float], List[float]], List[Dict[str, float]]]) -> Tuple[float, Tuple[List[float], List[float]]]:
    """Helper function for parallel fitness evaluation. Scores a single candidate."""
    candidate, training = args
    model = ANFIS(membership_params=candidate[0], rule_outputs=candidate[1])
    score = model.score(training)
    return score, candidate


def _initialize_process_worker(training: List[Dict[str, float]]) -> None:
    global _PROCESS_TRAINING_DATA
    _PROCESS_TRAINING_DATA = training


def _evaluate_candidate_in_process(candidate: Tuple[List[float], List[float]]) -> Tuple[float, Tuple[List[float], List[float]]]:
    model = ANFIS(membership_params=candidate[0], rule_outputs=candidate[1])
    score = model.score(_PROCESS_TRAINING_DATA)
    return score, candidate


class ANFIS:
    def __init__(self, membership_params: Optional[List[float]] = None, rule_outputs: Optional[List[float]] = None):
        self.input_names = INPUTS
        self.membership_params = membership_params or self._initial_membership_params()
        # Initialize rule_outputs with rule-based defaults (non-zero) instead of zeros
        self.rule_outputs = rule_outputs or self._initial_rule_outputs()
    
    def _initial_rule_outputs(self) -> List[float]:
        """Initialize rule outputs based on heuristic irrigation requirements."""
        rule_outputs = []
        representative_values = {
            "moisture": [20.0, 50.0, 80.0],
            "humidity": [45.0, 60.0, 75.0],
            "temperature": [22.0, 27.0, 32.0],
        }
        for moisture_idx, humidity_idx, temperature_idx in self._rule_indices():
            category = irrigation_requirement_class(
                representative_values["moisture"][moisture_idx],
                representative_values["humidity"][humidity_idx],
                representative_values["temperature"][temperature_idx],
            )
            rule_outputs.append(category_probability(category))
        return rule_outputs

    def _initial_membership_params(self) -> List[float]:
        params = []
        for input_name in self.input_names:
            lo, hi = DEFAULT_RANGES[input_name]
            spread = (hi - lo) / 4.0
            params.extend([
                lo + spread * 0.0,
                spread,
                lo + spread * 1.5,
                spread,
                lo + spread * 3.0,
                spread,
            ])
        return params

    def _rule_based_candidate(self) -> Tuple[List[float], List[float]]:
        membership_params = []
        for input_name in self.input_names:
            lo, hi = DEFAULT_RANGES[input_name]
            span = hi - lo
            membership_params.extend([
                lo + span * 0.18,
                span * 0.16,
                lo + span * 0.50,
                span * 0.18,
                lo + span * 0.82,
                span * 0.16,
            ])

        rule_outputs = []
        representative_values = {
            "moisture": [20.0, 45.0, 75.0],
            "humidity": [38.0, 58.0, 80.0],
            "temperature": [21.0, 27.0, 33.0],
        }
        for moisture_idx, humidity_idx, temperature_idx in self._rule_indices():
            category = irrigation_requirement_class(
                representative_values["moisture"][moisture_idx],
                representative_values["humidity"][humidity_idx],
                representative_values["temperature"][temperature_idx],
            )
            rule_outputs.append(category_probability(category))
        return membership_params, rule_outputs

    def _unpack_membership(self, params: List[float]) -> Dict[str, List[Tuple[float, float]]]:
        memberships = {}
        i = 0
        for input_name in self.input_names:
            memberships[input_name] = []
            for _ in range(3):
                mean = float(params[i])
                sigma = float(params[i + 1])
                memberships[input_name].append((mean, max(1e-3, sigma)))
                i += 2
        return memberships

    def _rule_indices(self) -> Iterable[Tuple[int, int, int]]:
        return RULE_INDICES

    def predict(self, inputs: Dict[str, float]) -> float:
        memberships = self._unpack_membership(self.membership_params)
        return self._predict_with_memberships(inputs, memberships, self.rule_outputs)

    def _predict_with_memberships(
        self,
        inputs: Dict[str, float],
        memberships: Dict[str, List[Tuple[float, float]]],
        rule_outputs: List[float],
    ) -> float:
        moisture_memberships = [gaussian(inputs["moisture"], *params) for params in memberships["moisture"]]
        humidity_memberships = [gaussian(inputs["humidity"], *params) for params in memberships["humidity"]]
        temperature_memberships = [gaussian(inputs["temperature"], *params) for params in memberships["temperature"]]
        total_weight = 0.0
        weighted_output = 0.0
        for rule_index, (m_idx, h_idx, t_idx) in enumerate(RULE_INDICES):
            weight = moisture_memberships[m_idx] * humidity_memberships[h_idx] * temperature_memberships[t_idx]
            total_weight += weight
            weighted_output += weight * rule_outputs[rule_index]
        if total_weight <= 0.0:
            return float(sum(rule_outputs)) / max(len(rule_outputs), 1)
        return float(weighted_output / total_weight)

    def predict_category(self, inputs: Dict[str, float]) -> str:
        return probability_category(self.predict(inputs))

    def score(self, dataset: Iterable[Dict[str, float]]) -> float:
        errors = []
        memberships = self._unpack_membership(self.membership_params)
        rule_outputs = self.rule_outputs
        for example in dataset:
            predicted = self._predict_with_memberships(example, memberships, rule_outputs)
            errors.append((predicted - example["target_probability"]) ** 2)
        return sum(errors) / max(len(errors), 1)

    def _random_candidate(self, rng: random.Random) -> Tuple[List[float], List[float]]:
        membership_params = []
        for input_name in self.input_names:
            lo, hi = DEFAULT_RANGES[input_name]
            span = hi - lo
            for _ in range(3):
                mean = rng.uniform(lo, hi)
                sigma = rng.uniform(span * 0.05, span * 0.35)
                membership_params.extend([mean, sigma])
        rule_outputs = [rng.uniform(0.0, 1.0) for _ in range(27)]
        return membership_params, rule_outputs

    def _candidate_to_model(self, candidate: Tuple[List[float], List[float]]) -> "ANFIS":
        return ANFIS(membership_params=candidate[0], rule_outputs=candidate[1])

    def _mutate(self, candidate: Tuple[List[float], List[float]], rng: random.Random) -> Tuple[List[float], List[float]]:
        membership_params, rule_outputs = list(candidate[0]), list(candidate[1])
        for i in range(len(membership_params)):
            if rng.random() < 0.10:
                perturb = rng.gauss(0, 1.0)
                membership_params[i] += perturb
        for i in range(len(rule_outputs)):
            if rng.random() < 0.10:
                rule_outputs[i] = clamp(rule_outputs[i] + rng.gauss(0, 0.08), 0.0, 1.0)
        return membership_params, rule_outputs

    def _crossover(self, parent_a: Tuple[List[float], List[float]], parent_b: Tuple[List[float], List[float]], rng: random.Random) -> Tuple[List[float], List[float]]:
        membership_a, rule_a = parent_a
        membership_b, rule_b = parent_b
        child_membership = [membership_a[i] if rng.random() < 0.5 else membership_b[i] for i in range(len(membership_a))]
        child_rule = [rule_a[i] if rng.random() < 0.5 else rule_b[i] for i in range(len(rule_a))]
        return child_membership, child_rule

    def fit(
        self,
        dataset: Iterable[Dict[str, float]],
        generations: int = 80,
        population: int = 40,
        seed: Optional[int] = None,
        parallel: bool = True,
        parallel_workers: Optional[int] = None,
        parallel_backend: str = "process",
    ) -> None:
        rng = random.Random(seed)
        training = list(dataset)
        candidates: List[Tuple[List[float], List[float]]] = [self._rule_based_candidate()]
        candidates.extend(self._random_candidate(rng) for _ in range(max(population - 1, 0)))

        available_workers = os.cpu_count() or 1
        worker_count = parallel_workers or available_workers
        worker_count = max(1, min(worker_count, population))

        executor = None
        if parallel and worker_count > 1:
            if parallel_backend == "process":
                executor = ProcessPoolExecutor(
                    max_workers=worker_count,
                    initializer=_initialize_process_worker,
                    initargs=(training,),
                )
            else:
                executor = ThreadPoolExecutor(max_workers=worker_count)

        try:
            for generation in range(generations):
                if executor is not None and parallel_backend == "process":
                    scored = list(executor.map(_evaluate_candidate_in_process, candidates))
                elif executor is not None:
                    evaluation_args = [(candidate, training) for candidate in candidates]
                    scored = list(executor.map(_evaluate_candidate, evaluation_args))
                else:
                    evaluation_args = [(candidate, training) for candidate in candidates]
                    scored = [_evaluate_candidate(args) for args in evaluation_args]

                scored.sort(key=lambda x: x[0])
                elites = [candidate for _, candidate in scored[: max(2, population // 10)]]
                candidates = elites.copy()
                while len(candidates) < population:
                    parent_a = rng.choice(elites)
                    parent_b = rng.choice(elites)
                    child = self._crossover(parent_a, parent_b, rng)
                    child = self._mutate(child, rng)
                    candidates.append(child)
                if generation % 10 == 0 or generation == generations - 1:
                    best_candidate = scored[0][1]
                    self.membership_params, self.rule_outputs = best_candidate
            self.membership_params, self.rule_outputs = scored[0][1]
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

    def serialize(self) -> Dict[str, Any]:
        return {
            "membership_params": self.membership_params,
            "rule_outputs": self.rule_outputs,
        }

    @classmethod
    def deserialize(cls, payload: Dict[str, Any]) -> "ANFIS":
        return cls(membership_params=payload["membership_params"], rule_outputs=payload["rule_outputs"])


from datetime import date
from typing import Any

from digital_twin.simulation.dto import ExperimentSnapshot


class AnfisIrrigationExperiment:
    """Runs the ANFIS-GA controller against the baseline simulation."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        train_samples: int = 2000,
        test_samples: int = 800,
        seed: int | None = 2026,
        generations: int = 35,
        population: int = 24,
        parallel_workers: int | None = None,
        parallel_backend: str = "process",
        persist: bool = False,
        snapshot: ExperimentSnapshot | None = None,
    ) -> None:
        self.start_date = start_date
        self.end_date = end_date
        self.train_samples = train_samples
        self.test_samples = test_samples
        self.seed = seed
        self.generations = generations
        self.population = population
        self.parallel_workers = parallel_workers
        self.parallel_backend = parallel_backend
        self.persist = persist
        self.snapshot = snapshot

    def run(self) -> dict[str, Any]:
        from digital_twin.simulation.engine import run_daily_anfis_experiment

        return run_daily_anfis_experiment(
            start_date=self.start_date,
            end_date=self.end_date,
            train_samples=self.train_samples,
            test_samples=self.test_samples,
            seed=self.seed,
            generations=self.generations,
            population=self.population,
            parallel_workers=self.parallel_workers,
            parallel_backend=self.parallel_backend,
            persist=self.persist,
            snapshot=self.snapshot,
        )


