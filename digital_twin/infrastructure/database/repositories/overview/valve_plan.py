from __future__ import annotations

from datetime import datetime
from typing import Any

from digital_twin.domain.valves import VALVE_ZONE_DESIGN, VALVE_ZONE_ORDER
from digital_twin.infrastructure.database.repositories.overview._common import (
    SAFE_TAP_FLOW_L_MIN,
    VALVE_SWITCH_PAUSE_MIN,
    number as _number,
    window_minutes as _window_minutes,
)


def valve_plan(conn, now: datetime, next_window: dict[str, Any] | None) -> dict[str, Any]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (sensor_id)
                sensor_id, recorded_at, soil_moisture_pct
            FROM sensor_readings
            ORDER BY
                sensor_id,
                recorded_at DESC,
                CASE reading_resolution
                    WHEN 'raw_15min' THEN 1
                    WHEN 'hourly' THEN 2
                    ELSE 3
                END
        )
        SELECT
            p.id,
            p.pot_code,
            p.label,
            p.balcony_zone,
            p.rain_exposure,
            p.sun_exposure,
            p.size_class,
            p.small_subtype,
            p.drip_flow_ml_min,
            p.cycle_soak_enabled,
            p.moisture_min_pct,
            p.moisture_target_pct,
            pt.label AS plant_type_label,
            pt.water_need_level,
            pt.heat_sensitive,
            ps.volume_l,
            ps.retention_factor,
            l.soil_moisture_pct,
            l.recorded_at
        FROM pots p
        JOIN plant_types pt ON pt.code = p.plant_type_code
        JOIN pot_size_profiles ps
          ON ps.code = CASE
                WHEN p.size_class = 'small' THEN 'small_' || p.small_subtype
                ELSE p.size_class
             END
        LEFT JOIN latest l ON l.sensor_id = p.id
        WHERE p.active = true
        ORDER BY p.balcony_zone, p.id
        """,
        {"now": now},
    ).fetchall()

    candidates = [_valve_candidate(row) for row in rows]
    candidates = [candidate for candidate in candidates if candidate is not None]
    zones: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        zones.setdefault(candidate["zone"], []).append(candidate)

    priority_order = []
    zone_design = _zone_design_for(zones)
    for design in zone_design:
        zone = design["zone"]
        zone_candidates = zones[zone]
        ordered = sorted(zone_candidates, key=lambda item: (-item["priority_score"], item["pot_code"]))
        total_flow_ml_min = sum(item["flow_rate_ml_min"] for item in ordered)
        planned_volume_ml = sum(item["planned_volume_ml"] for item in ordered)
        design_volume_ml = sum(item["design_volume_ml"] for item in ordered)
        immediate_volume_ml = sum(item["planned_volume_ml"] for item in ordered if item["requires_run"])
        estimated_run_minutes = planned_volume_ml / max(total_flow_ml_min, 1.0)
        immediate_run_minutes = immediate_volume_ml / max(total_flow_ml_min, 1.0)
        immediate_pots = sum(1 for item in ordered if item["requires_run"])
        top_pots = [item for item in ordered if item["requires_run"]] or ordered
        priority_order.append(
            {
                "rank": 0,
                "valve_number": design["valve_number"],
                "zone": zone,
                "affected_pots": len(ordered),
                "immediate_pots": immediate_pots,
                "requires_run": immediate_pots > 0,
                "total_flow_ml_min": round(total_flow_ml_min, 2),
                "total_flow_l_min": round(total_flow_ml_min / 1000.0, 3),
                "estimated_run_minutes": round(estimated_run_minutes, 1),
                "immediate_run_minutes": round(immediate_run_minutes, 1),
                "planned_volume_ml": round(planned_volume_ml, 1),
                "planned_volume_l": round(planned_volume_ml / 1000.0, 2),
                "design_volume_ml": round(design_volume_ml, 1),
                "design_volume_l": round(design_volume_ml / 1000.0, 2),
                "immediate_volume_ml": round(immediate_volume_ml, 1),
                "immediate_volume_l": round(immediate_volume_ml / 1000.0, 2),
                "priority_score": round(max(item["priority_score"] for item in ordered), 2),
                "top_pots": top_pots[:3],
            }
        )

    priority_order.sort(
        key=lambda item: (
            0 if item["requires_run"] else 1,
            -item["priority_score"],
            item["valve_number"],
        )
    )
    for index, item in enumerate(priority_order, start=1):
        item["rank"] = index

    switch_pause_minutes = VALVE_SWITCH_PAUSE_MIN if len(priority_order) > 1 else 0.0
    full_refill_runtime = sum(item["estimated_run_minutes"] for item in priority_order)
    full_refill_runtime += max(0, len(priority_order) - 1) * switch_pause_minutes
    immediate_order = [item for item in priority_order if item["requires_run"]]
    immediate_runtime = sum(item["immediate_run_minutes"] for item in immediate_order)
    immediate_runtime += max(0, len(immediate_order) - 1) * switch_pause_minutes
    window_minutes = _window_minutes(next_window)
    full_schedule = _fit_valve_schedule(
        priority_order,
        duration_key="estimated_run_minutes",
        switch_pause_min=switch_pause_minutes,
        window_minutes=window_minutes,
    )
    immediate_schedule = _fit_valve_schedule(
        immediate_order,
        duration_key="immediate_run_minutes",
        switch_pause_min=switch_pause_minutes,
        window_minutes=window_minutes,
    )
    required_valves = len(priority_order)
    affected_pots = sum(item["affected_pots"] for item in priority_order)
    immediate_starts = sum(1 for item in priority_order if item["requires_run"])
    immediate_pots = sum(item["immediate_pots"] for item in priority_order)
    total_flow_ml_min = sum(item["total_flow_ml_min"] for item in priority_order)
    complete_irrigation_volume_ml = sum(item["design_volume_ml"] for item in priority_order)
    immediate_irrigation_volume_ml = sum(item["immediate_volume_ml"] for item in priority_order)
    max_zone_flow_ml_min = max([item["total_flow_ml_min"] for item in priority_order] or [0.0])

    if required_valves == 0:
        recommendation = "No active pots to map"
    elif immediate_starts == 0:
        recommendation = "No immediate run, optimized full-refill plan is ready"
    elif not immediate_schedule["fits_window"]:
        recommendation = "Split sequence across watering windows"
    elif immediate_schedule["max_parallel_valves"] > 1:
        recommendation = "Run optimized parallel batches by priority"
    else:
        recommendation = "Run priority valve sequence"

    return {
        "required_valves": required_valves,
        "valve_starts": immediate_starts,
        "immediate_pots": immediate_pots,
        "affected_pots": affected_pots,
        "run_mode": "sequential",
        "total_runtime_min": round(immediate_runtime, 1),
        "full_refill_runtime_min": round(full_refill_runtime, 1),
        "design_runtime_min": round(full_refill_runtime, 1),
        "complete_irrigation_volume_ml": round(complete_irrigation_volume_ml, 1),
        "complete_irrigation_volume_l": round(complete_irrigation_volume_ml / 1000.0, 2),
        "full_refill_volume_ml": round(complete_irrigation_volume_ml, 1),
        "full_refill_volume_l": round(complete_irrigation_volume_ml / 1000.0, 2),
        "immediate_irrigation_volume_ml": round(immediate_irrigation_volume_ml, 1),
        "immediate_irrigation_volume_l": round(immediate_irrigation_volume_ml / 1000.0, 2),
        "optimized_runtime_min": full_schedule["runtime_min"],
        "immediate_optimized_runtime_min": immediate_schedule["runtime_min"],
        "total_flow_ml_min": round(total_flow_ml_min, 2),
        "total_flow_l_min": round(total_flow_ml_min / 1000.0, 3),
        "max_zone_flow_ml_min": round(max_zone_flow_ml_min, 2),
        "max_zone_flow_l_min": round(max_zone_flow_ml_min / 1000.0, 3),
        "max_parallel_valves": full_schedule["max_parallel_valves"],
        "max_parallel_flow_l_min": full_schedule["max_parallel_flow_l_min"],
        "safe_tap_flow_l_min": SAFE_TAP_FLOW_L_MIN,
        "switch_pause_min": switch_pause_minutes,
        "next_window_minutes": round(window_minutes, 1) if window_minutes is not None else None,
        "fits_next_window": bool(
            (immediate_starts == 0 and full_schedule["fits_window"])
            or (immediate_starts > 0 and immediate_schedule["fits_window"])
        ),
        "recommendation": recommendation,
        "priority_order": priority_order[:5],
        "optimized_schedule": full_schedule["batches"],
        "immediate_schedule": immediate_schedule["batches"],
        "generated_at": now.isoformat(),
    }


def _valve_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    moisture = _number(row.get("soil_moisture_pct"), _number(row.get("moisture_target_pct"), 0.0))
    min_moisture = _number(row.get("moisture_min_pct"), 0.0)
    target = _number(row.get("moisture_target_pct"), min_moisture)
    urgency = max(0.0, min_moisture - moisture)
    requires_run = urgency > 0
    design_volume_ml = _planned_valve_volume_ml(row, min_moisture, target)
    planned_volume_ml = _planned_valve_volume_ml(row, moisture if requires_run else min_moisture, target)
    flow_rate = max(_number(row.get("drip_flow_ml_min"), 1.0), 1.0)
    run_minutes = planned_volume_ml / flow_rate if flow_rate > 0 else 0.0
    if bool(row.get("cycle_soak_enabled")) and run_minutes >= 10:
        run_minutes += 10

    deficit_to_target = max(0.0, target - moisture)
    margin_to_min = max(0.0, moisture - min_moisture)
    readiness_score = max(0.0, 18.0 - margin_to_min)
    water_need_bonus = {"high": 4.0, "medium": 2.0, "low": 0.0}.get(str(row.get("water_need_level") or "medium"), 2.0)
    sun_bonus = {"reflected_heat": 6.0, "full": 4.0, "partial": 1.5, "shade": 0.0}.get(str(row.get("sun_exposure") or "partial"), 1.5)
    heat_bonus = 2.0 if row.get("heat_sensitive") else 0.0
    priority_score = urgency * 4.0 + deficit_to_target + readiness_score + sun_bonus + water_need_bonus + heat_bonus
    return {
        "pot_id": int(row["id"]),
        "pot_code": row["pot_code"],
        "label": row["label"],
        "zone": row["balcony_zone"],
        "rain_exposure": row.get("rain_exposure") or "partially_exposed",
        "sun_exposure": row["sun_exposure"],
        "plant_type_label": row["plant_type_label"],
        "moisture_pct": round(moisture, 1),
        "moisture_min_pct": round(min_moisture, 1),
        "moisture_target_pct": round(target, 1),
        "planned_volume_ml": planned_volume_ml,
        "design_volume_ml": design_volume_ml,
        "flow_rate_ml_min": round(flow_rate, 2),
        "run_minutes": round(run_minutes, 1),
        "priority_score": round(priority_score, 2),
        "requires_run": requires_run,
    }


def _zone_design_for(zones: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    design = [item for item in VALVE_ZONE_DESIGN if item["zone"] in zones]
    unknown_zones = sorted(zone for zone in zones if zone not in VALVE_ZONE_ORDER)
    next_valve = len(VALVE_ZONE_DESIGN) + 1
    design.extend(
        {"valve_number": next_valve + index, "zone": zone}
        for index, zone in enumerate(unknown_zones)
    )
    return design


def _fit_valve_schedule(
    jobs: list[dict[str, Any]],
    duration_key: str,
    switch_pause_min: float,
    window_minutes: float | None,
) -> dict[str, Any]:
    if not jobs:
        return {
            "batches": [],
            "runtime_min": 0.0,
            "fits_window": True,
            "max_parallel_valves": 0,
            "max_parallel_flow_l_min": 0.0,
            "flow_limit_l_min": SAFE_TAP_FLOW_L_MIN,
        }

    total_flow_l_min = sum(float(job["total_flow_l_min"]) for job in jobs)
    flow_limit = min(max(SAFE_TAP_FLOW_L_MIN, max(float(job["total_flow_l_min"]) for job in jobs)), total_flow_l_min)
    best = _build_valve_batches(jobs, duration_key, switch_pause_min, flow_limit)
    if window_minutes is None or best["runtime_min"] <= window_minutes:
        best["fits_window"] = True
        return best

    while flow_limit < total_flow_l_min and best["runtime_min"] > window_minutes:
        flow_limit = min(total_flow_l_min, flow_limit + 0.25)
        candidate = _build_valve_batches(jobs, duration_key, switch_pause_min, flow_limit)
        if candidate["runtime_min"] <= best["runtime_min"]:
            best = candidate

    best["fits_window"] = bool(window_minutes is None or best["runtime_min"] <= window_minutes)
    return best


def _build_valve_batches(
    jobs: list[dict[str, Any]],
    duration_key: str,
    switch_pause_min: float,
    flow_limit_l_min: float,
) -> dict[str, Any]:
    raw_batches: list[list[dict[str, Any]]] = []
    current_batch: list[dict[str, Any]] = []
    current_flow = 0.0
    for job in jobs:
        job_flow = float(job["total_flow_l_min"])
        if current_batch and current_flow + job_flow > flow_limit_l_min:
            raw_batches.append(current_batch)
            current_batch = []
            current_flow = 0.0
        current_batch.append(job)
        current_flow += job_flow
    if current_batch:
        raw_batches.append(current_batch)

    cursor = 0.0
    batches = []
    for index, batch in enumerate(raw_batches, start=1):
        duration = max(float(item.get(duration_key, 0.0)) for item in batch)
        flow = sum(float(item["total_flow_l_min"]) for item in batch)
        start = cursor
        end = start + duration
        batches.append(
            {
                "batch": index,
                "start_min": round(start, 1),
                "end_min": round(end, 1),
                "duration_min": round(duration, 1),
                "flow_l_min": round(flow, 3),
                "valves": [
                    {
                        "valve_number": item["valve_number"],
                        "zone": item["zone"],
                        "priority_rank": item["rank"],
                        "duration_min": round(float(item.get(duration_key, 0.0)), 1),
                    }
                    for item in batch
                ],
            }
        )
        cursor = end + switch_pause_min

    runtime = batches[-1]["end_min"] if batches else 0.0
    return {
        "batches": batches,
        "runtime_min": round(runtime, 1),
        "fits_window": False,
        "max_parallel_valves": max((len(batch["valves"]) for batch in batches), default=0),
        "max_parallel_flow_l_min": round(max((batch["flow_l_min"] for batch in batches), default=0.0), 3),
        "flow_limit_l_min": round(flow_limit_l_min, 3),
    }


def _planned_valve_volume_ml(row: dict[str, Any], moisture: float, target: float) -> float:
    need_pct = max(0.0, target - moisture)
    volume_l = _number(row.get("volume_l"), 1.0)
    retention = max(_number(row.get("retention_factor"), 1.0), 0.1)
    flow_rate = max(_number(row.get("drip_flow_ml_min"), 1.0), 1.0)
    planned_volume_ml = max(0.0, need_pct * volume_l * 10.0 / retention)
    max_minutes = {"huge": 90, "large": 60, "medium": 35, "small": 20}.get(str(row.get("size_class")), 35)
    return round(min(planned_volume_ml, flow_rate * max_minutes), 2)
