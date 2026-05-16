import random


def generate_sensor_data():
    moisture = random.randint(20, 80)
    temperature = random.randint(18, 36)
    humidity = random.randint(30, 90)

    return {
        "moisture": moisture,
        "temperature": temperature,
        "humidity": humidity
    }