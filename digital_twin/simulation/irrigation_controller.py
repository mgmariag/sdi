from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from digital_twin.simulation.dto import PotState
from digital_twin.simulation.soil_model import (
    clamp as _clamp,
    local_observed_at as _local_observed_at,
    number as _number,
    season as _season,
    sun_factor as _sun_factor,
    wind_factor as _wind_factor,
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
def _decision_slot(day: date, observed_at: datetime, day_profile: dict[str, Any]) -> str | None:
    hour = observed_at.hour
    season = day_profile["season"]
    if season == "summer":
        if 6 <= hour <= 8:
            return "morning"
        if day_profile["heatwave_day"] and 17 <= hour <= 19:
            return "evening"
    elif season == "winter":
        if hour == 10:
            return "winter_check"
    elif season == "autumn":
        if 8 <= hour <= 10:
            return "morning"
    else:
        if day_profile["min_temperature_c"] < 7.0:
            return "morning" if 9 <= hour <= 10 else None
        if 8 <= hour <= 10:
            return "morning"
    return None


def _make_irrigation_decision(state: PotState, pot: dict[str, Any], weather: dict[str, Any], day_profile: dict[str, Any], slot: str) -> dict[str, Any]:
    target = pot["winter_moisture_target_pct"] if slot == "winter_check" else pot["moisture_target_pct"]
    threshold = _threshold_for_pot(pot, day_profile, slot)
    reason_code = "moisture_ok"
    reason_detail = "Moisture is above the decision threshold."
    should_irrigate = False
    observed_local = _local_observed_at(weather)

    if day_profile["freeze_risk"]:
        reason_code = "freeze_risk"
        reason_detail = "Skipped because freezing temperatures are present or forecast."
    elif slot == "winter_check" and not _winter_irrigation_allowed(state, day_profile):
        reason_code = "winter_conditions_not_met"
        reason_detail = "Winter watering requires temperature above 10C, dry soil, and no recent rain."
    elif slot == "evening" and not _second_watering_allowed(state, pot, day_profile):
        reason_code = "second_watering_not_needed"
        reason_detail = "Evening watering is reserved for heatwave/dry wind stress and eligible pots."
    elif _is_outdoor(pot, observed_local.date()) and day_profile["precipitation_mm"] >= 2.0 and state.moisture > threshold * 0.85:
        reason_code = "rain_sufficient"
        reason_detail = "Skipped because stored weather has enough daily precipitation."
    elif state.moisture < threshold:
        should_irrigate = True
        reason_code = "below_moisture_threshold"
        reason_detail = f"Moisture {state.moisture:.1f}% is below threshold {threshold:.1f}%."

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
        "target_moisture_pct": round(target, 2),
        "weather_hourly_id": weather["id"],
    }


def _make_fuzzy_dt_decision(state: PotState, pot: dict[str, Any], weather: dict[str, Any], day_profile: dict[str, Any]) -> dict[str, Any]:
    observed_local = _local_observed_at(weather)
    dap = _days_after_planting(observed_local.date(), pot)
    etc_mm = _crop_evapotranspiration_mm(day_profile, pot, dap)
    prescription_mm = _fuzzy_irrigation_prescription_mm(
        soil_moisture_pct=state.moisture,
        target_moisture_pct=pot["moisture_target_pct"],
        max_moisture_pct=pot["moisture_max_pct"],
        etc_mm=etc_mm,
        dap=dap,
        rain_forecast_mm=day_profile["precipitation_mm"],
        rain_probability_pct=day_profile["max_precipitation_probability_pct"],
        freeze_risk=day_profile["freeze_risk"],
    )
    planned_volume_ml = _fuzzy_prescribed_volume_ml(state, pot, prescription_mm)
    should_irrigate = prescription_mm >= 0.25 and planned_volume_ml >= 10.0

    if day_profile["freeze_risk"]:
        reason_code = "fuzzy_freeze_risk"
        reason_detail = "FIS prescription suppressed because freezing temperatures are present or forecast."
    elif not should_irrigate:
        reason_code = "fuzzy_no_irrigation"
        reason_detail = f"FIS prescription {prescription_mm:.2f} mm does not require irrigation."
    else:
        reason_code = "fuzzy_prescription"
        reason_detail = (
            f"FIS prescription {prescription_mm:.2f} mm from ETc {etc_mm:.2f} mm, "
            f"moisture {state.moisture:.1f}%, DAP {dap}, rain forecast {day_profile['precipitation_mm']:.2f} mm."
        )

    return {
        "pot_id": pot["id"],
        "pot_code": pot["pot_code"],
        "decided_at": observed_local.isoformat(),
        "date": observed_local.date().isoformat(),
        "slot": "morning",
        "should_irrigate": should_irrigate,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "current_moisture_pct": round(state.moisture, 2),
        "target_moisture_pct": round(pot["moisture_target_pct"], 2),
        "weather_hourly_id": weather["id"],
        "prescription_mm": round(prescription_mm, 2),
        "etc_mm": round(etc_mm, 2),
        "days_after_planting": dap,
        "rain_forecast_mm": round(day_profile["precipitation_mm"], 2),
        "rain_probability_pct": round(day_profile["max_precipitation_probability_pct"], 2),
        "planned_volume_ml": round(planned_volume_ml, 2),
    }


