from __future__ import annotations

import random
from datetime import date, datetime, time
from typing import Any

import digital_twin.simulation.anfis.controller as anfis_controller
from digital_twin.simulation.anfis.model import (
    ANFIS,
    probability_category,
    target_probability,
)
from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.shared.constants import (
    ANFIS_DECISION_THRESHOLD,
    ANFIS_FORECAST_DECISION_THRESHOLD,
    LOCAL_TZ,
)
from digital_twin.simulation.shared.types import PotState
from digital_twin.domain.pot import Pot
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.domain.weather import local_observed_at
from digital_twin.simulation.state.environment import StateEnvironment
from digital_twin.simulation.state.projection import StateProjector
from digital_twin.simulation.sensors.calibration import (
    lookup_sensor_reading,
    sensor_date_is_future,
    sensor_lookup_time,
)
from digital_twin.simulation.valves.zones import valve_number_for_zone

ANFIS_ZONE_MODEL_MIN_SAMPLES = 120
ANFIS_CALIBRATION_SHARE = 0.18
ANFIS_TRAINING_DATASET_VERSION = 4


def anfis_decision_threshold(
    sensor_context: dict[str, Any],
    experiment_date: date,
    decision_threshold: float = ANFIS_DECISION_THRESHOLD,
    forecast_decision_threshold: float = ANFIS_FORECAST_DECISION_THRESHOLD,
) -> float:
    if sensor_date_is_future(sensor_context, experiment_date):
        return forecast_decision_threshold
    return decision_threshold


def make_anfis_execution_decision(
    state: PotState,
    pot: dict[str, Any],
    weather: dict[str, Any],
    day_profile: dict[str, Any],
    slot: str,
) -> dict[str, Any]:
    observed_local = local_observed_at(weather)
    target = pot["winter_moisture_target_pct"] if slot == "winter_check" else pot["moisture_target_pct"]
    reason_code = "anfis_probability_pending"
    reason_detail = "Valve-zone average ANFIS probability decides irrigation; runtime uses the full calculated need."
    should_irrigate = False

    if soil.number(weather.get("temperature_c"), day_profile["avg_temperature_c"]) <= 3.0:
        reason_code = "freeze_risk"
        reason_detail = "Skipped because temperature is too low for irrigation."

    return {
        "pot_id": pot["id"],
        "pot_code": pot["pot_code"],
        "decided_at": observed_local.isoformat(),
        "date": observed_local.date().isoformat(),
        "slot": slot,
        "should_irrigate": should_irrigate,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "current_moisture_pct": round(state.moisture, 2),
        "target_moisture_pct": round(target, 2),
        "weather_hourly_id": weather["id"],
        "dose_factor": 1.0,
        "dose_policy_source": "anfis_full_dose_policy",
    }


def anfis_duration_policy_note(decision: dict[str, Any]) -> str:
    return " ANFIS runtime uses the full calculated need."


def anfis_zone_probability_summary(decisions: list[dict[str, Any]]) -> tuple[float, float]:
    probabilities = [
        float(decision["predicted_probability"])
        for decision in decisions
        if decision.get("predicted_probability") is not None
    ]
    if not probabilities:
        return 0.0, 0.0
    return sum(probabilities) / len(probabilities), max(probabilities)


def anfis_inputs(
    state: PotState,
    weather: dict[str, Any],
    sensor_reading: dict[str, Any] | None,
    pot: dict[str, Any],
    day_profile: dict[str, Any],
    prior_moisture_pct: float | None = None,
) -> dict[str, float]:
    observed_day = local_observed_at(weather).date()
    rain = soil.number(day_profile.get("precipitation_mm"), soil.number(weather.get("precipitation_mm"), 0.0))
    effective_rain = rain * Pot.from_mapping(pot).rain_exposure_factor(observed_day)
    if sensor_reading:
        moisture = soil.number(sensor_reading["soil_moisture_pct"], state.moisture)
        temperature = soil.number(sensor_reading["air_temperature_c"], soil.number(weather["temperature_c"], 20.0))
    else:
        moisture = state.moisture
        temperature = soil.number(weather["temperature_c"], 20.0)
    return {
        "moisture": float(moisture),
        "temperature": float(temperature),
        "rain": float(effective_rain),
    }


