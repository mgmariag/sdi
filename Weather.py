import random


def get_weather_prediction():
    rain_probability = random.randint(0, 100)

    if rain_probability > 70:
        return True

    return False