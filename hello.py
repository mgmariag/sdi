from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from DigitalTwin import DigitalTwin
from Simulator import generate_sensor_data
from Weather import get_weather_prediction
import threading
import time

app = FastAPI()


def run_experiment(steps: int = 1000, threshold: float = 35, hysteresis: float = 0, flow_rate: float = 1.0):
    """Run a digital twin experiment and return results as a dictionary."""
    twin = DigitalTwin(threshold=threshold, hysteresis_width=hysteresis, irrigation_flow_l_per_min=flow_rate)
    
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
        water_usage = flow_rate if state["irrigation_active"] else 0
        
        logs.append({
            "step": t,
            "moisture": state["soil_moisture"],
            "temperature": state["temperature"],
            "humidity": state["humidity"],
            "rain_prediction": state["rain_prediction"],
            "irrigation_active": state["irrigation_active"],
            "water_usage_l": water_usage
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
            "totalWaterUsage": round(irrigation_steps * flow_rate, 2),
            "percentTimeIrrigated": round(irrigation_steps / max(steps, 1) * 100, 1),
            "threshold": threshold,
            "hysteresisWidth": hysteresis,
            "flowRate": flow_rate
        }
    }

twin = DigitalTwin()

# IMPORTANT: allow BAS origin (or use * for dev only)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/hello")
def hello():
    return {"message": "Hello from Python"}


@app.get("/api/experiment")
def run_dt_experiment(
    steps: int = Query(1000, ge=10, le=10000),
    threshold: float = Query(35, ge=10, le=80),
    hysteresis: float = Query(0, ge=0, le=20),
    flow_rate: float = Query(1.0, ge=0.1, le=10)
):
    """Run a digital twin irrigation experiment with given parameters."""
    result = run_experiment(steps=steps, threshold=threshold, hysteresis=hysteresis, flow_rate=flow_rate)
    return result


# Background synchronization loop
# Simulates DT synchronization with physical sensors

def update_loop():
    while True:
        sensor_data = generate_sensor_data()

        twin.update_sensor_data(
            sensor_data["moisture"],
            sensor_data["temperature"],
            sensor_data["humidity"]
        )

        rain_prediction = get_weather_prediction()

        twin.update_weather(rain_prediction)

        twin.evaluate_irrigation()

        print("Twin Updated:", twin.get_state())

        time.sleep(5)


thread = threading.Thread(target=update_loop)
thread.daemon = True
thread.start()


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