import csv
import json
import time
from datetime import datetime
from pathlib import Path
import sys

try:
    import matplotlib.pyplot as plt
except ImportError:
    plt = None

# ensure project root is on sys.path when running from tools/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from DigitalTwin import DigitalTwin
from Simulator import SoilDynamics, generate_scenario, generate_sensor_data
from tools.anfis import ANFIS, generate_anfis_dataset, probability_category


OUT = Path(__file__).resolve().parents[1] / "experiment_outputs"
OUT.mkdir(exist_ok=True)
ANFIS_DECISION_THRESHOLD = 0.6
DEFAULT_SCENARIO_SEED = 2026


def run_simulation(steps=1000, timestep_sec=60, twin_params=None, seed=None, scenario=None):
    if twin_params is None:
        twin_params = {}

    if scenario is None:
        scenario = generate_scenario(steps, seed=seed)

    twin = DigitalTwin(**twin_params)
    simulator_soil = SoilDynamics()

    logs = []

    prev_irrigation = twin.irrigation_active
    event_count = 0
    irrigation_steps = 0
    previous_water_usage_ml = 0

    for t in range(steps):
        conditions = scenario[t]
        sensor = generate_sensor_data(
            soil_instance=simulator_soil,
            irrigation_ml=previous_water_usage_ml,
            conditions=conditions,
        )
        rain = conditions["rain"]

        twin.update_sensor_data(sensor["moisture"], sensor["temperature"], sensor["humidity"])
        twin.update_weather(rain)
        twin.evaluate_irrigation()

        state = twin.get_state()
        current_water_usage_ml = state["irrigation_flow_ml_per_min"] if state["irrigation_active"] else 0

        logs.append({
            "step": t,
            "timestamp": datetime.utcnow().isoformat(),
            "moisture": state["soil_moisture"],
            "temperature": state["temperature"],
            "humidity": state["humidity"],
            "rain_prediction": state["rain_prediction"],
            "rain_amount": round(conditions["rain_amount"], 2),
            "irrigation_active": state["irrigation_active"],
            "water_usage_l": round(current_water_usage_ml / 1000, 2),
            "water_usage_ml": current_water_usage_ml,
        })

        if state["irrigation_active"]:
            irrigation_steps += 1

        if not prev_irrigation and state["irrigation_active"]:
            event_count += 1
        prev_irrigation = state["irrigation_active"]
        previous_water_usage_ml = current_water_usage_ml

    metrics = {
        "steps": steps,
        "timestep_sec": timestep_sec,
        "scenario_seed": seed,
        "irrigation_steps": irrigation_steps,
        "irrigation_event_count": event_count,
        "percent_time_irrigation": irrigation_steps / steps * 100.0,
    }

    return logs, metrics


def save_results(name, logs, metrics):
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base = OUT / f"{name}_{now}"
    base.mkdir(parents=True, exist_ok=True)

    csv_file = base / "trace.csv"
    with csv_file.open("w", newline='', encoding='utf-8') as cf:
        writer = csv.DictWriter(cf, fieldnames=list(logs[0].keys()))
        writer.writeheader()
        writer.writerows(logs)

    (base / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding='utf-8')

    steps = [r["step"] for r in logs]
    moisture = [r["moisture"] for r in logs]

    if plt is not None:
        plt.figure(figsize=(10, 4))
        plt.plot(steps, moisture, label="moisture")

        if "baseline_irrigation_active" in logs[0] and "sparse_irrigation_active" in logs[0]:
            baseline_irrigation = [1 if r["baseline_irrigation_active"] else 0 for r in logs]
            sparse_irrigation = [1 if r["sparse_irrigation_active"] else 0 for r in logs]
            plt.fill_between(steps, 0, baseline_irrigation, alpha=0.2, label="baseline irrigation")
            plt.fill_between(steps, 0, sparse_irrigation, alpha=0.2, label="sparse irrigation")
        else:
            irrigation = [1 if r.get("irrigation_active") else 0 for r in logs]
            plt.fill_between(steps, 0, irrigation, alpha=0.2, label="irrigation")

        plt.xlabel("step")
        plt.ylabel("moisture / irrigation")
        plt.legend()
        plt.tight_layout()
        plt.savefig(base / "plot.png")
        plt.close()
    else:
        print("matplotlib not available; skipping plot generation")

    print(f"Saved results to {base}")


