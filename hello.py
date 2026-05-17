from copy import deepcopy
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from DigitalTwin import DigitalTwin
from Simulator import SoilDynamics, generate_scenario, generate_sensor_data
from tools.experiments import DEFAULT_SCENARIO_SEED, run_anfis_experiment, run_sampling_experiment
import threading
import time

app = FastAPI()
experiment_cache = {}
DEFAULT_ANFIS_PARALLEL_WORKERS = 20
DEFAULT_ANFIS_PARALLEL_BACKEND = "process"


def get_cached_result(cache_key, compute_result):
    cache_hit = cache_key in experiment_cache
    if not cache_hit:
        experiment_cache[cache_key] = compute_result()

    result = deepcopy(experiment_cache[cache_key])
    result["summary"]["cacheHit"] = cache_hit
    return result


def run_experiment(
    steps: int = 1000,
    threshold: float = 35,
    hysteresis: float = 0,
    flow_rate_ml: float = 10.0,
    seed: int | None = DEFAULT_SCENARIO_SEED,
):
    """Run a digital twin experiment and return results as a dictionary."""
    twin = DigitalTwin(threshold=threshold, hysteresis_width=hysteresis, irrigation_flow_ml_per_min=flow_rate_ml)
    
    logs = []
    prev_irrigation = twin.irrigation_active
    event_count = 0
    irrigation_steps = 0
    simulator_soil = SoilDynamics()
    previous_water_usage = 0
    scenario = generate_scenario(steps, seed=seed)
    
    for t in range(steps):
        conditions = scenario[t]
        sensor = generate_sensor_data(
            soil_instance=simulator_soil,
            irrigation_ml=previous_water_usage * 1000,
            conditions=conditions,
        )
        rain = conditions["rain"]
        
        twin.update_sensor_data(sensor["moisture"], sensor["temperature"], sensor["humidity"])
        twin.update_weather(rain)
        twin.evaluate_irrigation()
        
        state = twin.get_state()
        water_usage_ml = flow_rate_ml if state["irrigation_active"] else 0
        previous_water_usage = water_usage_ml / 1000.0
        
        logs.append({
            "step": t,
            "moisture": state["soil_moisture"],
            "temperature": state["temperature"],
            "humidity": state["humidity"],
            "rain_prediction": state["rain_prediction"],
            "rain_amount": round(conditions["rain_amount"], 2),
            "irrigation_active": state["irrigation_active"],
            "water_usage_l": round(water_usage_ml / 1000, 2),
            "water_usage_ml": water_usage_ml
        })
        
        if state["irrigation_active"]:
            irrigation_steps += 1
        
        if not prev_irrigation and state["irrigation_active"]:
            event_count += 1
        prev_irrigation = state["irrigation_active"]
    
    return {
        "entries": logs,
        "summary": {
            "totalEntries": steps,
            "irrigationEvents": event_count,
            "irrigationSteps": irrigation_steps,
            "totalWaterUsage": round(irrigation_steps * flow_rate_ml / 1000, 2),
            "percentTimeIrrigated": round(irrigation_steps / max(steps, 1) * 100, 1),
            "threshold": threshold,
            "hysteresisWidth": hysteresis,
            "scenarioSeed": seed
        }
    }

twin = DigitalTwin()

# IMPORTANT: allow BAS origin (or use * for dev only)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://localhost:8081"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/hello")
def hello():
    return {"message": "Select an experiment to begin"}


@app.get("/api/experiment")
def run_dt_experiment(
    steps: int = Query(1000, ge=10, le=10000),
    threshold: float = Query(35, ge=10, le=80),
    hysteresis: float = Query(0, ge=0, le=20),
    flow_rate_ml: float = Query(10.0, ge=1, le=10000),
    seed: int | None = Query(DEFAULT_SCENARIO_SEED)
):
    """Run a digital twin irrigation experiment with given parameters."""
    cache_key = ("baseline", steps, threshold, hysteresis, flow_rate_ml, seed)
    return get_cached_result(
        cache_key,
        lambda: run_experiment(
            steps=steps,
            threshold=threshold,
            hysteresis=hysteresis,
            flow_rate_ml=flow_rate_ml,
            seed=seed,
        )
    )