def _make_fao_pm_decision(state: PotState, pot: dict[str, Any], weather: dict[str, Any], day_profile: dict[str, Any]) -> dict[str, Any]:
    observed_local = _local_observed_at(weather)
    dap = _days_after_planting(observed_local.date(), pot)
    etc_mm = _crop_evapotranspiration_mm(day_profile, pot, dap)
    prescription_mm = 0.0 if day_profile["freeze_risk"] else etc_mm
    planned_volume_ml = _fuzzy_prescribed_volume_ml(state, pot, prescription_mm)
    should_irrigate = prescription_mm >= 0.25 and planned_volume_ml >= 10.0

    if day_profile["freeze_risk"]:
        reason_code = "fao_freeze_risk"
        reason_detail = "FAO/PM prescription suppressed because freezing temperatures are present or forecast."
    elif not should_irrigate:
        reason_code = "fao_no_irrigation"
        reason_detail = f"FAO/PM prescription {prescription_mm:.2f} mm does not require irrigation."
    else:
        reason_code = "fao_pm_prescription"
        reason_detail = f"FAO/PM prescription {prescription_mm:.2f} mm from ETc {etc_mm:.2f} mm."

    return {
        "pot_id": pot["id"],
        "pot_code": pot["pot_code"],
        "decided_at": observed_local.isoformat(),
        "date": observed_local.date().isoformat(),
        "slot": "morning",
        "should_irrigate": should_irrigate,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "current_moisture_pct": round(state.moisture, 2),
        "target_moisture_pct": round(pot["moisture_target_pct"], 2),
        "weather_hourly_id": weather["id"],
        "prescription_mm": round(prescription_mm, 2),
        "etc_mm": round(etc_mm, 2),
        "days_after_planting": dap,
        "rain_forecast_mm": round(day_profile["precipitation_mm"], 2),
        "rain_probability_pct": round(day_profile["max_precipitation_probability_pct"], 2),
        "planned_volume_ml": round(planned_volume_ml, 2),
    }


def _fuzzy_irrigation_prescription_mm(
    soil_moisture_pct: float,
    target_moisture_pct: float,
    max_moisture_pct: float,
    etc_mm: float,
    dap: int,
    rain_forecast_mm: float,
    rain_probability_pct: float,
    freeze_risk: bool,
) -> float:
    if freeze_risk:
        return 0.0

    deficit_pct = max(0.0, target_moisture_pct - soil_moisture_pct)
    surplus_pct = max(0.0, soil_moisture_pct - target_moisture_pct)
    dry = _triangular(deficit_pct, 4.0, 11.0, 20.0)
    very_dry = _right_shoulder(deficit_pct, 14.0, 26.0)
    slight_deficit = _triangular(deficit_pct, 0.0, 5.0, 12.0)
    adequate = _triangular(surplus_pct, 0.0, 2.0, 8.0)
    wet = max(_right_shoulder(soil_moisture_pct, target_moisture_pct + 6.0, max_moisture_pct), _right_shoulder(surplus_pct, 8.0, 16.0))

    et_low = _left_shoulder(etc_mm, 0.8, 1.8)
    et_medium = _triangular(etc_mm, 1.2, 3.0, 4.8)
    et_high = _right_shoulder(etc_mm, 3.8, 6.2)

    stage_initial = _left_shoulder(float(dap), 18.0, 42.0)
    stage_development = _triangular(float(dap), 25.0, 70.0, 115.0)
    stage_mid = _right_shoulder(float(dap), 80.0, 130.0)
    active_stage = max(stage_development, stage_mid * 0.95)

    rain_none = _left_shoulder(rain_forecast_mm, 0.2, 1.0)
    rain_moderate = _triangular(rain_forecast_mm, 0.5, 2.5, 5.5)
    rain_heavy = _right_shoulder(rain_forecast_mm, 4.0, 8.0)

    prob_low = _left_shoulder(rain_probability_pct, 25.0, 55.0)
    prob_medium = _triangular(rain_probability_pct, 35.0, 60.0, 85.0)
    prob_high = _right_shoulder(rain_probability_pct, 70.0, 90.0)

    rules = {
        "none": max(wet, rain_heavy, min(rain_moderate, prob_high), min(adequate, et_low)),
        "very_low": max(min(adequate, et_medium), min(slight_deficit, et_low), min(stage_initial, prob_medium)),
        "low": max(min(slight_deficit, et_medium, rain_none), min(dry, et_low), min(dry, rain_moderate)),
        "medium": max(min(dry, et_medium, rain_none), min(slight_deficit, et_high, prob_low), min(dry, active_stage, prob_medium)),
        "high": max(min(dry, et_high, prob_low), min(very_dry, et_medium, rain_none), min(very_dry, active_stage)),
        "very_high": min(very_dry, et_high, active_stage, rain_none, prob_low),
    }
    return _defuzzify_irrigation_mm(rules)


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


