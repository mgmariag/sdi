import csv
import json
from datetime import datetime
from pathlib import Path
import sys   #!!!!
import matplotlib.pyplot as plt

# ensure project root is on sys.path when running from tools/
ROOT = Path(__file__).resolve().parents[1]  #!!!!
if str(ROOT) not in sys.path:   #!!!!
    sys.path.insert(0, str(ROOT))    #!!!!

from DigitalTwin import DigitalTwin
from Simulator import generate_sensor_data
from Weather import get_weather_prediction


OUT = Path(__file__).resolve().parents[1] / "experiment_outputs"
OUT.mkdir(exist_ok=True)


def run_simulation(steps=1000, timestep_sec=60, twin_params=None, seed=None):
    if twin_params is None:
        twin_params = {}

    twin = DigitalTwin(**twin_params)

    logs = []

    prev_irrigation = twin.irrigation_active
    event_count = 0
    irrigation_steps = 0

    for t in range(steps):
        sensor = generate_sensor_data()
        rain = get_weather_prediction()

        twin.update_sensor_data(sensor["moisture"], sensor["temperature"], sensor["humidity"])
        twin.update_weather(rain)
        twin.evaluate_irrigation()

        state = twin.get_state()
        logs.append({
            "step": t,
            "timestamp": datetime.utcnow().isoformat(),
            "moisture": state["soil_moisture"],
            "temperature": state["temperature"],
            "humidity": state["humidity"],
            "rain_prediction": state["rain_prediction"],
            "irrigation_active": state["irrigation_active"],
        })

        if state["irrigation_active"]:
            irrigation_steps += 1

        if not prev_irrigation and state["irrigation_active"]:
            event_count += 1
        prev_irrigation = state["irrigation_active"]

    metrics = {
        "steps": steps,
        "timestep_sec": timestep_sec,
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

    # quick plot: moisture and irrigation
    steps = [r["step"] for r in logs]
    moisture = [r["moisture"] for r in logs]
    irrigation = [1 if r["irrigation_active"] else 0 for r in logs]

    plt.figure(figsize=(10, 4))
    plt.plot(steps, moisture, label="moisture")
    plt.fill_between(steps, 0, irrigation, alpha=0.2, label="irrigation")
    plt.xlabel("step")
    plt.ylabel("moisture / irrigation")
    plt.legend()
    plt.tight_layout()
    plt.savefig(base / "plot.png")
    plt.close()

    print(f"Saved results to {base}")


def main():
    # baseline run using current default threshold
    logs, metrics = run_simulation(steps=1000, timestep_sec=60)
    save_results("baseline_threshold", logs, metrics)


if __name__ == '__main__':
    main()
