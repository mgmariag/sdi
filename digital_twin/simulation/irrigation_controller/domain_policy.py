from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from digital_twin.simulation.irrigation_controller.environment import (
    rain_exposure_factor,
)
from digital_twin.simulation.irrigation_controller.sizing import (
    _size_flow_rate_multiplier,
)
from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.soil_model import clamp, local_observed_at, number


class IrrigationDomainPolicy:
    """Shared irrigation domain rules that are not owned by one controller."""

    def decision_slot(self, day: date, observed_at: datetime, day_profile: dict[str, Any]) -> str | None:
        hour = observed_at.hour
        max_temp = number(day_profile.get("max_temperature_c"), 20.0)

        if self.dormant_period(day):
            return "winter_check" if hour == 10 else None
        if hour == 6:
            return "morning"
        if max_temp >= 32.0 and hour == 18:
            return "evening"
        return None

    def dormant_period(self, day: date) -> bool:
        return day.month in {12, 1, 2, 3}

    def target_moisture(self, pot: dict[str, Any], day_profile: dict[str, Any], slot: str) -> float:
        if slot == "winter_check":
            return float(pot["winter_moisture_target_pct"])

        target = float(pot["moisture_target_pct"])
        max_temp = number(day_profile.get("max_temperature_c"), 20.0)
        boost = 0.0
        if 28.0 <= max_temp < 32.0 and (
            self.high_need_pot(pot) or pot.get("sun_exposure") in {"full", "reflected_heat"}
        ):
            boost = 2.0
        elif 32.0 <= max_temp < 35.0:
            boost = 2.0
        elif max_temp >= 35.0 and self.heat_priority_pot(pot):
            boost = 3.0
        if slot == "evening":
            boost = min(boost, 1.5)
        return min(float(pot["moisture_max_pct"]) - 2.0, target + boost)

    def trigger_threshold(self, pot: dict[str, Any], day_profile: dict[str, Any], slot: str) -> float:
        min_moisture = float(pot["moisture_min_pct"])
        target = float(pot["moisture_target_pct"])
        max_temp = number(day_profile.get("max_temperature_c"), 20.0)
        avg_temp = number(day_profile.get("avg_temperature_c"), max_temp)

        if slot == "winter_check":
            return max(7.0, min(float(pot["winter_moisture_target_pct"]) - 5.0, 12.0))
        if max_temp < 15.0:
            threshold = max(7.0, min_moisture - 5.0)
        elif avg_temp < 18.0:
            threshold = min_moisture - 1.5
        elif avg_temp < 22.0:
            threshold = min_moisture
        elif max_temp < 28.0:
            threshold = min_moisture + (1.0 if self.high_need_pot(pot) else 0.0)
        elif max_temp < 32.0:
            threshold = min_moisture + 3.0
        elif max_temp < 35.0:
            threshold = min_moisture + 5.0
        else:
            threshold = min_moisture + (7.0 if self.heat_priority_pot(pot) else 5.0)

        if day_profile.get("dry_windy_day") and pot["size_class"] == "small":
            threshold += 2.0
        if (
            int(day_profile.get("dry_streak_days") or 0) >= 2
            and number(day_profile.get("precipitation_mm"), 0.0) < 0.5
            and max_temp >= 24.0
        ):
            if pot["size_class"] == "small":
                threshold += 4.0
            elif self.high_need_pot(pot) and pot.get("sun_exposure") in {"full", "reflected_heat"}:
                threshold += 2.0
        if slot == "evening":
            threshold = max(threshold, min_moisture + (4.0 if max_temp >= 35.0 else 2.0))
        return clamp(threshold, 5.0, target - 1.0)

    def temperature_dose_factor(self, day_profile: dict[str, Any]) -> float:
        min_temp = number(day_profile.get("min_temperature_c"), 20.0)
        max_temp = number(day_profile.get("max_temperature_c"), 20.0)
        avg_temp = number(day_profile.get("avg_temperature_c"), max_temp)
        if max_temp <= 15.0 and 5.0 <= min_temp <= 12.0:
            return 0.5
        if avg_temp < 15.0:
            return 0.5
        return 1.0

    def allowed_dose_factor(self, value: float) -> float:
        number_value = clamp(number(value, 1.0), 0.5, 1.0)
        if number_value <= 0.625:
            return 0.5
        if number_value <= 0.875:
            return 0.75
        return 1.0

    def rain_policy(self, pot: dict[str, Any], day: date, day_profile: dict[str, Any]) -> dict[str, Any]:
        exposure = rain_exposure_factor(pot, day)
        rain_mm = number(day_profile.get("precipitation_mm"), 0.0)
        probability = number(day_profile.get("max_precipitation_probability_pct"), 0.0)
        if probability <= 0.0 and rain_mm >= 1.0:
            probability = 100.0
        effective_rain = rain_mm * exposure
        reduction = 0.0
        skip = False

        if probability > 80.0 and effective_rain > 10.0:
            reduction = 0.5
            skip = True
        elif probability > 75.0 and effective_rain > 6.0:
            reduction = 0.5
            skip = True
        elif probability >= 60.0 and effective_rain >= 3.0:
            reduction = 0.5
        elif probability >= 40.0 and effective_rain >= 1.0:
            reduction = 0.25

        if exposure <= 0.0:
            skip = False
            if probability >= 40.0 and rain_mm >= 1.0:
                reduction = 0.25
            else:
                reduction = min(reduction, 0.25)

        return {
            "rain_exposure_factor": exposure,
            "effective_rain_mm": effective_rain,
            "rain_probability_pct": probability,
            "dose_factor": self.allowed_dose_factor(1.0 - reduction),
            "skip": skip,
        }

    def is_covered_rain_day(self, rain_policy: dict[str, Any], day_profile: dict[str, Any]) -> bool:
        return (
            number(day_profile.get("precipitation_mm"), 0.0) >= 1.0
            and number(rain_policy.get("rain_probability_pct"), 0.0) >= 40.0
            and number(rain_policy.get("rain_exposure_factor"), 0.0) <= 0.0
        )

    def cadence_days(self, day_profile: dict[str, Any]) -> int:
        avg_temp = number(day_profile.get("avg_temperature_c"), 20.0)
        max_temp = number(day_profile.get("max_temperature_c"), avg_temp)
        rain_mm = number(day_profile.get("precipitation_mm"), 0.0)
        rain_probability = number(day_profile.get("max_precipitation_probability_pct"), 0.0)

        if day_profile.get("dormant_period") or max_temp < 12.0:
            return 7
        if max_temp < 15.0:
            return 4
        if avg_temp < 18.0:
            return 3 if rain_mm < 3.0 and rain_probability < 60.0 else 5
        if max_temp >= 22.0:
            return 1
        if avg_temp < 25.0:
            return 2
        return 1

    def cadence_allows(self, pot: dict[str, Any], day: date, cadence_days: int) -> bool:
        if cadence_days <= 1:
            return True
        return (day.toordinal() + int(pot["id"])) % cadence_days == 0

    def critically_low(self, state: PotState, pot: dict[str, Any], slot: str) -> bool:
        if slot == "winter_check":
            return state.moisture <= max(7.0, float(pot["winter_moisture_target_pct"]) - 6.0)
        return state.moisture <= max(8.0, float(pot["moisture_min_pct"]) - 8.0)

    def winter_irrigation_allowed(self, state: PotState, day_profile: dict[str, Any]) -> bool:
        return (
            state.moisture < 10.0
            and day_profile.get("min_temperature_next_14_days_c", day_profile["min_temperature_c"]) > 5.0
            and day_profile.get("precipitation_next_14_days_mm", day_profile["precipitation_mm"]) < 1.0
            and day_profile.get("max_precipitation_probability_next_14_days_pct", 0.0) < 40.0
        )

    def second_watering_allowed(self, state: PotState, pot: dict[str, Any], day_profile: dict[str, Any]) -> bool:
        max_temp = number(day_profile.get("max_temperature_c"), 20.0)
        if max_temp < 32.0:
            return False
        eligible = self.heat_priority_pot(pot) or pot["allows_second_watering"]
        if max_temp >= 35.0:
            return eligible and state.moisture < pot["moisture_target_pct"]
        return eligible and state.moisture < max(pot["moisture_min_pct"] + 2.0, pot["moisture_target_pct"] - 6.0)

    def high_need_pot(self, pot: dict[str, Any]) -> bool:
        return (
            pot.get("water_need_level") == "high"
            or pot.get("heat_sensitive")
            or pot.get("plant_type_code") in {"vegetables", "herbs", "tomatoes", "cucumbers", "flowering"}
        )

    def heat_priority_pot(self, pot: dict[str, Any]) -> bool:
        return (
            self.high_need_pot(pot)
            or pot.get("size_class") == "small"
            or pot.get("small_subtype") == "hanging"
            or pot.get("balcony_zone") == "hanging_row"
        )

    def full_dose_start_moisture(self, pot: dict[str, Any], decision: dict[str, Any], target: float) -> float:
        if decision.get("full_dose_start_moisture_pct") is not None:
            return clamp(number(decision.get("full_dose_start_moisture_pct"), target), 0.0, target)
        if decision.get("current_moisture_pct") is not None:
            return clamp(number(decision.get("current_moisture_pct"), target), 0.0, target)
        if decision.get("slot") == "winter_check":
            return max(7.0, min(target - 5.0, 12.0))
        return number(pot.get("moisture_min_pct"), target)

    def irrigation_request(self, pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        target = number(decision.get("target_moisture_pct"), pot["moisture_target_pct"])
        start_moisture = self.full_dose_start_moisture(pot, decision, target)
        need_pct = max(0.0, target - start_moisture)
        dose_factor = self.allowed_dose_factor(number(decision.get("dose_factor"), 1.0))
        volume_l = pot["volume_l"]
        retention = max(pot["retention_factor"], 0.1)
        flow_rate = max(pot["drip_flow_ml_min"] * _size_flow_rate_multiplier(pot), 1.0)
        max_minutes = {"huge": 90, "large": 60, "medium": 35, "small": 20}[pot["size_class"]]
        full_dose_volume_ml = min(max(0.0, need_pct * volume_l * 10.0 / retention), flow_rate * max_minutes)
        planned_volume_ml = full_dose_volume_ml * dose_factor
        duration_min = planned_volume_ml / flow_rate
        cycle_count = 2 if pot["cycle_soak_enabled"] and duration_min >= 10 else 1
        soak_pause_min = 10 if cycle_count == 2 else 0

        scheduled_start = local_observed_at(weather)
        scheduled_end = scheduled_start + timedelta(minutes=duration_min + soak_pause_min)
        sensor_id = int(decision.get("sensor_id", pot["id"]))
        return {
            "pot_id": pot["id"],
            "sensor_id": sensor_id,
            "request_sensor_id": sensor_id,
            "pot_code": pot["pot_code"],
            "date": scheduled_start.date().isoformat(),
            "slot": decision["slot"],
            "scheduled_start_at": scheduled_start.isoformat(),
            "scheduled_end_at": scheduled_end.isoformat(),
            "flow_rate_ml_min": round(flow_rate, 2),
            "planned_volume_ml": round(planned_volume_ml, 2),
            "requested_volume_ml": round(planned_volume_ml, 2),
            "duration_min": round(duration_min, 3),
            "cycle_count": cycle_count,
            "soak_pause_min": soak_pause_min,
            "full_dose_volume_ml": round(full_dose_volume_ml, 2),
            "dose_factor": round(dose_factor, 3),
            "dose_reduction_pct": round((1.0 - dose_factor) * 100.0, 1),
        }