def predict_anfis_probability(
    model: ANFIS | anfis_controller.AnfisModelController,
    inputs: dict[str, Any],
    zone: str | None = None,
) -> float:
    if isinstance(model, anfis_controller.AnfisModelController):
        return model.predict(inputs, zone or str(inputs.get("valve_zone") or ""))
    return model.predict(inputs)


def evaluate_anfis_model(
    model: ANFIS | anfis_controller.AnfisModelController,
    dataset: list[dict[str, float | str]],
) -> dict[str, Any]:
    matches = 0
    decision_matches = 0
    mse = 0.0
    for item in dataset:
        predicted = predict_anfis_probability(model, item)
        target_probability = float(item["target_probability"])
        mse += (predicted - target_probability) ** 2
        if probability_category(predicted) == item["target_category"]:
            matches += 1
        if (predicted >= ANFIS_DECISION_THRESHOLD) == (target_probability >= ANFIS_DECISION_THRESHOLD):
            decision_matches += 1

    mse /= max(len(dataset), 1)
    rmse = mse**0.5
    return {
        "test_mse": round(mse, 6),
        "test_rmse": round(rmse, 4),
        "test_probability_fit_percent": round(max(0.0, 1.0 - rmse) * 100.0, 2),
        "test_accuracy_percent": round(matches / max(len(dataset), 1) * 100.0, 2),
        "test_decision_accuracy_percent": round(decision_matches / max(len(dataset), 1) * 100.0, 2),
        "test_decision_threshold": ANFIS_DECISION_THRESHOLD,
        "test_samples": len(dataset),
    }



def generate_database_anfis_dataset(
    weather_rows: list[dict[str, Any]],
    pots: list[dict[str, Any]],
    samples: int,
    seed: int | None,
    sensor_context: dict[str, Any] | None = None,
    weather_by_day: dict[date, list[dict[str, Any]]] | None = None,
    day_profiles: dict[date, dict[str, Any]] | None = None,
    state_environment: StateEnvironment | None = None,
) -> list[dict[str, float | str]]:
    rng = random.Random(seed)
    if not sensor_context or not sensor_context.get("available"):
        return []

    state_environment = state_environment or StateEnvironment()
    pot_by_sensor_id = {int(pot["id"]): pot for pot in pots}
    lookup = sensor_context.get("lookup") or {}
    weather_by_day = weather_by_day or state_environment.group_weather_by_day(weather_rows)
    day_profiles = day_profiles or {}
    dataset: list[dict[str, float | str]] = []
    seen: set[tuple[date, time, int]] = set()
    latest_by_sensor: dict[int, dict[str, Any]] = {}

    exact_sensor_keys = [
        key for key in lookup.keys()
        if len(key) >= 3 and not isinstance(key[1], int)
    ]
    seen_readings: set[tuple[int, str]] = set()
    sorted_keys = sorted(
        exact_sensor_keys,
        key=lambda item: (item[0], sensor_lookup_time(item[1]), int(item[2])),
    )
    for reading_date, slot_time, sensor_id in sorted_keys:
        slot_time = sensor_lookup_time(slot_time)
        key = (reading_date, slot_time, int(sensor_id))
        if key in seen:
            continue
        seen.add(key)

        pot = pot_by_sensor_id.get(int(sensor_id))
        if pot is None:
            continue
        sensor_reading = lookup_sensor_reading(lookup, reading_date, slot_time, int(sensor_id))
        if sensor_reading is None:
            continue
        recorded_at = sensor_reading.get("recorded_at")
        reading_time_key = (
            _local_timestamp_key(recorded_at)
            if recorded_at is not None
            else f"{reading_date.isoformat()}T{slot_time.isoformat()}"
        )
        reading_identity = (
            int(sensor_id),
            reading_time_key,
        )
        if reading_identity in seen_readings:
            continue

        day_weather = weather_by_day.get(reading_date, [])
        if not day_weather:
            continue
        observed_at = datetime.combine(reading_date, slot_time, tzinfo=LOCAL_TZ)
        weather = StateProjector.weather_for_hour(day_weather, observed_at)
        if weather is None:
            continue
        day_profile = day_profiles.get(reading_date) or state_environment.day_profile(reading_date, day_weather, weather_by_day)
        decision_slot = DEFAULT_IRRIGATION_POLICY.decision_slot(reading_date, observed_at, day_profile)
        if decision_slot is None:
            decision_slot = _anfis_training_slot(reading_date, day_profile, rng)

        prior_reading = latest_by_sensor.get(int(sensor_id))
        example = _anfis_training_example(
            pot,
            sensor_reading,
            weather,
            day_profile,
            decision_slot,
            prior_reading=prior_reading,
            slot_time=slot_time,
        )
        if example is not None:
            dataset.append(example)
            seen_readings.add(reading_identity)
        latest_by_sensor[int(sensor_id)] = sensor_reading

    if samples > 0 and len(dataset) > samples:
        return _weighted_anfis_dataset_sample(dataset, samples, rng)
    return dataset


