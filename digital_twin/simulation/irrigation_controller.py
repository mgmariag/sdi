from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from digital_twin.simulation.dto import PotState
from digital_twin.simulation.soil_model import (
    clamp as _clamp,
    local_observed_at as _local_observed_at,
    number as _number,
    season as _season,
)

_FUZZY_OUTPUT_SETS = {
    "none": (0.0, 0.0, 0.4),
    "very_low": (0.0, 0.8, 1.6),
    "low": (1.0, 2.0, 3.2),
    "medium": (2.6, 4.0, 5.4),
    "high": (4.6, 6.0, 7.2),
    "very_high": (6.4, 8.0, 8.0),
}
_DEFUZZ_X_VALUES = tuple(index / 20.0 for index in range(161))
_DEFUZZ_OUTPUT_MEMBERSHIPS: dict[str, tuple[float, ...]] | None = None


class IrrigationDomainPolicy:
    """Shared irrigation domain rules that are not owned by one controller."""

    def decision_slot(self, day: date, observed_at: datetime, day_profile: dict[str, Any]) -> str | None:
        hour = observed_at.hour
        max_temp = _number(day_profile.get("max_temperature_c"), 20.0)

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
        max_temp = _number(day_profile.get("max_temperature_c"), 20.0)
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
        max_temp = _number(day_profile.get("max_temperature_c"), 20.0)
        avg_temp = _number(day_profile.get("avg_temperature_c"), max_temp)

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
            and _number(day_profile.get("precipitation_mm"), 0.0) < 0.5
            and max_temp >= 24.0
        ):
            if pot["size_class"] == "small":
                threshold += 4.0
            elif self.high_need_pot(pot) and pot.get("sun_exposure") in {"full", "reflected_heat"}:
                threshold += 2.0
        if slot == "evening":
            threshold = max(threshold, min_moisture + (4.0 if max_temp >= 35.0 else 2.0))
        return _clamp(threshold, 5.0, target - 1.0)

    def temperature_dose_factor(self, day_profile: dict[str, Any]) -> float:
        min_temp = _number(day_profile.get("min_temperature_c"), 20.0)
        max_temp = _number(day_profile.get("max_temperature_c"), 20.0)
        avg_temp = _number(day_profile.get("avg_temperature_c"), max_temp)
        if max_temp <= 15.0 and 5.0 <= min_temp <= 12.0:
            return 0.5
        if avg_temp < 15.0:
            return 0.5
        return 1.0

    def allowed_dose_factor(self, value: float) -> float:
        number_value = _clamp(_number(value, 1.0), 0.5, 1.0)
        if number_value <= 0.625:
            return 0.5
        if number_value <= 0.875:
            return 0.75
        return 1.0

    def rain_policy(self, pot: dict[str, Any], day: date, day_profile: dict[str, Any]) -> dict[str, Any]:
        exposure = _rain_exposure_factor(pot, day)
        rain_mm = _number(day_profile.get("precipitation_mm"), 0.0)
        probability = _number(day_profile.get("max_precipitation_probability_pct"), 0.0)
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
            _number(day_profile.get("precipitation_mm"), 0.0) >= 1.0
            and _number(rain_policy.get("rain_probability_pct"), 0.0) >= 40.0
            and _number(rain_policy.get("rain_exposure_factor"), 0.0) <= 0.0
        )

    def cadence_days(self, day_profile: dict[str, Any]) -> int:
        avg_temp = _number(day_profile.get("avg_temperature_c"), 20.0)
        max_temp = _number(day_profile.get("max_temperature_c"), avg_temp)
        rain_mm = _number(day_profile.get("precipitation_mm"), 0.0)
        rain_probability = _number(day_profile.get("max_precipitation_probability_pct"), 0.0)

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
        max_temp = _number(day_profile.get("max_temperature_c"), 20.0)
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
            return _clamp(_number(decision.get("full_dose_start_moisture_pct"), target), 0.0, target)
        if decision.get("current_moisture_pct") is not None:
            return _clamp(_number(decision.get("current_moisture_pct"), target), 0.0, target)
        if decision.get("slot") == "winter_check":
            return max(7.0, min(target - 5.0, 12.0))
        return _number(pot.get("moisture_min_pct"), target)

    def irrigation_request(self, pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        target = _number(decision.get("target_moisture_pct"), pot["moisture_target_pct"])
        start_moisture = self.full_dose_start_moisture(pot, decision, target)
        need_pct = max(0.0, target - start_moisture)
        dose_factor = self.allowed_dose_factor(_number(decision.get("dose_factor"), 1.0))
        volume_l = pot["volume_l"]
        retention = max(pot["retention_factor"], 0.1)
        flow_rate = max(pot["drip_flow_ml_min"] * _size_flow_rate_multiplier(pot), 1.0)
        max_minutes = {"huge": 90, "large": 60, "medium": 35, "small": 20}[pot["size_class"]]
        full_dose_volume_ml = min(max(0.0, need_pct * volume_l * 10.0 / retention), flow_rate * max_minutes)
        planned_volume_ml = full_dose_volume_ml * dose_factor
        duration_min = planned_volume_ml / flow_rate
        cycle_count = 2 if pot["cycle_soak_enabled"] and duration_min >= 10 else 1
        soak_pause_min = 10 if cycle_count == 2 else 0

        scheduled_start = _local_observed_at(weather)
        scheduled_end = scheduled_start + timedelta(minutes=duration_min + soak_pause_min)
        return {
            "pot_id": pot["id"],
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


class FuzzyIrrigationPolicy:
    """Fuzzy DT controller rules, separate from the default threshold policy."""

    comfort_target_offset_pct = 6.0
    comfort_minimum_margin_pct = 3.0
    comfort_rain_grace_pct = 3.0

    def decision_slot(self, day: date, observed_at: datetime, day_profile: dict[str, Any]) -> str | None:
        hour = observed_at.hour
        max_temp = _number(day_profile.get("max_temperature_c"), 20.0)

        if day.month in {12, 1, 2, 3}:
            return "winter_check" if hour == 10 else None
        if hour == 6:
            return "morning"
        if (max_temp >= 35.0 or day_profile.get("heatwave_day")) and hour == 18:
            return "evening"
        return None

    def comfort_floor(self, pot: dict[str, Any], day_profile: dict[str, Any], slot: str = "morning") -> float:
        target = float(pot["moisture_target_pct"])
        min_moisture = float(pot["moisture_min_pct"])
        floor = max(
            min_moisture + self.comfort_minimum_margin_pct,
            target - self.comfort_target_offset_pct,
        )
        if self.high_need_pot(pot):
            floor += 1.0
        if day_profile.get("heatwave_day"):
            floor += 1.5 if self.heat_priority_pot(pot) else 0.75
        if day_profile.get("dry_windy_day"):
            floor += 1.5 if pot["size_class"] == "small" else 0.75
        if slot == "winter_check":
            winter_target = float(pot["winter_moisture_target_pct"])
            floor = max(min_moisture, winter_target - 3.0)
        return _clamp(floor, 5.0, target - 1.0)

    def trigger_threshold(self, pot: dict[str, Any], day_profile: dict[str, Any], slot: str = "morning") -> float:
        threshold = self.comfort_floor(pot, day_profile, slot)
        if slot == "evening":
            threshold = max(threshold, float(pot["moisture_min_pct"]) + 2.0)
        return _clamp(threshold, 5.0, float(pot["moisture_target_pct"]) - 2.0)

    def heat_priority_pot(self, pot: dict[str, Any]) -> bool:
        return (
            self.high_need_pot(pot)
            or pot.get("size_class") == "small"
            or pot.get("small_subtype") == "hanging"
            or pot.get("balcony_zone") == "hanging_row"
        )

    def high_need_pot(self, pot: dict[str, Any]) -> bool:
        return (
            pot.get("water_need_level") == "high"
            or pot.get("heat_sensitive")
            or pot.get("plant_type_code") in {"vegetables", "herbs", "tomatoes", "cucumbers", "flowering"}
        )

    def skip_reason(
        self,
        soil_moisture_pct: float,
        temperature_c: float,
        rain_mm: float,
        comfort_floor_pct: float | None = None,
        minimum_moisture_pct: float | None = None,
    ) -> dict[str, str] | None:
        if temperature_c <= 3.0:
            return {
                "code": "cold_skip",
                "detail": "Skipped because temperature is too low for irrigation.",
            }
        comfort_floor_pct = 42.0 if comfort_floor_pct is None else float(comfort_floor_pct)
        minimum_moisture_pct = 28.0 if minimum_moisture_pct is None else float(minimum_moisture_pct)
        if soil_moisture_pct <= minimum_moisture_pct:
            return None
        rain_grace_floor = max(minimum_moisture_pct + 2.0, comfort_floor_pct - self.comfort_rain_grace_pct)
        if rain_mm >= 8.0 and soil_moisture_pct >= rain_grace_floor - 1.0:
            return {
                "code": "rain_sufficient",
                "detail": f"Skipped because rain {rain_mm:.1f} mm can cover the current fuzzy comfort gap.",
            }
        if soil_moisture_pct < comfort_floor_pct and rain_mm < 4.0:
            return None
        if soil_moisture_pct >= 62.0:
            return {
                "code": "moisture_sufficient",
                "detail": f"Skipped because soil moisture {soil_moisture_pct:.1f}% is already sufficient.",
            }
        if rain_mm >= 4.0 and soil_moisture_pct >= rain_grace_floor:
            return {
                "code": "rain_sufficient",
                "detail": f"Skipped because rain {rain_mm:.1f} mm is sufficient near the fuzzy comfort floor.",
            }
        if rain_mm >= 1.0 and soil_moisture_pct >= comfort_floor_pct:
            return {
                "code": "rain_and_moisture_sufficient",
                "detail": f"Skipped because rain {rain_mm:.1f} mm and moisture {soil_moisture_pct:.1f}% are sufficient.",
            }
        if temperature_c <= 10.0 and soil_moisture_pct >= comfort_floor_pct:
            return {
                "code": "cool_day_moisture_sufficient",
                "detail": (
                    f"Skipped because temperature {temperature_c:.1f}C is cool and "
                    f"soil moisture {soil_moisture_pct:.1f}% is sufficient."
                ),
            }
        return None

    def prescription_mm(self, soil_moisture_pct: float, temperature_c: float, rain_mm: float) -> float:
        return _fuzzy_irrigation_prescription_mm(
            soil_moisture_pct=soil_moisture_pct,
            temperature_c=temperature_c,
            rain_mm=rain_mm,
        )

    def prescribed_volume_ml(self, state: PotState, pot: dict[str, Any], prescription_mm: float) -> float:
        return self.comfort_preserving_volume_ml(state, pot, prescription_mm)

    def comfort_preserving_volume_ml(
        self,
        state: PotState,
        pot: dict[str, Any],
        prescription_mm: float,
        day_profile: dict[str, Any] | None = None,
        slot: str = "morning",
    ) -> float:
        base_volume_ml = max(0.0, prescription_mm) * _pot_surface_area_m2(pot) * 1000.0
        retention = max(float(pot["retention_factor"]), 0.1)
        day_profile = day_profile or {}
        comfort_floor = self.comfort_floor(pot, day_profile, slot)
        min_moisture = float(pot["moisture_min_pct"])
        flow_rate = max(pot["drip_flow_ml_min"] * _size_flow_rate_multiplier(pot), 1.0)

        if state.moisture >= comfort_floor:
            return min(base_volume_ml * 0.25, flow_rate * 2.0)

        deficit_pct = max(0.0, comfort_floor - state.moisture)
        if state.moisture <= min_moisture:
            deficit_pct += max(0.0, min_moisture - state.moisture) * 0.5
        deficit_volume_ml = deficit_pct * float(pot["volume_l"]) * 10.0 / retention
        if deficit_volume_ml <= 0.0:
            return base_volume_ml

        if state.moisture <= min_moisture:
            deficit_factor = 1.0
        elif state.moisture < comfort_floor:
            deficit_factor = 0.9
        else:
            deficit_factor = 0.15
        if day_profile.get("heatwave_day") or day_profile.get("dry_windy_day"):
            deficit_factor = min(1.0, deficit_factor + 0.1)
        if _number(day_profile.get("precipitation_mm"), 0.0) >= 4.0:
            deficit_factor *= 0.85

        comfort_volume_ml = deficit_volume_ml * deficit_factor
        max_minutes = {"huge": 60, "large": 42, "medium": 27, "small": 17}[pot["size_class"]]
        return min(max(base_volume_ml, comfort_volume_ml), flow_rate * max_minutes)

    def irrigation_request(self, pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        planned_volume_ml = max(0.0, _number(decision.get("planned_volume_ml"), 0.0))
        flow_rate = max(pot["drip_flow_ml_min"] * _size_flow_rate_multiplier(pot), 1.0)
        duration_min = planned_volume_ml / flow_rate if flow_rate > 0 else 0.0
        cycle_count = 2 if pot["cycle_soak_enabled"] and duration_min >= 10 else 1
        soak_pause_min = 10 if cycle_count == 2 else 0

        scheduled_start = _local_observed_at(weather)
        scheduled_end = scheduled_start + timedelta(minutes=duration_min + soak_pause_min)
        return {
            "pot_id": pot["id"],
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
            "prescription_mm": decision.get("prescription_mm", 0.0),
        }

    def make_decision(
        self,
        state: PotState,
        pot: dict[str, Any],
        weather: dict[str, Any],
        day_profile: dict[str, Any],
        slot: str = "morning",
    ) -> dict[str, Any]:
        observed_local = _local_observed_at(weather)
        temperature_c = _number(weather.get("temperature_c"), day_profile["avg_temperature_c"])
        rain_mm = _number(day_profile.get("precipitation_mm"), 0.0)
        comfort_floor = self.comfort_floor(pot, day_profile, slot)
        min_moisture = float(pot["moisture_min_pct"])
        skip_reason = self.skip_reason(
            state.moisture,
            temperature_c,
            rain_mm,
            comfort_floor_pct=comfort_floor,
            minimum_moisture_pct=min_moisture,
        )
        trigger_threshold = self.trigger_threshold(pot, day_profile, slot)
        prescription_mm = 0.0 if skip_reason else self.prescription_mm(
            soil_moisture_pct=state.moisture,
            temperature_c=temperature_c,
            rain_mm=rain_mm,
        )
        planned_volume_ml = self.comfort_preserving_volume_ml(state, pot, prescription_mm, day_profile, slot)
        hard_safety_deficit = state.moisture <= min_moisture
        comfort_deficit = state.moisture < comfort_floor
        should_irrigate = (
            skip_reason is None
            and state.moisture < trigger_threshold
            and planned_volume_ml >= 10.0
            and (prescription_mm >= 0.25 or comfort_deficit or hard_safety_deficit)
        )

        if skip_reason:
            reason_code = f"fuzzy_{skip_reason['code']}"
            reason_detail = skip_reason["detail"]
        elif hard_safety_deficit and should_irrigate:
            reason_code = "fuzzy_moisture_safety_floor"
            reason_detail = (
                f"Soil moisture {state.moisture:.1f}% is at or below the safety floor "
                f"{min_moisture:.1f}%; fuzzy waters toward target {pot['moisture_target_pct']:.1f}%."
            )
        elif comfort_deficit and should_irrigate:
            reason_code = "fuzzy_comfort_preserving_prescription"
            reason_detail = (
                f"Soil moisture {state.moisture:.1f}% is below fuzzy comfort floor "
                f"{comfort_floor:.1f}%; fuzzy prescription {prescription_mm:.2f} mm is scaled toward comfort."
            )
        elif state.moisture >= trigger_threshold and prescription_mm > 0.0:
            reason_code = "fuzzy_soft_zone_need"
            reason_detail = (
                f"Soil moisture {state.moisture:.1f}% is above the fuzzy trigger threshold "
                f"{trigger_threshold:.1f}%, but this pot keeps a {prescription_mm:.2f} mm "
                "soft prescription if another pot opens the valve zone."
            )
        elif not should_irrigate:
            reason_code = "fuzzy_no_irrigation"
            reason_detail = f"FIS prescription {prescription_mm:.2f} mm does not require irrigation."
        else:
            reason_code = "fuzzy_prescription"
            reason_detail = (
                f"FIS prescription {prescription_mm:.2f} mm from moisture {state.moisture:.1f}%, "
                f"temperature {temperature_c:.1f}C, rain {rain_mm:.2f} mm."
            )

        return {
            "pot_id": pot["id"],
            "pot_code": pot["pot_code"],
            "decided_at": observed_local.isoformat(),
            "date": observed_local.date().isoformat(),
            "slot": slot,
            "should_irrigate": should_irrigate,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
            "current_moisture_pct": round(state.moisture, 2),
            "target_moisture_pct": round(pot["moisture_target_pct"], 2),
            "weather_hourly_id": weather["id"],
            "prescription_mm": round(prescription_mm, 2),
            "temperature_c": round(temperature_c, 2),
            "rain_forecast_mm": round(rain_mm, 2),
            "planned_volume_ml": round(planned_volume_ml, 2),
            "fuzzy_trigger_threshold_pct": round(trigger_threshold, 2),
            "fuzzy_comfort_floor_pct": round(comfort_floor, 2),
            "fuzzy_safety_floor_pct": round(min_moisture, 2),
            "fuzzy_policy": "fuzzy_dt_prescription_control",
        }


DEFAULT_IRRIGATION_POLICY = IrrigationDomainPolicy()
DEFAULT_FUZZY_POLICY = FuzzyIrrigationPolicy()


def _baseline_decision_slot(day: date, observed_at: datetime, day_profile: dict[str, Any]) -> str | None:
    return DEFAULT_IRRIGATION_POLICY.decision_slot(day, observed_at, day_profile)


def _baseline_dormant_period(day: date) -> bool:
    return DEFAULT_IRRIGATION_POLICY.dormant_period(day)


def _make_baseline_irrigation_decision(state: PotState, pot: dict[str, Any], weather: dict[str, Any], day_profile: dict[str, Any], slot: str) -> dict[str, Any]:
    observed_local = _local_observed_at(weather)
    target = _baseline_target_for_pot(pot, day_profile, slot)
    threshold = _baseline_threshold_for_pot(pot, day_profile, slot)
    critical_low = _baseline_critically_low(state, pot, slot)
    rain_policy = _baseline_rain_policy(pot, observed_local.date(), day_profile)
    cadence_days = _baseline_cadence_days(day_profile)
    cadence_ok = _baseline_cadence_allows(pot, observed_local.date(), cadence_days)
    dose_factor = min(_baseline_temperature_dose_factor(day_profile), rain_policy["dose_factor"])
    reason_code = "moisture_ok"
    reason_detail = "Moisture is above the baseline decision threshold."
    should_irrigate = False

    if day_profile["freeze_risk"]:
        reason_code = "freeze_risk"
        reason_detail = "Skipped because freezing temperatures are present or forecast."
    elif slot == "winter_check" and not _baseline_winter_irrigation_allowed(state, day_profile):
        reason_code = "winter_conditions_not_met"
        reason_detail = "Winter watering requires very dry soil, 14 days above 5C, and no meaningful precipitation."
    elif slot == "evening" and not _baseline_second_watering_allowed(state, pot, day_profile):
        reason_code = "second_watering_not_needed"
        reason_detail = "Evening baseline watering is reserved for hot/extreme days and eligible or still-low pots."
    elif rain_policy["skip"] and not critical_low:
        reason_code = "rain_sufficient"
        reason_detail = (
            f"Skipped because effective rain is {rain_policy['effective_rain_mm']:.1f} mm "
            f"at {rain_policy['rain_probability_pct']:.0f}% probability."
        )
    elif _baseline_covered_rain_day(rain_policy, day_profile) and state.moisture < target:
        should_irrigate = True
        reason_code = "covered_rain_day"
        reason_detail = (
            f"Rain is present but this pot is not exposed; moisture {state.moisture:.1f}% "
            f"is below target {target:.1f}%, so baseline waters 25% less."
        )
    elif state.moisture < threshold or critical_low:
        should_irrigate = True
        if rain_policy["skip"] and critical_low:
            dose_factor = min(_baseline_temperature_dose_factor(day_profile), 0.5)
        reason_code = "baseline_moisture_temperature_rule"
        reason_detail = (
            f"Moisture {state.moisture:.1f}% is below threshold {threshold:.1f}%; "
            f"max temp {day_profile['max_temperature_c']:.1f}C, effective rain {rain_policy['effective_rain_mm']:.1f} mm."
        )
    elif not cadence_ok:
        reason_code = "cool_period_cadence"
        reason_detail = (
            f"Cool-season cadence allows irrigation every {cadence_days} days; "
            f"moisture {state.moisture:.1f}% is still above threshold {threshold:.1f}%."
        )

    dose_factor = _baseline_allowed_dose_factor(dose_factor)
    return {
        "pot_id": pot["id"],
        "pot_code": pot["pot_code"],
        "decided_at": observed_local.isoformat(),
        "date": observed_local.date().isoformat(),
        "slot": slot,
        "should_irrigate": should_irrigate,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "current_moisture_pct": round(state.moisture, 2),
        "threshold_pct": round(threshold, 2),
        "target_moisture_pct": round(target, 2),
        "weather_hourly_id": weather["id"],
        "dose_factor": round(dose_factor, 3),
        "effective_rain_mm": round(rain_policy["effective_rain_mm"], 2),
        "rain_exposure_factor": round(rain_policy["rain_exposure_factor"], 2),
        "rain_reduction_pct": round((1.0 - rain_policy["dose_factor"]) * 100.0, 1),
        "baseline_cadence_days": cadence_days,
    }


def _make_fuzzy_dt_decision(
    state: PotState,
    pot: dict[str, Any],
    weather: dict[str, Any],
    day_profile: dict[str, Any],
    slot: str = "morning",
) -> dict[str, Any]:
    return DEFAULT_FUZZY_POLICY.make_decision(state, pot, weather, day_profile, slot)


def _fuzzy_irrigation_prescription_mm(
    soil_moisture_pct: float,
    temperature_c: float,
    rain_mm: float,
) -> float:
    very_dry = _left_shoulder(soil_moisture_pct, 28.0, 42.0)
    dry = _triangular(soil_moisture_pct, 30.0, 45.0, 60.0)
    adequate = _triangular(soil_moisture_pct, 52.0, 66.0, 78.0)
    wet = _right_shoulder(soil_moisture_pct, 72.0, 86.0)

    cold = _left_shoulder(temperature_c, 8.0, 14.0)
    mild = _triangular(temperature_c, 10.0, 22.0, 30.0)
    hot = _right_shoulder(temperature_c, 27.0, 34.0)

    rain_none = _left_shoulder(rain_mm, 0.2, 1.0)
    rain_moderate = _triangular(rain_mm, 0.5, 2.5, 5.5)
    rain_heavy = _right_shoulder(rain_mm, 4.0, 8.0)

    rules = {
        "none": max(wet, cold, rain_heavy, min(adequate, rain_moderate)),
        "very_low": max(min(adequate, mild, rain_none), min(dry, rain_moderate), min(very_dry, rain_heavy)),
        "low": max(min(dry, mild, rain_none), min(adequate, hot, rain_none), min(very_dry, rain_moderate)),
        "medium": max(min(dry, hot, rain_none), min(very_dry, mild, rain_none)),
        "high": min(very_dry, hot, rain_none),
        "very_high": min(very_dry, hot, rain_none, _left_shoulder(rain_mm, 0.0, 0.4)),
    }
    return _defuzzify_irrigation_mm(rules)


def _three_input_irrigation_skip_reason(
    soil_moisture_pct: float,
    temperature_c: float,
    rain_mm: float,
) -> dict[str, str] | None:
    if temperature_c <= 3.0:
        return {
            "code": "cold_skip",
            "detail": "Skipped because temperature is too low for irrigation.",
        }
    if soil_moisture_pct >= 62.0:
        return {
            "code": "moisture_sufficient",
            "detail": f"Skipped because soil moisture {soil_moisture_pct:.1f}% is already sufficient.",
        }
    if rain_mm >= 4.0 and soil_moisture_pct > 28.0:
        return {
            "code": "rain_sufficient",
            "detail": f"Skipped because rain {rain_mm:.1f} mm is sufficient for the current moisture level.",
        }
    if rain_mm >= 1.0 and soil_moisture_pct >= 42.0:
        return {
            "code": "rain_and_moisture_sufficient",
            "detail": f"Skipped because rain {rain_mm:.1f} mm and moisture {soil_moisture_pct:.1f}% are sufficient.",
        }
    if temperature_c <= 10.0 and soil_moisture_pct >= 35.0:
        return {
            "code": "cool_day_moisture_sufficient",
            "detail": (
                f"Skipped because temperature {temperature_c:.1f}C is cool and "
                f"soil moisture {soil_moisture_pct:.1f}% is sufficient."
            ),
        }
    return None


def _fuzzy_trigger_threshold_for_pot(pot: dict[str, Any], day_profile: dict[str, Any]) -> float:
    return DEFAULT_FUZZY_POLICY.trigger_threshold(pot, day_profile, "morning")


def _defuzzify_irrigation_mm(rule_strengths: dict[str, float]) -> float:
    output_memberships = _defuzz_output_memberships()
    numerator = 0.0
    denominator = 0.0
    active_terms = [
        (output_memberships[term], float(strength))
        for term, strength in rule_strengths.items()
        if strength > 0
    ]
    for index, x in enumerate(_DEFUZZ_X_VALUES):
        membership = 0.0
        for term_memberships, strength in active_terms:
            term_membership = term_memberships[index]
            if term_membership:
                membership = max(membership, min(strength, term_membership))
        numerator += x * membership
        denominator += membership
    return round(numerator / denominator, 2) if denominator > 0 else 0.0


def _defuzz_output_memberships() -> dict[str, tuple[float, ...]]:
    global _DEFUZZ_OUTPUT_MEMBERSHIPS
    if _DEFUZZ_OUTPUT_MEMBERSHIPS is None:
        _DEFUZZ_OUTPUT_MEMBERSHIPS = {
            term: tuple(_triangular(x, *points) for x in _DEFUZZ_X_VALUES)
            for term, points in _FUZZY_OUTPUT_SETS.items()
        }
    return _DEFUZZ_OUTPUT_MEMBERSHIPS


def _triangular(value: float, left: float, center: float, right: float) -> float:
    value = float(value)
    if left == center and value <= center:
        return 1.0
    if center == right and value >= center:
        return 1.0
    if value <= left or value >= right:
        return 0.0
    if value == center:
        return 1.0
    if value < center:
        return (value - left) / max(center - left, 1e-9)
    return (right - value) / max(right - center, 1e-9)

# The fuzzy prescription uses the same three inputs as ANFIS:
# soil moisture, temperature, and rain.
def _left_shoulder(value: float, full_until: float, zero_at: float) -> float:
    value = float(value)
    if value <= full_until:
        return 1.0
    if value >= zero_at:
        return 0.0
    return (zero_at - value) / max(zero_at - full_until, 1e-9)


def _right_shoulder(value: float, zero_until: float, full_at: float) -> float:
    value = float(value)
    if value <= zero_until:
        return 0.0
    if value >= full_at:
        return 1.0
    return (value - zero_until) / max(full_at - zero_until, 1e-9)


def _size_flow_rate_multiplier(pot: dict[str, Any]) -> float:
    return 1.0


def _pot_surface_area_m2(pot: dict[str, Any]) -> float:
    if pot["size_class"] == "small":
        return {
            "window_box": 0.06,
            "hanging": 0.04,
            "tabletop": 0.025,
        }.get(pot.get("small_subtype"), 0.04)
    return {
        "medium": 0.09,
        "large": 0.18,
        "huge": 0.32,
    }[pot["size_class"]]


def _fuzzy_prescribed_volume_ml(state: PotState, pot: dict[str, Any], prescription_mm: float) -> float:
    return DEFAULT_FUZZY_POLICY.prescribed_volume_ml(state, pot, prescription_mm)


def _fuzzy_prescribed_request(pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_FUZZY_POLICY.irrigation_request(pot, weather, decision)


def _apply_fuzzy_prescribed_event(state: PotState, pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    event = _fuzzy_prescribed_request(pot, weather, decision)
    return _apply_event_delivery(state, pot, event, event["requested_volume_ml"], event.get("duration_min"))


def _baseline_irrigation_request(pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_IRRIGATION_POLICY.irrigation_request(pot, weather, decision)


def _apply_baseline_irrigation_event(state: PotState, pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    event = _baseline_irrigation_request(pot, weather, decision)
    return _apply_event_delivery(state, pot, event, event["requested_volume_ml"], event.get("duration_min"))


def _apply_event_delivery(
    state: PotState,
    pot: dict[str, Any],
    event: dict[str, Any],
    delivered_volume_ml: float,
    duration_min: float | None = None,
) -> dict[str, Any]:
    delivered_volume_ml = max(0.0, float(delivered_volume_ml or 0.0))
    requested_volume_ml = max(0.0, _number(event.get("requested_volume_ml"), event.get("planned_volume_ml", 0.0)))
    flow_rate = max(_number(event.get("flow_rate_ml_min"), pot["drip_flow_ml_min"]), 1.0)
    runtime_min = max(0.0, _number(duration_min, delivered_volume_ml / flow_rate if flow_rate > 0 else 0.0))
    cycle_count = 2 if pot["cycle_soak_enabled"] and runtime_min >= 10 else 1
    soak_pause_min = 10 if cycle_count == 2 else 0

    _apply_planned_volume(state, pot, delivered_volume_ml)

    scheduled_start = datetime.fromisoformat(str(event["scheduled_start_at"]))
    scheduled_end = scheduled_start + timedelta(minutes=runtime_min + soak_pause_min)
    output = dict(event)
    output.update(
        {
            "scheduled_end_at": scheduled_end.isoformat(),
            "duration_min": round(runtime_min, 3),
            "valve_runtime_min": round(runtime_min, 3),
            "cycle_count": cycle_count,
            "soak_pause_min": soak_pause_min,
            "requested_volume_ml": round(requested_volume_ml, 2),
            "delivered_volume_ml": round(delivered_volume_ml, 2),
            "planned_volume_ml": round(delivered_volume_ml, 2),
            "delivery_error_ml": round(delivered_volume_ml - requested_volume_ml, 2),
            "delivery_ratio": round(delivered_volume_ml / requested_volume_ml, 4) if requested_volume_ml > 0 else None,
            "physical_distribution_policy": "valve_runtime_x_pot_drip_flow",
        }
    )
    return output


def _baseline_full_dose_start_moisture(pot: dict[str, Any], decision: dict[str, Any], target: float) -> float:
    return DEFAULT_IRRIGATION_POLICY.full_dose_start_moisture(pot, decision, target)


def _apply_planned_volume(state: PotState, pot: dict[str, Any], planned_volume_ml: float) -> None:
    volume_l = pot["volume_l"]
    retention = max(pot["retention_factor"], 0.1)
    moisture_gain = planned_volume_ml * retention / max(volume_l * 10.0, 1.0)
    state.moisture = _clamp(state.moisture + moisture_gain, 0.0, 100.0)




def _baseline_target_for_pot(pot: dict[str, Any], day_profile: dict[str, Any], slot: str) -> float:
    return DEFAULT_IRRIGATION_POLICY.target_moisture(pot, day_profile, slot)


def _baseline_threshold_for_pot(pot: dict[str, Any], day_profile: dict[str, Any], slot: str) -> float:
    return DEFAULT_IRRIGATION_POLICY.trigger_threshold(pot, day_profile, slot)


def _baseline_temperature_dose_factor(day_profile: dict[str, Any]) -> float:
    return DEFAULT_IRRIGATION_POLICY.temperature_dose_factor(day_profile)


def _baseline_allowed_dose_factor(value: float) -> float:
    return DEFAULT_IRRIGATION_POLICY.allowed_dose_factor(value)


def _baseline_rain_policy(pot: dict[str, Any], day: date, day_profile: dict[str, Any]) -> dict[str, Any]:
    return DEFAULT_IRRIGATION_POLICY.rain_policy(pot, day, day_profile)


def _baseline_covered_rain_day(rain_policy: dict[str, Any], day_profile: dict[str, Any]) -> bool:
    return DEFAULT_IRRIGATION_POLICY.is_covered_rain_day(rain_policy, day_profile)


def _rain_exposure_factor(pot: dict[str, Any], day: date) -> float:
    if not _is_outdoor(pot, day):
        return 0.0
    rain_exposure = str(pot.get("rain_exposure") or "")
    if rain_exposure:
        return {
            "covered": 0.0,
            "partially_exposed": 0.5,
            "fully_exposed": 1.0,
        }.get(rain_exposure, 0.5)

    zone = str(pot.get("balcony_zone") or "")
    if zone in {"north_shelter"}:
        return 0.0
    if zone in {"west_wall", "east_corner"}:
        return 0.5
    if zone in {"south_rail", "hanging_row"}:
        return 1.0
    if str(pot.get("sun_exposure") or "") in {"full", "reflected_heat"}:
        return 1.0
    return 0.5


def _baseline_cadence_days(day_profile: dict[str, Any]) -> int:
    return DEFAULT_IRRIGATION_POLICY.cadence_days(day_profile)


def _baseline_cadence_allows(pot: dict[str, Any], day: date, cadence_days: int) -> bool:
    return DEFAULT_IRRIGATION_POLICY.cadence_allows(pot, day, cadence_days)


def _baseline_critically_low(state: PotState, pot: dict[str, Any], slot: str) -> bool:
    return DEFAULT_IRRIGATION_POLICY.critically_low(state, pot, slot)


def _baseline_winter_irrigation_allowed(state: PotState, day_profile: dict[str, Any]) -> bool:
    return DEFAULT_IRRIGATION_POLICY.winter_irrigation_allowed(state, day_profile)


def _baseline_second_watering_allowed(state: PotState, pot: dict[str, Any], day_profile: dict[str, Any]) -> bool:
    return DEFAULT_IRRIGATION_POLICY.second_watering_allowed(state, pot, day_profile)


def _baseline_high_need_pot(pot: dict[str, Any]) -> bool:
    return DEFAULT_IRRIGATION_POLICY.high_need_pot(pot)


def _baseline_heat_priority_pot(pot: dict[str, Any]) -> bool:
    return DEFAULT_IRRIGATION_POLICY.heat_priority_pot(pot)


def _threshold_for_pot(pot: dict[str, Any], day_profile: dict[str, Any], slot: str) -> float:
    if slot == "winter_check":
        return 10.0
    threshold = pot["moisture_min_pct"]
    if day_profile["heatwave_day"] and pot["plant_type_code"] in {"vegetables", "herbs"}:
        threshold += 4.0
    if day_profile["dry_windy_day"] and pot["size_class"] == "small":
        threshold += 3.0
    if slot == "evening":
        threshold = max(8.0, threshold - 3.0)
    return threshold


def _winter_irrigation_allowed(state: PotState, day_profile: dict[str, Any]) -> bool:
    return (
        day_profile["max_temperature_c"] > 10.0
        and day_profile["no_rain_10_days"]
        and state.moisture < 10.0
    )


def _second_watering_allowed(state: PotState, pot: dict[str, Any], day_profile: dict[str, Any]) -> bool:
    eligible = pot["allows_second_watering"] or pot["size_class"] == "small"
    return eligible and (day_profile["heatwave_day"] or day_profile["dry_windy_day"]) and state.moisture < pot["moisture_target_pct"]


def _is_emergency_dryness(state: PotState, pot: dict[str, Any], day: date, observed_at: datetime) -> bool:
    if _season(day) == "summer" and 11 <= observed_at.hour <= 16:
        return state.moisture < max(8.0, pot["moisture_min_pct"] - 8.0)
    return False


def _alert_row(pot: dict[str, Any], weather: dict[str, Any], alert_type: str, severity: str, title: str) -> dict[str, Any]:
    observed_local = _local_observed_at(weather)
    return {
        "pot_id": pot["id"],
        "pot_code": pot["pot_code"],
        "raised_at": observed_local.isoformat(),
        "alert_type": alert_type,
        "severity": severity,
        "title": title,
        "detail": f"{pot['pot_code']} at {observed_local.isoformat()}",
    }


def _is_outdoor(pot: dict[str, Any], day: date) -> bool:
    return True


def _cold_month(day: date) -> bool:
    return day.month in {11, 12, 1, 2, 3}


def _upcoming_freeze(day: date, weather_by_day: dict[date, list[dict[str, Any]]], days: int = 3) -> bool:
    for offset in range(1, days + 1):
        rows = weather_by_day.get(day + timedelta(days=offset), [])
        if rows and min(_number(row["temperature_c"], 20.0) for row in rows) <= 0:
            return True
    return False


def _precipitation_last_days(day: date, weather_by_day: dict[date, list[dict[str, Any]]], days: int = 10) -> float:
    total = 0.0
    for offset in range(1, days + 1):
        rows = weather_by_day.get(day - timedelta(days=offset), [])
        total += sum(_number(row["precipitation_mm"], 0.0) for row in rows)
    return total
