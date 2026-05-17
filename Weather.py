import random


def get_weather_prediction(rng=None):
    if rng is None:
        rng = random

    rain_probability = rng.randint(0, 100)
    return rain_probability > 70