def _weighted_anfis_dataset_sample(
    dataset: list[dict[str, float | str]],
    samples: int,
    rng: random.Random,
) -> list[dict[str, float | str]]:
    if samples >= len(dataset):
        return list(dataset)
    selected: list[dict[str, float | str]] = []
    remaining = list(dataset)
    while remaining and len(selected) < samples:
        total_weight = sum(max(0.1, float(item.get("training_weight", 1.0))) for item in remaining)
        pick = rng.uniform(0.0, total_weight)
        cursor = 0.0
        for index, item in enumerate(remaining):
            cursor += max(0.1, float(item.get("training_weight", 1.0)))
            if cursor >= pick:
                selected.append(item)
                del remaining[index]
                break
    return selected


def split_anfis_training_calibration(
    dataset: list[dict[str, float | str]],
    seed: int | None,
    calibration_share: float = ANFIS_CALIBRATION_SHARE,
) -> tuple[list[dict[str, float | str]], list[dict[str, float | str]]]:
    if len(dataset) < 2:
        return dataset, dataset
    rng = random.Random(None if seed is None else seed + 17)
    rows = list(dataset)
    rng.shuffle(rows)
    calibration_count = max(1, min(len(rows) - 1, int(round(len(rows) * calibration_share))))
    calibration = _stratified_anfis_test_sample(rows, calibration_count, rng)
    calibration_ids = {id(item) for item in calibration}
    training = [item for item in rows if id(item) not in calibration_ids]
    return training or rows, calibration or training or rows


def expand_anfis_training_dataset(dataset: list[dict[str, float | str]]) -> list[dict[str, float | str]]:
    expanded: list[dict[str, float | str]] = []
    for item in dataset:
        weight = max(1, min(5, int(round(float(item.get("training_weight", 1.0))))))
        expanded.extend([item] * weight)
    return expanded or dataset