def _evaluate_anfis_model(model: ANFIS, dataset: list) -> dict:
    matches = 0
    mse = 0.0
    for item in dataset:
        predicted = model.predict(item)
        mse += (predicted - item["target_probability"]) ** 2
        if probability_category(predicted) == item["target_category"]:
            matches += 1
    mse /= max(len(dataset), 1)
    return {
        "test_mse": round(mse, 6),
        "test_accuracy_percent": round(matches / max(len(dataset), 1) * 100.0, 2),
        "test_samples": len(dataset),
    }


def run_anfis_experiment(
    steps=1000,
    train_samples=500,
    test_samples=200,
    twin_params=None,
    seed=None,
    generations=60,
    population=40,
    scenario=None,
    parallel_workers=None,
    parallel_backend="process",
):
    if twin_params is None:
        twin_params = {}

    start_time = time.perf_counter()
    if scenario is None:
        scenario = generate_scenario(steps, seed=seed)

    train_dataset = generate_anfis_dataset(train_samples, seed=seed)
    test_dataset = generate_anfis_dataset(test_samples, seed=(seed + 1) if seed is not None else None)

    model = ANFIS()
    model.fit(
        train_dataset,
        generations=generations,
        population=population,
        seed=seed,
        parallel=True,
        parallel_workers=parallel_workers,
        parallel_backend=parallel_backend,
    )

    evaluation = _evaluate_anfis_model(model, test_dataset)

    baseline = DigitalTwin(**twin_params)
    anfis = DigitalTwin(**twin_params)
    baseline_soil = SoilDynamics()
    anfis_soil = SoilDynamics()

    logs = []
    prev_baseline = baseline.irrigation_active
    prev_anfis = anfis.irrigation_active
    baseline_events = 0
    baseline_steps = 0
    anfis_events = 0
    anfis_steps = 0
    baseline_usage = 0
    anfis_usage = 0
    predicted_probabilities = []

    baseline_previous_usage_ml = 0
    anfis_previous_usage_ml = 0

    for t in range(steps):
        conditions = scenario[t]
        baseline_sensor = generate_sensor_data(
            soil_instance=baseline_soil,
            irrigation_ml=baseline_previous_usage_ml,
            conditions=conditions,
        )
        anfis_sensor = generate_sensor_data(
            soil_instance=anfis_soil,
            irrigation_ml=anfis_previous_usage_ml,
            conditions=conditions,
        )
        rain = conditions["rain"]

        baseline.update_sensor_data(
            baseline_sensor["moisture"],
            baseline_sensor["temperature"],
            baseline_sensor["humidity"],
        )
        baseline.update_weather(rain)
        baseline.evaluate_irrigation()
        baseline_state = baseline.get_state()
        baseline_active = baseline_state["irrigation_active"]

        anfis.update_sensor_data(
            anfis_sensor["moisture"],
            anfis_sensor["temperature"],
            anfis_sensor["humidity"],
        )
        anfis.update_weather(rain)

        predicted_probability = model.predict(anfis_sensor)
        predicted_probabilities.append(predicted_probability)
        anfis_active = predicted_probability >= ANFIS_DECISION_THRESHOLD
        anfis.irrigation_active = anfis_active
        anfis_state = anfis.get_state()

        current_baseline_usage_ml = baseline_state["irrigation_flow_ml_per_min"] if baseline_active else 0
        current_anfis_usage_ml = anfis_state["irrigation_flow_ml_per_min"] if anfis_active else 0

        if baseline_active:
            baseline_steps += 1
        if anfis_active:
            anfis_steps += 1

        if not prev_baseline and baseline_active:
            baseline_events += 1
        if not prev_anfis and anfis_active:
            anfis_events += 1

        logs.append({
            "step": t,
            "timestamp": datetime.utcnow().isoformat(),
            "moisture": baseline_state["soil_moisture"],
            "baseline_moisture": baseline_state["soil_moisture"],
            "anfis_moisture": anfis_state["soil_moisture"],
            "temperature": baseline_state["temperature"],
            "humidity": baseline_state["humidity"],
            "rain_prediction": rain,
            "rain_amount": round(conditions["rain_amount"], 2),
            "predicted_probability": round(predicted_probability, 4),
            "predicted_probability_percent": round(predicted_probability * 100, 2),
            "predicted_category": probability_category(predicted_probability),
            "baseline_irrigation_active": baseline_active,
            "anfis_irrigation_active": anfis_active,
            "baseline_water_usage_l": round(current_baseline_usage_ml / 1000, 2),
            "baseline_water_usage_ml": current_baseline_usage_ml,
            "anfis_water_usage_l": round(current_anfis_usage_ml / 1000, 2),
            "anfis_water_usage_ml": current_anfis_usage_ml,
        })

        prev_baseline = baseline_active
        prev_anfis = anfis_active
        baseline_previous_usage_ml = current_baseline_usage_ml
        anfis_previous_usage_ml = current_anfis_usage_ml

    execution_time_seconds = round(time.perf_counter() - start_time, 3)

    # Compute predicted probability statistics
    pred_prob_mean = round(sum(predicted_probabilities) / max(len(predicted_probabilities), 1), 4) if predicted_probabilities else 0.0
    pred_prob_min = round(min(predicted_probabilities), 4) if predicted_probabilities else 0.0
    pred_prob_max = round(max(predicted_probabilities), 4) if predicted_probabilities else 0.0

    metrics = {
        "steps": steps,
        "baseline_irrigation_steps": baseline_steps,
        "anfis_irrigation_steps": anfis_steps,
        "baseline_irrigation_event_count": baseline_events,
        "anfis_irrigation_event_count": anfis_events,
        "baseline_total_water_usage_l": round(baseline_steps * twin_params.get("irrigation_flow_ml_per_min", 10.0) / 1000, 2),
        "anfis_total_water_usage_l": round(anfis_steps * twin_params.get("irrigation_flow_ml_per_min", 10.0) / 1000, 2),
        "baseline_threshold": twin_params.get("threshold", 35),
        "anfis_probability_threshold": ANFIS_DECISION_THRESHOLD,
        "predicted_probability_mean": pred_prob_mean,
        "predicted_probability_min": pred_prob_min,
        "predicted_probability_max": pred_prob_max,
        "scenario_seed": seed,
        "train_samples": train_samples,
        "test_samples": test_samples,
        "parallel_workers": parallel_workers,
        "parallel_backend": parallel_backend,
        "execution_time_seconds": execution_time_seconds,
        **evaluation,
    }

    return logs, metrics


