from __future__ import annotations

from datetime import date
from typing import Any

from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.shared.types import PotState
from digital_twin.simulation.soil_model import local_observed_at


def make_baseline_irrigation_decision(state: PotState, pot: dict[str, Any], weather: dict[str, Any], day_profile: dict[str, Any], slot: str) -> dict[str, Any]:
    observed_local = local_observed_at(weather)
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
