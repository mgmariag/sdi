import random


class SoilDynamics:
    def __init__(self, initial_moisture=45, irrigation_gain_per_ml=3.0):
        self.moisture = initial_moisture
        self.irrigation_gain_per_ml = irrigation_gain_per_ml

    def step(self, rng=None, rain_probability=0.12, irrigation_ml=0.0, conditions=None):
        if rng is None:
            rng = random

        if conditions is None:
            conditions = generate_environment(rng, rain_probability)

        temperature = conditions["temperature"]
        humidity = conditions["humidity"]
        rain = conditions["rain"]
        rain_amount = conditions["rain_amount"]

        # Evapotranspiration is intentionally stronger than the first prototype
        # so soil can realistically cross common irrigation thresholds.
        evap = 0.35 + 1.2 * (temperature / 36) * (100 - humidity) / 100
        self.moisture -= evap

        if rain:
            self.moisture += rain_amount

        if irrigation_ml > 0:
            self.moisture += irrigation_ml * self.irrigation_gain_per_ml

        self.moisture = max(0, min(100, self.moisture))

        return {
            "moisture": round(self.moisture, 2),
            "temperature": round(temperature, 2),
            "humidity": round(humidity, 2),
            "rain": rain,
            "rain_amount": round(rain_amount, 2)
        }


def generate_environment(rng=None, rain_probability=0.12):
    if rng is None:
        rng = random

    rain = rng.random() < rain_probability
    return {
        "temperature": rng.uniform(18, 36),
        "humidity": rng.uniform(30, 90),
        "rain": rain,
        "rain_amount": rng.uniform(1.5, 6.0) if rain else 0.0,
    }


def generate_scenario(steps, seed=None, rain_probability=0.12):
    rng = random.Random(seed) if seed is not None else random
    return [generate_environment(rng, rain_probability) for _ in range(steps)]


soil = SoilDynamics()


def generate_sensor_data(rng=None, soil_instance=None, irrigation_ml=0.0, conditions=None):
    if soil_instance is None:
        soil_instance = soil
    return soil_instance.step(rng, irrigation_ml=irrigation_ml, conditions=conditions)