def run_sampling_experiment(steps=1000, sample_interval=10, twin_params=None, seed=None, scenario=None):
    if twin_params is None:
        twin_params = {}

    if scenario is None:
        scenario = generate_scenario(steps, seed=seed)

    baseline = DigitalTwin(**twin_params)
    sparse = DigitalTwin(**twin_params)
    baseline_soil = SoilDynamics()
    sparse_soil = SoilDynamics()

    logs = []
    prev_baseline = baseline.irrigation_active
    prev_sparse = sparse.irrigation_active

    baseline_events = 0
    sparse_events = 0
    baseline_irrigation_steps = 0
    sparse_irrigation_steps = 0
    matches = 0
    mismatches = 0

    baseline_previous_usage_ml = 0
    sparse_previous_usage_ml = 0

    for t in range(steps):
        conditions = scenario[t]
        baseline_sensor = generate_sensor_data(
            soil_instance=baseline_soil,
            irrigation_ml=baseline_previous_usage_ml,
            conditions=conditions,
        )
        sparse_sensor = generate_sensor_data(
            soil_instance=sparse_soil,
            irrigation_ml=sparse_previous_usage_ml,
            conditions=conditions,
        )
        rain = conditions["rain"]

        baseline.update_sensor_data(
            baseline_sensor["moisture"],
            baseline_sensor["temperature"],
            baseline_sensor["humidity"],
        )
        baseline.update_weather(rain)
        baseline.evaluate_irrigation()

        if t % sample_interval == 0:
            sparse.update_sensor_data(
                sparse_sensor["moisture"],
                sparse_sensor["temperature"],
                sparse_sensor["humidity"],
            )
        sparse.update_weather(rain)
        sparse.evaluate_irrigation()

        baseline_state = baseline.get_state()
        sparse_state = sparse.get_state()

        baseline_active = baseline_state["irrigation_active"]
        sparse_active = sparse_state["irrigation_active"]

        current_baseline_usage_ml = baseline_state["irrigation_flow_ml_per_min"] if baseline_active else 0
        current_sparse_usage_ml = sparse_state["irrigation_flow_ml_per_min"] if sparse_active else 0

        logs.append({
            "step": t,
            "timestamp": datetime.utcnow().isoformat(),
            "moisture": baseline_state["soil_moisture"],
            "baseline_moisture": baseline_state["soil_moisture"],
            "sparse_moisture": sparse_state["soil_moisture"],
            "temperature": baseline_state["temperature"],
            "humidity": baseline_state["humidity"],
            "rain_prediction": baseline_state["rain_prediction"],
            "rain_amount": round(conditions["rain_amount"], 2),
            "baseline_irrigation_active": baseline_active,
            "sparse_irrigation_active": sparse_active,
            "baseline_water_usage_l": round(current_baseline_usage_ml / 1000, 2),
            "baseline_water_usage_ml": current_baseline_usage_ml,
            "sparse_water_usage_l": round(current_sparse_usage_ml / 1000, 2),
            "sparse_water_usage_ml": current_sparse_usage_ml,
            "sample_interval": sample_interval,
        })

        if baseline_active:
            baseline_irrigation_steps += 1
        if sparse_active:
            sparse_irrigation_steps += 1

        if not prev_baseline and baseline_active:
            baseline_events += 1
        if not prev_sparse and sparse_active:
            sparse_events += 1

        if baseline_active == sparse_active:
            matches += 1
        else:
            mismatches += 1

        prev_baseline = baseline_active
        prev_sparse = sparse_active
        baseline_previous_usage_ml = current_baseline_usage_ml
        sparse_previous_usage_ml = current_sparse_usage_ml

    metrics = {
        "steps": steps,
        "sample_interval": sample_interval,
        "baseline_irrigation_steps": baseline_irrigation_steps,
        "sparse_irrigation_steps": sparse_irrigation_steps,
        "baseline_irrigation_event_count": baseline_events,
        "sparse_irrigation_event_count": sparse_events,
        "baseline_total_water_usage_l": round(baseline_irrigation_steps * baseline_state["irrigation_flow_ml_per_min"] / 1000, 2),
        "sparse_total_water_usage_l": round(sparse_irrigation_steps * sparse_state["irrigation_flow_ml_per_min"] / 1000, 2),
        "accuracy_percent": round(matches / max(steps, 1) * 100.0, 2),
        "mismatch_steps": mismatches,
        "threshold": twin_params.get("threshold", 35),
        "hysteresis_width": twin_params.get("hysteresis_width", 0),
        "flow_rate_ml": twin_params.get("irrigation_flow_ml_per_min", 10.0),
        "scenario_seed": seed,
    }

    return logs, metrics


def main():
    scenario_seed = DEFAULT_SCENARIO_SEED

    # baseline run using current default threshold
    logs, metrics = run_simulation(steps=1000, timestep_sec=60, seed=scenario_seed)
    save_results("baseline_threshold", logs, metrics)

    # compare full sampling vs sparse sampling at multiple intervals
    for interval in [5, 10, 20]:
        logs, metrics = run_sampling_experiment(steps=1000, sample_interval=interval, seed=scenario_seed)
        save_results(f"sampling_interval_{interval}", logs, metrics)


if __name__ == '__main__':
    main()
