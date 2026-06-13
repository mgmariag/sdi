from __future__ import annotations

from datetime import date, datetime
from typing import Any

from digital_twin.simulation.irrigation_controller.defaults import (
    DEFAULT_FUZZY_POLICY,
    DEFAULT_IRRIGATION_POLICY,
)
from digital_twin.simulation.shared.constants import HOURLY_CHART_MAX_RANGE_DAYS
from digital_twin.simulation.soil_model import (
    local_observed_at,
    number,
)


def uses_hourly_chart(start_date: date, end_date: date) -> bool:
    return (end_date - start_date).days < HOURLY_CHART_MAX_RANGE_DAYS


def add_chart_summary(
    summary: dict[str, Any],
    chart_entries: list[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> None:
    summary["chartGranularity"] = "hourly" if uses_hourly_chart(start_date, end_date) and chart_entries else "daily"
    summary["chartEntryCount"] = len(chart_entries)


def new_daily_moisture_tracker() -> dict[str, Any]:
    return {"snapshots": []}


def daily_moisture_snapshot_label(day: date, observed_at: datetime, day_profile: dict[str, Any]) -> str | None:
    slot = DEFAULT_IRRIGATION_POLICY.decision_slot(day, observed_at, day_profile)
    if slot:
        return f"before_{slot}"

    hour = observed_at.hour
    max_temp = number(day_profile.get("max_temperature_c"), 20.0)
    if day.month in {12, 1, 2, 3} and hour == 13:
        return "after_winter_check"
    if hour == 9:
        return "after_morning"
    if max_temp >= 32.0 and hour == 21:
        return "after_evening"
    return None


def post_irrigation_snapshot_index(
    day: date,
    weather_rows: list[dict[str, Any]],
    start_index: int,
    day_profile: dict[str, Any],
) -> tuple[int | None, str | None]:
    for index in range(max(0, start_index), len(weather_rows)):
        observed_local = local_observed_at(weather_rows[index])
        if observed_local.date() != day:
            continue
        label = daily_moisture_snapshot_label(day, observed_local, day_profile)
        if label and label.startswith("after_"):
            return index, label
    return None, None


def record_daily_moisture_snapshot(
    tracker: dict[str, Any],
    pot_states: dict[int, Any],
    label: str,
) -> None:
    moistures = [state.moisture for state in pot_states.values()]
    if not moistures:
        return
    tracker.setdefault("snapshots", []).append(
        {
            "label": label,
            "average_moisture": sum(moistures) / len(moistures),
            "min_moisture": min(moistures),
            "max_moisture": max(moistures),
        }
    )


def daily_moisture_summary(
    tracker: dict[str, Any],
    pot_states: dict[int, Any],
) -> dict[str, Any]:
    snapshots = tracker.get("snapshots") or []
    moistures = [state.moisture for state in pot_states.values()]
    avg_moisture = sum(moistures) / max(len(moistures), 1)
    end_of_day_summary = {
        "moisture": round(avg_moisture, 2),
        "average_moisture": round(avg_moisture, 2),
        "min_moisture": round(min(moistures), 2),
        "max_moisture": round(max(moistures), 2),
    }
    if not snapshots:
        return {
            **end_of_day_summary,
            "moisture_sample_count": 0,
            "moisture_sample_method": "end_of_day",
        }

    post_window_snapshots = [
        item for item in snapshots
        if str(item.get("label") or "").startswith("after_")
    ]
    pre_window_snapshots = [
        item for item in snapshots
        if str(item.get("label") or "").startswith("before_")
    ]
    pre_window_moisture = (
        sum(float(item["average_moisture"]) for item in pre_window_snapshots) / len(pre_window_snapshots)
        if pre_window_snapshots
        else None
    )
    post_window_moisture = (
        sum(float(item["average_moisture"]) for item in post_window_snapshots) / len(post_window_snapshots)
        if post_window_snapshots
        else None
    )
    return {
        **end_of_day_summary,
        "moisture_sample_count": len(snapshots),
        "moisture_sample_method": "end_of_day",
        "moisture_sample_labels": [str(item["label"]) for item in snapshots],
        "pre_irrigation_moisture": round(pre_window_moisture, 2) if pre_window_moisture is not None else None,
        "post_irrigation_moisture": round(post_window_moisture, 2) if post_window_moisture is not None else None,
    }


def sampling_moisture_chart_summary(
    rows: list[dict[str, Any]],
    sample_interval_hours: int,
    hourly: bool,
) -> dict[str, int]:
    if not rows:
        return {"sample_count": 0, "sample_interval_rows": 0}

    sample_interval_rows = max(1, sample_interval_hours if hourly else round(sample_interval_hours / 24))
    sample_count = 0
    has_sample_flags = any("sparse_sensor_sample" in row for row in rows)

    for index, row in enumerate(rows):
        raw_sparse = number(row.get("sparse_moisture"), None)
        if raw_sparse is not None:
            row["sparse_moisture_raw"] = round(raw_sparse, 2)

        sample_now = bool(row.get("sparse_sensor_sample")) if has_sample_flags else index % sample_interval_rows == 0
        if sample_now:
            sample_count += 1

    return {"sample_count": sample_count, "sample_interval_rows": sample_interval_rows}


def _default_comfort_threshold_pct(pots: list[dict[str, Any]]) -> float:
    targets = [
        float(pot["moisture_target_pct"])
        for pot in pots
        if pot.get("moisture_target_pct") is not None
    ]
    return round(sum(targets) / max(len(targets), 1), 2) if targets else 0.0


def comfort_threshold_pct(pots: list[dict[str, Any]]) -> float:
    return _default_comfort_threshold_pct(pots)


def fuzzy_comfort_threshold_pct(pots: list[dict[str, Any]]) -> float:
    day_profile = {
        "avg_temperature_c": 22.0,
        "max_temperature_c": 22.0,
        "precipitation_mm": 0.0,
        "heatwave_day": False,
        "dry_windy_day": False,
    }
    floors = [
        DEFAULT_FUZZY_POLICY.comfort_floor(pot, day_profile, "morning")
        for pot in pots
        if pot.get("moisture_target_pct") is not None and pot.get("moisture_min_pct") is not None
    ]
    return round(sum(floors) / max(len(floors), 1), 2) if floors else 0.0


def moisture_safe_savings_metrics(
    entries: list[dict[str, Any]],
    prefix: str,
    pots: list[dict[str, Any]],
    water_savings_percent: float,
    comfort_threshold_pct: float | None = None,
) -> dict[str, Any]:
    threshold = (
        _default_comfort_threshold_pct(pots)
        if comfort_threshold_pct is None
        else float(comfort_threshold_pct)
    )
    tolerance_pct = 2.0
    safe_days = 0
    deficits = []
    for entry in entries:
        baseline_moisture = float(entry.get("baseline_moisture") or 0.0)
        comparison_moisture = float(entry.get(f"{prefix}_moisture") or 0.0)
        if comparison_moisture >= threshold or comparison_moisture >= baseline_moisture - tolerance_pct:
            safe_days += 1
        deficits.append(max(0.0, threshold - comparison_moisture))

    total_days = max(len(entries), 1)
    comfort_preserved = safe_days / total_days * 100.0
    moisture_safe_savings = float(water_savings_percent) * comfort_preserved / 100.0
    return {
        "comfort_threshold_pct": threshold,
        "comfort_preserved_days": safe_days,
        "comfort_preserved_percent": round(comfort_preserved, 2),
        "comfort_tolerance_pct": tolerance_pct,
        "average_comfort_deficit_pct": round(sum(deficits) / total_days, 2),
        "moisture_safe_savings_percent": round(moisture_safe_savings, 2),
    }


def new_sampling_estimation_stats() -> dict[str, float | int]:
    return {
        "estimation_points": 0,
        "error_sum": 0.0,
        "absolute_error_sum": 0.0,
        "max_absolute_error": 0.0,
        "sensor_refreshes": 0,
        "direct_refreshes": 0,
        "associated_refreshes": 0,
        "forecast_refreshes": 0,
        "missing_refreshes": 0,
        "association_distance_sum": 0.0,
    }


def record_sampling_estimation_error(
    stats: dict[str, float | int],
    controller_state: Any,
    actual_state: Any,
) -> None:
    error = controller_state.moisture - actual_state.moisture
    absolute_error = abs(error)
    stats["estimation_points"] += 1
    stats["error_sum"] += error
    stats["absolute_error_sum"] += absolute_error
    stats["max_absolute_error"] = max(float(stats["max_absolute_error"]), absolute_error)


def sampling_estimation_summary(stats: dict[str, float | int]) -> dict[str, Any]:
    points = int(stats["estimation_points"])
    associated_refreshes = int(stats["associated_refreshes"])
    return {
        "sampling_moisture_mae_pct": round(float(stats["absolute_error_sum"]) / max(points, 1), 2),
        "sampling_moisture_bias_pct": round(float(stats["error_sum"]) / max(points, 1), 2),
        "sampling_moisture_max_error_pct": round(float(stats["max_absolute_error"]), 2),
        "sampling_estimation_points": points,
        "sampling_sensor_refreshes": int(stats["sensor_refreshes"]),
        "sampling_direct_refreshes": int(stats["direct_refreshes"]),
        "sampling_associated_refreshes": associated_refreshes,
        "sampling_forecast_refreshes": int(stats["forecast_refreshes"]),
        "sampling_missing_refreshes": int(stats["missing_refreshes"]),
        "sampling_average_association_distance": round(
            float(stats["association_distance_sum"]) / max(associated_refreshes, 1),
            2,
        ),
    }