def train_anfis_controller(
    train_dataset: list[dict[str, float | str]],
    calibration_dataset: list[dict[str, float | str]],
    generations: int,
    population: int,
    seed: int | None,
) -> anfis_controller.AnfisModelController:
    weighted_train = expand_anfis_training_dataset(train_dataset)
    global_model = ANFIS()
    global_model.fit(
        weighted_train,
        generations=generations,
        population=population,
        seed=seed,
    )
    global_calibrator = anfis_controller.AnfisProbabilityCalibrator.fit(global_model, calibration_dataset)
    zone_models: dict[str, ANFIS] = {}
    zone_calibrators: dict[str, anfis_controller.AnfisProbabilityCalibrator] = {}
    by_zone: dict[str, list[dict[str, float | str]]] = {}
    for item in train_dataset:
        zone = str(item.get("valve_zone") or "")
        if zone:
            by_zone.setdefault(zone, []).append(item)

    zone_generations = max(8, min(generations, generations // 2 or generations))
    zone_population = max(8, min(population, population // 2 or population))
    for zone, rows in sorted(by_zone.items()):
        if len(rows) < ANFIS_ZONE_MODEL_MIN_SAMPLES:
            continue
        zone_training, zone_calibration = split_anfis_training_calibration(
            rows,
            None if seed is None else seed + valve_number_for_zone(zone),
            calibration_share=ANFIS_CALIBRATION_SHARE,
        )
        model = ANFIS(
            membership_params=list(global_model.membership_params),
            rule_outputs=list(global_model.rule_outputs),
        )
        model.fit(
            expand_anfis_training_dataset(zone_training),
            generations=zone_generations,
            population=zone_population,
            seed=None if seed is None else seed + valve_number_for_zone(zone),
        )
        zone_models[zone] = model
        zone_calibrators[zone] = anfis_controller.AnfisProbabilityCalibrator.fit(model, zone_calibration)

    return anfis_controller.AnfisModelController(
        global_model=global_model,
        global_calibrator=global_calibrator,
        zone_models=zone_models,
        zone_calibrators=zone_calibrators,
    )


def _stratified_anfis_test_sample(
    dataset: list[dict[str, float | str]],
    test_count: int,
    rng: random.Random,
) -> list[dict[str, float | str]]:
    if test_count <= 0:
        return []

    by_category: dict[str, list[dict[str, float | str]]] = {}
    for item in dataset:
        by_category.setdefault(str(item.get("target_category", "unknown")), []).append(item)
    for rows in by_category.values():
        rng.shuffle(rows)

    selected: list[dict[str, float | str]] = []
    for rows in by_category.values():
        category_count = int(round(test_count * len(rows) / len(dataset)))
        if category_count == 0 and len(rows) > 1 and len(selected) < test_count:
            category_count = 1
        category_count = min(category_count, len(rows) - 1 if len(rows) > 1 else len(rows))
        selected.extend(rows[:category_count])

    if len(selected) < test_count:
        selected_ids = {id(item) for item in selected}
        remaining = [item for item in dataset if id(item) not in selected_ids]
        rng.shuffle(remaining)
        selected.extend(remaining[: test_count - len(selected)])
    elif len(selected) > test_count:
        rng.shuffle(selected)
        selected = selected[:test_count]

    return selected


def _anfis_training_example(
    pot: dict[str, Any],
    sensor_reading: dict[str, Any],
    weather: dict[str, Any],
    day_profile: dict[str, Any],
    slot: str,
    prior_reading: dict[str, Any] | None = None,
    slot_time: time | None = None,
) -> dict[str, float | str] | None:
    moisture = soil.number(sensor_reading.get("soil_moisture_pct"), None)
    if moisture is None:
        return None

    state = PotState(moisture=soil.clamp(float(moisture), 0.0, 100.0))
    prior_moisture = (
        soil.number(prior_reading.get("soil_moisture_pct"), None)
        if prior_reading
        else None
    )
    inputs = anfis_inputs(
        state,
        weather,
        sensor_reading,
        pot,
        day_profile,
        prior_moisture_pct=prior_moisture,
    )
    probability = anfis_training_target_probability(inputs)
    signals, weight = anfis_training_signals(pot, sensor_reading, day_profile, prior_reading, slot_time)
    return {
        **inputs,
        "target_probability": probability,
        "target_category": probability_category(probability),
        "valve_zone": str(pot.get("balcony_zone") or ""),
        "training_signals": ",".join(signals),
        "training_weight": weight,
    }


def anfis_training_signals(
    pot: dict[str, Any],
    sensor_reading: dict[str, Any],
    day_profile: dict[str, Any],
    prior_reading: dict[str, Any] | None,
    slot_time: time | None,
) -> tuple[list[str], float]:
    moisture = soil.number(sensor_reading.get("soil_moisture_pct"), 50.0)
    target = soil.number(pot.get("moisture_target_pct"), 40.0)
    min_moisture = soil.number(pot.get("moisture_min_pct"), target - 8.0)
    max_temp = soil.number(day_profile.get("max_temperature_c"), 20.0)
    rain_mm = soil.number(day_profile.get("precipitation_mm"), 0.0)
    signals = ["real_sensor_reading"]
    weight = 1.0

    if moisture <= min(target, 42.0) and max_temp >= 30.0 and rain_mm < 0.5:
        signals.append("dry_hot_no_rain")
        weight += 2.0
    elif moisture <= target and rain_mm < 0.5:
        signals.append("dry_no_rain")
        weight += 1.0

    if _is_post_irrigation_recovery_reading(sensor_reading, prior_reading, slot_time, target, moisture):
        signals.append("post_irrigation_recovery")
        weight += 1.0

    if moisture <= min_moisture:
        signals.append("below_minimum")
        weight += 1.0

    return signals, min(weight, 5.0)


def _is_post_irrigation_recovery_reading(
    sensor_reading: dict[str, Any],
    prior_reading: dict[str, Any] | None,
    slot_time: time | None,
    target: float,
    moisture: float,
) -> bool:
    if slot_time is not None and (
        time(7, 0) <= slot_time <= time(10, 30)
        or time(19, 0) <= slot_time <= time(22, 30)
    ) and moisture >= target - 2.0:
        return True

    if not prior_reading:
        return False
    prior_moisture = soil.number(prior_reading.get("soil_moisture_pct"), moisture)
    return moisture >= target - 2.0 and moisture - prior_moisture >= 2.0


def anfis_training_signal_summary(dataset: list[dict[str, float | str]]) -> dict[str, Any]:
    signal_counts: dict[str, int] = {}
    weighted_samples = 0
    zones: dict[str, int] = {}
    for item in dataset:
        weighted_samples += max(1, min(5, int(round(float(item.get("training_weight", 1.0))))))
        zone = str(item.get("valve_zone") or "")
        if zone:
            zones[zone] = zones.get(zone, 0) + 1
        for signal in str(item.get("training_signals") or "real_sensor_reading").split(","):
            if signal:
                signal_counts[signal] = signal_counts.get(signal, 0) + 1
    return {
        "samples": len(dataset),
        "weighted_samples": weighted_samples,
        "signals": dict(sorted(signal_counts.items())),
        "valve_zones": dict(sorted(zones.items())),
    }


def anfis_training_target_probability(
    inputs: dict[str, float],
) -> float:
    probability = target_probability(
        float(inputs["moisture"]),
        float(inputs["temperature"]),
        float(inputs["rain"]),
    )
    return round(soil.clamp(probability, 0.02, 0.95), 4)


def _anfis_training_slot(observed_date: date, day_profile: dict[str, Any], rng: random.Random) -> str:
    if observed_date.month in {12, 1, 2, 3}:
        return "winter_check"
    if soil.number(day_profile.get("max_temperature_c"), 20.0) >= 32.0 and rng.random() < 0.35:
        return "evening"
    return "morning"



class AnfisFeatureBuilder:
    """Builds ANFIS runtime inputs and execution decisions."""

    def decision_threshold(
        self,
        sensor_context: dict[str, Any],
        experiment_date: date,
        decision_threshold: float = ANFIS_DECISION_THRESHOLD,
        forecast_decision_threshold: float = ANFIS_FORECAST_DECISION_THRESHOLD,
    ) -> float:
        return anfis_decision_threshold(
            sensor_context,
            experiment_date,
            decision_threshold=decision_threshold,
            forecast_decision_threshold=forecast_decision_threshold,
        )

    def execution_decision(
        self,
        state: PotState,
        pot: dict[str, Any],
        weather: dict[str, Any],
        day_profile: dict[str, Any],
        slot: str,
    ) -> dict[str, Any]:
        return make_anfis_execution_decision(state, pot, weather, day_profile, slot)

    def inputs(
        self,
        state: PotState,
        weather: dict[str, Any],
        sensor_reading: dict[str, Any] | None,
        pot: dict[str, Any],
        day_profile: dict[str, Any],
        prior_moisture_pct: float | None = None,
    ) -> dict[str, float]:
        return anfis_inputs(
            state,
            weather,
            sensor_reading,
            pot,
            day_profile,
            prior_moisture_pct=prior_moisture_pct,
        )

    def duration_policy_note(self, decision: dict[str, Any]) -> str:
        return anfis_duration_policy_note(decision)

    def zone_probability_summary(self, decisions: list[dict[str, Any]]) -> tuple[float, float]:
        return anfis_zone_probability_summary(decisions)


class AnfisModelEvaluator:
    """Evaluates trained ANFIS controllers against labeled examples."""

    def predict_probability(
        self,
        model: ANFIS | anfis_controller.AnfisModelController,
        inputs: dict[str, Any],
        zone: str | None = None,
    ) -> float:
        return predict_anfis_probability(model, inputs, zone)

    def evaluate(
        self,
        model: ANFIS | anfis_controller.AnfisModelController,
        dataset: list[dict[str, float | str]],
    ) -> dict[str, Any]:
        return evaluate_anfis_model(model, dataset)


class AnfisDatasetBuilder:
    """Builds and describes training datasets from sensor/weather snapshots."""

    def generate_database_dataset(
        self,
        weather_rows: list[dict[str, Any]],
        pots: list[dict[str, Any]],
        samples: int,
        seed: int | None,
        sensor_context: dict[str, Any] | None = None,
        weather_by_day: dict[date, list[dict[str, Any]]] | None = None,
        day_profiles: dict[date, dict[str, Any]] | None = None,
        state_environment: StateEnvironment | None = None,
    ) -> list[dict[str, float | str]]:
        return generate_database_anfis_dataset(
            weather_rows,
            pots,
            samples,
            seed,
            sensor_context=sensor_context,
            weather_by_day=weather_by_day,
            day_profiles=day_profiles,
            state_environment=state_environment,
        )

    def signal_summary(self, dataset: list[dict[str, float | str]]) -> dict[str, Any]:
        return anfis_training_signal_summary(dataset)

    def training_signals(
        self,
        pot: dict[str, Any],
        sensor_reading: dict[str, Any],
        day_profile: dict[str, Any],
        prior_reading: dict[str, Any] | None,
        slot_time: time | None,
    ) -> tuple[list[str], float]:
        return anfis_training_signals(pot, sensor_reading, day_profile, prior_reading, slot_time)

    def target_probability(self, inputs: dict[str, float]) -> float:
        return anfis_training_target_probability(inputs)


class AnfisTrainer:
    """Splits, weights, and trains ANFIS controllers."""

    def split_training_calibration(
        self,
        dataset: list[dict[str, float | str]],
        seed: int | None,
        calibration_share: float = ANFIS_CALIBRATION_SHARE,
    ) -> tuple[list[dict[str, float | str]], list[dict[str, float | str]]]:
        return split_anfis_training_calibration(dataset, seed, calibration_share=calibration_share)

    def expand_training_dataset(self, dataset: list[dict[str, float | str]]) -> list[dict[str, float | str]]:
        return expand_anfis_training_dataset(dataset)

    def train_controller(
        self,
        train_dataset: list[dict[str, float | str]],
        calibration_dataset: list[dict[str, float | str]],
        generations: int,
        population: int,
        seed: int | None,
    ) -> anfis_controller.AnfisModelController:
        return train_anfis_controller(
            train_dataset,
            calibration_dataset,
            generations=generations,
            population=population,
            seed=seed,
        )


DEFAULT_ANFIS_FEATURE_BUILDER = AnfisFeatureBuilder()
DEFAULT_ANFIS_MODEL_EVALUATOR = AnfisModelEvaluator()
DEFAULT_ANFIS_DATASET_BUILDER = AnfisDatasetBuilder()
DEFAULT_ANFIS_TRAINER = AnfisTrainer()


def _local_timestamp_key(value: str | datetime) -> str:
    if isinstance(value, str):
        local_value = datetime.fromisoformat(value)
    else:
        local_value = value
    if local_value.tzinfo is not None:
        local_value = local_value.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return local_value.replace(microsecond=0).isoformat()



