from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping

from digital_twin.domain.pot import Pot
from digital_twin.simulation.shared.types import PotState
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.domain.weather import local_observed_at

PotInput = Mapping[str, Any] | Pot

_FUZZY_OUTPUT_SETS = {
    "none": (0.0, 0.0, 0.4),
    "very_low": (0.0, 0.8, 1.6),
    "low": (1.0, 2.0, 3.2),
    "medium": (2.6, 4.0, 5.4),
    "high": (4.6, 6.0, 7.2),
    "very_high": (6.4, 8.0, 8.0),
}
_FUZZY_SIGNAL_MAX = 8.0
_DEFUZZ_X_VALUES = tuple(index / 20.0 for index in range(161))
_DEFUZZ_OUTPUT_MEMBERSHIPS: dict[str, tuple[float, ...]] | None = None


class FuzzyIrrigationPolicy:
    """Fuzzy DT controller rules, separate from the default threshold policy."""

    comfort_target_offset_pct = 6.0
    comfort_minimum_margin_pct = 3.0
    minimum_prescription_score_pct = 12.5
    low_moisture_prescription_score_pct = 3.125

    def decision_slot(self, day: date, observed_at: datetime, day_profile: dict[str, Any]) -> str | None:
        hour = observed_at.hour
        max_temp = soil.number(day_profile.get("max_temperature_c"), 20.0)

        if day.month in {12, 1, 2, 3}:
            return "winter_check" if hour == 10 else None
        if hour == 6:
            return "daily_prescription"
        if (max_temp >= 35.0 or day_profile.get("heatwave_day")) and hour == 18:
            return "evening"
        return None

    def comfort_floor(self, pot: PotInput, day_profile: dict[str, Any], slot: str = "morning") -> float:
        domain_pot = Pot.from_mapping(pot)
        target = domain_pot.moisture_target_pct
        min_moisture = domain_pot.moisture_min_pct
        floor = max(
            min_moisture + self.comfort_minimum_margin_pct,
            target - self.comfort_target_offset_pct,
        )
        if domain_pot.is_high_need():
            floor += 1.0
        if day_profile.get("heatwave_day"):
            floor += 1.5 if domain_pot.is_heat_priority() else 0.75
        if day_profile.get("dry_windy_day"):
            floor += 1.5 if domain_pot.size_class == "small" else 0.75
        if slot == "winter_check":
            floor = max(min_moisture, domain_pot.winter_moisture_target_pct - 3.0)
        return soil.clamp(floor, 5.0, target - 1.0)

    def trigger_threshold(self, pot: PotInput, day_profile: dict[str, Any], slot: str = "morning") -> float:
        domain_pot = Pot.from_mapping(pot)
        threshold = self.comfort_floor(pot, day_profile, slot)
        if slot == "evening":
            threshold = max(threshold, domain_pot.moisture_min_pct + 2.0)
        return soil.clamp(threshold, 5.0, domain_pot.moisture_target_pct - 2.0)

    def heat_priority_pot(self, pot: PotInput) -> bool:
        return Pot.from_mapping(pot).is_heat_priority()

    def high_need_pot(self, pot: PotInput) -> bool:
        return Pot.from_mapping(pot).is_high_need()

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
        return None

    def prescription_score_pct(
        self,
        soil_moisture_pct: float,
        temperature_c: float,
        rain_mm: float,
    ) -> float:
        return _fuzzy_irrigation_score_pct(
            soil_moisture_pct=soil_moisture_pct,
            temperature_c=temperature_c,
            rain_mm=rain_mm,
        )

    def volume_for_score_pct(self, pot: PotInput, prescription_score_pct: float) -> float:
        domain_pot = Pot.from_mapping(pot)
        flow_rate = domain_pot.effective_flow_rate_ml_min()
        max_minutes = {"huge": 60, "large": 42, "medium": 27, "small": 17}[domain_pot.size_class]
        signal = _score_pct_to_signal(prescription_score_pct)
        requested_volume_ml = max(0.0, signal) * domain_pot.surface_area_m2() * 1000.0
        return min(requested_volume_ml, flow_rate * max_minutes)

    def prescribed_volume_ml(self, state: PotState, pot: PotInput, prescription_score_pct: float) -> float:
        _ = state
        return self.volume_for_score_pct(pot, prescription_score_pct)

    def irrigation_request(self, pot: PotInput, weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        domain_pot = Pot.from_mapping(pot)
        planned_volume_ml = max(0.0, soil.number(decision.get("planned_volume_ml"), 0.0))
        flow_rate = domain_pot.effective_flow_rate_ml_min()
        duration_min = domain_pot.runtime_min_for_volume(planned_volume_ml)
        cycle_count = domain_pot.cycle_count_for_runtime(duration_min)
        soak_pause_min = domain_pot.soak_pause_min_for_runtime(duration_min)

        scheduled_start = local_observed_at(weather)
        scheduled_end = scheduled_start + timedelta(minutes=duration_min + soak_pause_min)
        sensor_id = int(decision.get("sensor_id", domain_pot.id))
        return {
            "pot_id": domain_pot.id,
            "sensor_id": sensor_id,
            "request_sensor_id": sensor_id,
            "pot_code": domain_pot.pot_code,
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
            "prescription_volume_ml": round(soil.number(decision.get("prescription_volume_ml"), planned_volume_ml), 2),
            "prescription_score_pct": decision.get("prescription_score_pct", 0.0),
        }

    def make_decision(
        self,
        state: PotState,
        pot: PotInput,
        weather: dict[str, Any],
        day_profile: dict[str, Any],
        slot: str = "morning",
    ) -> dict[str, Any]:
        domain_pot = Pot.from_mapping(pot)
        observed_local = local_observed_at(weather)
        temperature_c = soil.number(weather.get("temperature_c"), day_profile["avg_temperature_c"])
        rain_mm = soil.number(day_profile.get("precipitation_mm"), 0.0)
        comfort_floor = self.comfort_floor(pot, day_profile, slot)
        min_moisture = domain_pot.moisture_min_pct
        skip_reason = self.skip_reason(
            state.moisture,
            temperature_c,
            rain_mm,
            comfort_floor_pct=comfort_floor,
            minimum_moisture_pct=min_moisture,
        )
        trigger_threshold = self.trigger_threshold(pot, day_profile, slot)
        prescription_score_pct = self.prescription_score_pct(
            soil_moisture_pct=state.moisture,
            temperature_c=temperature_c,
            rain_mm=rain_mm,
        )
        hard_safety_deficit = state.moisture <= min_moisture
        comfort_deficit = state.moisture < comfort_floor
        if not skip_reason and hard_safety_deficit:
            prescription_score_pct = max(prescription_score_pct, self.low_moisture_prescription_score_pct)
        if skip_reason:
            prescription_score_pct = 0.0
            planned_volume_ml = 0.0
        else:
            planned_volume_ml = self.prescribed_volume_ml(state, pot, prescription_score_pct)
        activation_volume_ml = self.volume_for_score_pct(pot, self.minimum_prescription_score_pct)
        low_moisture_volume_ml = self.volume_for_score_pct(pot, self.low_moisture_prescription_score_pct)
        strong_prescription_signal = prescription_score_pct >= self.minimum_prescription_score_pct
        low_moisture_prescription_signal = (
            comfort_deficit
            and prescription_score_pct >= self.low_moisture_prescription_score_pct
        )
        should_irrigate = (
            skip_reason is None
            and planned_volume_ml >= 10.0
            and (
                strong_prescription_signal
                or low_moisture_prescription_signal
                or hard_safety_deficit
            )
        )

        if skip_reason:
            reason_code = f"fuzzy_{skip_reason['code']}"
            reason_detail = skip_reason["detail"]
        elif hard_safety_deficit and should_irrigate:
            reason_code = "fuzzy_moisture_safety_floor"
            reason_detail = (
                f"Soil moisture {state.moisture:.1f}% is at or below the safety floor "
                f"{min_moisture:.1f}%; fuzzy applies a {planned_volume_ml:.1f} ml safety supplement."
            )
        elif comfort_deficit and should_irrigate:
            reason_code = "fuzzy_low_moisture_prescription"
            reason_detail = (
                f"Soil moisture {state.moisture:.1f}% is below fuzzy comfort floor "
                f"{comfort_floor:.1f}%; fuzzy volume request {planned_volume_ml:.1f} ml is accepted."
            )
        elif should_irrigate:
            reason_code = "fuzzy_prescription_signal"
            reason_detail = (
                f"FIS volume request {planned_volume_ml:.1f} ml exceeds the useful activation volume "
                f"{activation_volume_ml:.1f} ml."
            )
        elif planned_volume_ml < 10.0 and prescription_score_pct > 0.0:
            reason_code = "fuzzy_prescription_below_runtime_minimum"
            reason_detail = (
                f"FIS volume request {planned_volume_ml:.1f} ml is below the useful valve runtime minimum."
            )
        elif prescription_score_pct > 0.0:
            reason_code = "fuzzy_prescription_below_activation_minimum"
            reason_detail = (
                f"FIS volume request {planned_volume_ml:.1f} ml is below the activation volume "
                f"{activation_volume_ml:.1f} ml; it can still be applied passively if another pot opens the valve zone."
            )
        elif not should_irrigate:
            reason_code = "fuzzy_no_irrigation"
            reason_detail = f"FIS volume request {planned_volume_ml:.1f} ml does not require irrigation."
        else:
            reason_code = "fuzzy_prescription"
            reason_detail = (
                f"FIS volume request {planned_volume_ml:.1f} ml from moisture {state.moisture:.1f}%, "
                f"temperature {temperature_c:.1f}C, rain {rain_mm:.2f} mm."
            )

        return {
            "pot_id": domain_pot.id,
            "pot_code": domain_pot.pot_code,
            "decided_at": observed_local.isoformat(),
            "date": observed_local.date().isoformat(),
            "slot": slot,
            "should_irrigate": should_irrigate,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
            "current_moisture_pct": round(state.moisture, 2),
            "target_moisture_pct": round(domain_pot.moisture_target_pct, 2),
            "weather_hourly_id": weather["id"],
            "prescription_score_pct": round(prescription_score_pct, 2),
            "prescription_volume_ml": round(planned_volume_ml, 2),
            "temperature_c": round(temperature_c, 2),
            "rain_forecast_mm": round(rain_mm, 2),
            "planned_volume_ml": round(planned_volume_ml, 2),
            "fuzzy_trigger_threshold_pct": round(trigger_threshold, 2),
            "fuzzy_comfort_floor_pct": round(comfort_floor, 2),
            "fuzzy_safety_floor_pct": round(min_moisture, 2),
            "fuzzy_activation_volume_ml": round(activation_volume_ml, 2),
            "fuzzy_low_moisture_volume_ml": round(low_moisture_volume_ml, 2),
            "fuzzy_policy": "fuzzy_dt_volume_control",
        }


def _fuzzy_irrigation_score_pct(
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
    signal = soil.clamp(_defuzzify_irrigation_signal(rules), 0.0, _FUZZY_SIGNAL_MAX)
    return round(signal / _FUZZY_SIGNAL_MAX * 100.0, 2)


def _defuzzify_irrigation_signal(rule_strengths: dict[str, float]) -> float:
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


def _score_pct_to_signal(score_pct: float) -> float:
    return soil.clamp(float(score_pct), 0.0, 100.0) / 100.0 * _FUZZY_SIGNAL_MAX


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