def _days_after_planting(day: date, pot: dict[str, Any]) -> int:
    start_month_day = {
        "vegetables": (4, 1),
        "herbs": (4, 15),
        "flowering": (4, 1),
        "shrubs": (3, 1),
        "succulents": (3, 15),
    }.get(pot["plant_type_code"], (4, 1))
    season_start = date(day.year, start_month_day[0], start_month_day[1])
    if day < season_start:
        season_start = date(day.year - 1, start_month_day[0], start_month_day[1])
    return max(1, min((day - season_start).days + 1, 240))


def _crop_evapotranspiration_mm(day_profile: dict[str, Any], pot: dict[str, Any], dap: int) -> float:
    reference_et = max(0.0, _number(day_profile.get("reference_evapotranspiration_mm"), 0.0))
    base_crop = {
        "vegetables": 1.08,
        "herbs": 0.92,
        "flowering": 0.95,
        "shrubs": 0.78,
        "succulents": 0.38,
    }.get(pot["plant_type_code"], 0.9)
    if dap <= 35:
        stage = 0.62
    elif dap <= 85:
        stage = 0.88
    elif dap <= 150:
        stage = 1.08
    else:
        stage = 0.82
    sun_factor = pot.get("_sun_factor")
    if sun_factor is None:
        sun_factor = _sun_factor(pot)
    wind_factor = pot.get("_wind_factor")
    if wind_factor is None:
        wind_factor = _wind_factor(pot)
    exposure = 0.85 + (sun_factor - 1.0) * 0.45 + (wind_factor - 1.0) * 0.3
    return round(reference_et * base_crop * stage * max(0.45, exposure), 2)




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
    return max(0.0, prescription_mm) * _pot_surface_area_m2(pot) * 1000.0


def _apply_fuzzy_prescribed_event(state: PotState, pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    planned_volume_ml = max(0.0, _number(decision.get("planned_volume_ml"), 0.0))
    flow_rate = max(pot["drip_flow_ml_min"] * _size_flow_rate_multiplier(pot), 1.0)
    duration_min = planned_volume_ml / flow_rate if flow_rate > 0 else 0.0
    cycle_count = 2 if pot["cycle_soak_enabled"] and duration_min >= 10 else 1
    soak_pause_min = 10 if cycle_count == 2 else 0

    _apply_planned_volume(state, pot, planned_volume_ml)

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
        "cycle_count": cycle_count,
        "soak_pause_min": soak_pause_min,
        "prescription_mm": decision.get("prescription_mm", 0.0),
    }


def _apply_irrigation_event(state: PotState, pot: dict[str, Any], weather: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    target = decision["target_moisture_pct"]
    need_pct = max(0.0, target - state.moisture)
    volume_l = pot["volume_l"]
    retention = max(pot["retention_factor"], 0.1)
    flow_rate = max(pot["drip_flow_ml_min"] * _size_flow_rate_multiplier(pot), 1.0)
    planned_volume_ml = max(0.0, need_pct * volume_l * 10.0 / retention)
    max_minutes = {"huge": 90, "large": 60, "medium": 35, "small": 20}[pot["size_class"]]
    planned_volume_ml = min(planned_volume_ml, flow_rate * max_minutes)
    duration_min = planned_volume_ml / flow_rate
    cycle_count = 2 if pot["cycle_soak_enabled"] and duration_min >= 10 else 1
    soak_pause_min = 10 if cycle_count == 2 else 0

    _apply_planned_volume(state, pot, planned_volume_ml)

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
        "cycle_count": cycle_count,
        "soak_pause_min": soak_pause_min,
    }


def _apply_planned_volume(state: PotState, pot: dict[str, Any], planned_volume_ml: float) -> None:
    volume_l = pot["volume_l"]
    retention = max(pot["retention_factor"], 0.1)
    moisture_gain = planned_volume_ml * retention / max(volume_l * 10.0, 1.0)
    state.moisture = _clamp(state.moisture + moisture_gain, 0.0, 100.0)




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
    outdoor_by_season = pot.get("_outdoor_by_season")
    if outdoor_by_season is not None:
        return bool(outdoor_by_season.get(_season(day)))
    if _season(day) == "winter":
        return pot["winter_location"] == "outdoor"
    return pot["default_location"] == "outdoor"


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