@app.get("/api/experiment/sampling")
def run_dt_sampling_experiment(
    steps: int = Query(1000, ge=10, le=10000),
    sample_interval: int = Query(10, ge=1, le=100),
    threshold: float = Query(35, ge=10, le=80),
    hysteresis: float = Query(0, ge=0, le=20),
    flow_rate_ml: float = Query(10.0, ge=1, le=10000),
    seed: int | None = Query(DEFAULT_SCENARIO_SEED)
):
    """Run a sparse sampling irrigation experiment and compare against full sampling."""
    cache_key = ("sampling", steps, sample_interval, threshold, hysteresis, flow_rate_ml, seed)
    return get_cached_result(
        cache_key,
        lambda: _run_sampling_payload(
            steps,
            sample_interval,
            threshold,
            hysteresis,
            flow_rate_ml,
            seed,
        )
    )


@app.get("/api/experiment/anfis")
def run_dt_anfis_experiment(
    steps: int = Query(1000, ge=10, le=10000),
    train_samples: int = Query(500, ge=100, le=2000),
    test_samples: int = Query(200, ge=50, le=1000),
    threshold: float = Query(35, ge=10, le=80),
    hysteresis: float = Query(0, ge=0, le=20),
    flow_rate_ml: float = Query(10.0, ge=1, le=10000),
    seed: int | None = Query(DEFAULT_SCENARIO_SEED),
    parallel_workers: int = Query(DEFAULT_ANFIS_PARALLEL_WORKERS, ge=1, le=32),
    parallel_backend: str = Query(DEFAULT_ANFIS_PARALLEL_BACKEND)
):
    """Run the ANFIS-GA irrigation experiment and compare it with the threshold baseline."""
    if not isinstance(parallel_workers, int):
        parallel_workers = DEFAULT_ANFIS_PARALLEL_WORKERS
    if parallel_backend not in {"process", "thread"}:
        parallel_backend = DEFAULT_ANFIS_PARALLEL_BACKEND

    cache_key = ("anfis", steps, train_samples, test_samples, threshold, hysteresis, flow_rate_ml, seed, parallel_workers, parallel_backend)
    return get_cached_result(
        cache_key,
        lambda: _run_anfis_payload(
            steps,
            train_samples,
            test_samples,
            threshold,
            hysteresis,
            flow_rate_ml,
            seed,
            parallel_workers,
            parallel_backend,
        )
    )


def _run_sampling_payload(steps, sample_interval, threshold, hysteresis, flow_rate_ml, seed):
    logs, metrics = run_sampling_experiment(
        steps=steps,
        sample_interval=sample_interval,
        twin_params={
            "threshold": threshold,
            "hysteresis_width": hysteresis,
            "irrigation_flow_ml_per_min": flow_rate_ml,
        },
        seed=seed,
    )
    return {"entries": logs, "summary": metrics}


def _run_anfis_payload(steps, train_samples, test_samples, threshold, hysteresis, flow_rate_ml, seed, parallel_workers, parallel_backend):
    logs, metrics = run_anfis_experiment(
        steps=steps,
        train_samples=train_samples,
        test_samples=test_samples,
        twin_params={
            "threshold": threshold,
            "hysteresis_width": hysteresis,
            "irrigation_flow_ml_per_min": flow_rate_ml,
        },
        seed=seed,
        parallel_workers=parallel_workers,
        parallel_backend=parallel_backend,
    )
    return {"entries": logs, "summary": metrics}


# Background synchronization loop
# Simulates DT synchronization with physical sensors

def update_loop():
    while True:
        previous_water_usage = twin.irrigation_flow_ml_per_min if twin.irrigation_active else 0
        sensor_data = generate_sensor_data(irrigation_ml=previous_water_usage)
        rain_prediction = sensor_data["rain"]

        twin.update_sensor_data(
            sensor_data["moisture"],
            sensor_data["temperature"],
            sensor_data["humidity"],
        )
        twin.update_weather(rain_prediction)

        # control decision
        twin.evaluate_irrigation()

        print("Twin Updated:", twin.get_state())

        time.sleep(5)


update_thread = None


@app.on_event("startup")
def start_update_loop():
    global update_thread
    if update_thread is None or not update_thread.is_alive():
        update_thread = threading.Thread(target=update_loop, daemon=True)
        update_thread.start()


@app.get("/")
def root():
    return {
        "message": "Digital Twin Irrigation System Running"
    }


@app.get("/dt-state")
def get_twin_state():
    return twin.get_state()


@app.get("/irrigation-status")
def irrigation_status():
    return {
        "irrigation_active": twin.irrigation_active
    }
