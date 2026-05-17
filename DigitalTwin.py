from datetime import datetime


class DigitalTwin:
    def __init__(self, threshold=35, hysteresis_width=0, irrigation_flow_ml_per_min=1.0):
        """Create a DigitalTwin.

        Args:
            threshold (float): soil moisture threshold to trigger irrigation.
            hysteresis_width (float): width of hysteresis band around threshold (same units as moisture).
            irrigation_flow_ml_per_min (float): assumed irrigation flow rate for usage estimates.
        """
        self.soil_moisture = 50
        self.temperature = 25
        self.humidity = 40
        self.rain_prediction = False
        self.irrigation_active = False
        self.last_update = datetime.now()

        # controller parameters
        self.threshold = threshold
        self.hysteresis_width = hysteresis_width
        self.irrigation_flow_ml_per_min = irrigation_flow_ml_per_min

    def update_sensor_data(self, moisture, temperature, humidity):
        self.soil_moisture = moisture
        self.temperature = temperature
        self.humidity = humidity
        self.last_update = datetime.now()

    def update_weather(self, rain_prediction):
        self.rain_prediction = rain_prediction

    def evaluate_irrigation(self):
        """Evaluate irrigation decision using threshold + optional hysteresis.

        Hysteresis logic: compute lower and upper bounds around `threshold` using
        `hysteresis_width`. If moisture < lower and no rain -> ON. If moisture > upper -> OFF.
        If hysteresis_width == 0 fall back to simple threshold: moisture < threshold -> ON.
        """
        if self.hysteresis_width and self.hysteresis_width > 0:
            half = self.hysteresis_width / 2.0
            lower = self.threshold - half
            upper = self.threshold + half
            if self.soil_moisture < lower and not self.rain_prediction:
                self.irrigation_active = True
            elif self.soil_moisture > upper or self.rain_prediction:
                self.irrigation_active = False
            # else keep previous state
        else:
            if self.soil_moisture < self.threshold and not self.rain_prediction:
                self.irrigation_active = True
            else:
                self.irrigation_active = False
        
        if self.irrigation_active:
            # irrigation increases soil moisture
            self.soil_moisture += self.irrigation_flow_ml_per_min * 1.2
        self.soil_moisture = max(0, min(100, self.soil_moisture))
        
    def get_state(self):
        return {
            "soil_moisture": self.soil_moisture,
            "temperature": self.temperature,
            "humidity": self.humidity,
            "rain_prediction": self.rain_prediction,
            "irrigation_active": self.irrigation_active,
            "threshold": self.threshold,
            "hysteresis_width": self.hysteresis_width,
            "irrigation_flow_ml_per_min": self.irrigation_flow_ml_per_min,
            "last_update": str(self.last_update)
        }
    
    def apply_environment(self, evap_loss=0.0, rain=False):
        # natural loss
        self.soil_moisture -= evap_loss

        # rain gain
        if rain:
            self.soil_moisture += 5

        # irrigation gain
        if self.irrigation_active:
            self.soil_moisture += self.irrigation_flow_ml_per_min * 0.5

        # clamp
        self.soil_moisture = max(0, min(100, self.soil_moisture))