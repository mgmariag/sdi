from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from digital_twin.core.time import now_local
from digital_twin.db.connection import get_connection
from digital_twin.db.schema import get_database_health, get_pot_summary, list_pots
from digital_twin.domain.irrigation_methods import VALVE_ZONE_DESIGN, VALVE_ZONE_ORDER

SAFE_TAP_FLOW_L_MIN = 2.0
VALVE_SWITCH_PAUSE_MIN = 1.0


class SensorRepository:
    """Sensor reading read access."""

    def summary(self, source: str | None = None) -> dict[str, Any]:
        from digital_twin.services.sensor_readings import get_sensor_reading_summary

        return get_sensor_reading_summary(source=source)


class PotRepository:
    """Read access for pot inventory and database health summaries."""

    def health(self) -> dict[str, Any]:
        return get_database_health()

    def summary(self) -> dict[str, Any]:
        return get_pot_summary()

    def list(self, limit: int = 50, offset: int = 0, size_class: str | None = None, plant_type: str | None = None) -> list[dict[str, Any]]:
        return list_pots(limit=limit, offset=offset, size_class=size_class, plant_type=plant_type)

class OverviewRepository:
    """Read model for the dashboard shown before an experiment is selected."""

    def current(self) -> dict[str, Any]:
        now = now_local().replace(tzinfo=None)
        today = now.date()
        with get_connection(row_factory=dict_row) as conn:
            active_pots = int(
                conn.execute("SELECT count(*) AS count FROM pots WHERE active = true").fetchone()["count"]
                or 0
            )
            current_state = self._current_state(conn, now, today)
            sensor_coverage = self._sensor_coverage(conn, now, today, active_pots)
            valve_plan = self._valve_plan(conn, now, current_state.get("next_irrigation_window"))
            plant_overview = self._plant_overview(conn)

        return _json_ready(
            {
                "generated_at": now.isoformat(),
                "state": current_state,
                "sensor_coverage": sensor_coverage,
                "valve_plan": valve_plan,
                "plant_overview": plant_overview,
            }
        )

    def _current_state(self, conn, now: datetime, today) -> dict[str, Any]:
        latest = conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (sensor_id)
                    sensor_id, recorded_at, soil_moisture_pct, source
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
                round(avg(soil_moisture_pct), 2) AS avg_moisture,
                max(recorded_at) AS latest_recorded_at,
                count(*) AS sensor_count
            FROM latest
            """
        ).fetchone()
        fallback = conn.execute(
            "SELECT round(avg(moisture_target_pct), 2) AS avg_target FROM pots WHERE active = true"
        ).fetchone()["avg_target"]
        moisture = _number(latest["avg_moisture"], _number(fallback, 0.0))
        latest_at = latest["latest_recorded_at"]

        rain = conn.execute(
            """
            SELECT
                coalesce(sum(coalesce(precipitation_mm, rain_mm, 0)), 0) AS rain_mm,
                count(*) AS weather_rows
            FROM weather_hourly
            WHERE location_name = 'Cluj-Napoca'
              AND observed_local_at >= %(start_at)s
              AND observed_local_at < %(end_at)s
            """,
            {
                "start_at": datetime.combine(today, time.min),
                "end_at": datetime.combine(today + timedelta(days=3), time.min),
            },
        ).fetchone()
        rain_mm = _number(rain["rain_mm"], 0.0)

        dry = conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (sensor_id)
                    sensor_id, soil_moisture_pct
                FROM sensor_readings
                ORDER BY sensor_id, recorded_at DESC
            )
            SELECT count(*) AS dry_sensors
            FROM latest l
            JOIN pots p ON p.id = l.sensor_id
            WHERE p.active = true
              AND l.soil_moisture_pct < p.moisture_min_pct
            """
        ).fetchone()
        dry_sensors = int(dry["dry_sensors"] or 0)
        recommendation_on = dry_sensors > 0

        confidence = _confidence_score(
            freshness_percent=_freshness_percent(latest_at, now),
            sensor_count=int(latest["sensor_count"] or 0),
            weather_rows=int(rain["weather_rows"] or 0),
        )

        return {
            "current_soil_moisture_pct": round(moisture, 2),
            "forecast_rain_next_3_days_mm": round(rain_mm, 2),
            "forecast_rain_level": _rain_level(rain_mm),
            "irrigation_recommendation": "ON" if recommendation_on else "OFF",
            "dry_sensor_count": dry_sensors,
            "confidence": confidence,
            "next_irrigation_window": _next_irrigation_window(conn, now),
            "latest_sensor_recorded_at": latest_at,
        }

    def _sensor_coverage(self, conn, now: datetime, today, active_pots: int) -> dict[str, Any]:
        sensor_nodes = int(
            conn.execute(
                """
                SELECT count(DISTINCT pot_id) AS count
                FROM sensor_location_recommendations
                """
            ).fetchone()["count"]
            or 0
        )
        if sensor_nodes == 0:
            sensor_nodes = int(
                conn.execute("SELECT count(DISTINCT sensor_id) AS count FROM sensor_readings").fetchone()["count"]
                or 0
            )

        actual_today = int(
            conn.execute(
                """
                SELECT count(DISTINCT sensor_id) AS count
                FROM sensor_readings
                WHERE source = 'actual_sensor'
                  AND recorded_at::date = %(today)s
                """,
                {"today": today},
            ).fetchone()["count"]
            or 0
        )
        measured_pots = min(active_pots, actual_today)
        estimated_pots = max(0, active_pots - measured_pots)

        latest_at = conn.execute("SELECT max(recorded_at) AS latest_at FROM sensor_readings").fetchone()["latest_at"]
        freshness = _freshness_percent(latest_at, now)
        return {
            "total_pots": active_pots,
            "sensor_nodes": sensor_nodes,
            "data_freshness_pct": freshness,
            "segments": [
                {"key": "measured", "label": "Measured pots", "count": measured_pots},
                {"key": "estimated", "label": "Estimated (DT)", "count": estimated_pots},
            ],
        }

    def _plant_overview(self, conn) -> dict[str, Any]:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (sensor_id)
                    sensor_id, soil_moisture_pct
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
                p.plant_type_code,
                pt.label AS plant_type_label,
                count(*)::int AS pot_count,
                round(avg(coalesce(l.soil_moisture_pct, p.moisture_target_pct)), 2) AS avg_moisture_pct
            FROM pots p
            JOIN plant_types pt ON pt.code = p.plant_type_code
            LEFT JOIN latest l ON l.sensor_id = p.id
            WHERE p.active = true
            GROUP BY p.plant_type_code, pt.label
            ORDER BY pot_count DESC, pt.label
            """
        ).fetchall()
        total = sum(int(row["pot_count"]) for row in rows)
        top = rows[:4]
        other = rows[4:]
        items = [
            {
                "key": row["plant_type_code"],
                "label": row["plant_type_label"],
                "count": int(row["pot_count"]),
                "avg_moisture_pct": _number(row["avg_moisture_pct"], 0.0),
            }
            for row in top
        ]
        if other:
            other_count = sum(int(row["pot_count"]) for row in other)
            weighted = sum(_number(row["avg_moisture_pct"], 0.0) * int(row["pot_count"]) for row in other)
            items.append(
                {
                    "key": "other",
                    "label": "Other",
                    "count": other_count,
                    "avg_moisture_pct": round(weighted / max(other_count, 1), 2),
                }
            )
        return {"total_pots": total, "items": items}

    def _valve_plan(self, conn, now: datetime, next_window: dict[str, Any] | None) -> dict[str, Any]:
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
            """
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
                    "immediate_volume_ml": round(immediate_volume_ml, 1),
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
        max_zone_flow_ml_min = max([item["total_flow_ml_min"] for item in priority_order] or [0.0])

        if required_valves == 0:
            recommendation = "No active pots to map"
        elif immediate_starts == 0:
            recommendation = "No immediate run; optimized full-refill plan is ready"
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


def _next_irrigation_window(conn, now: datetime) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT morning_window_start, morning_window_end, evening_window_start, evening_window_end
        FROM pots
        WHERE active = true
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return {"label": "N/A", "start_at": None, "end_at": None}

    candidates = []
    for offset in range(2):
        day = now.date() + timedelta(days=offset)
        for start_key, end_key in (
            ("morning_window_start", "morning_window_end"),
            ("evening_window_start", "evening_window_end"),
        ):
            start_time = row[start_key]
            end_time = row[end_key]
            if start_time and end_time:
                candidates.append((datetime.combine(day, start_time), datetime.combine(day, end_time)))
    future = [(start, end) for start, end in candidates if start > now]
    if not future:
        return {"label": "N/A", "start_at": None, "end_at": None}

    start, end = sorted(future, key=lambda item: item[0])[0]
    return {
        "label": f"{start:%Y-%m-%d %H:%M} - {end:%H:%M}",
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
    }


def _rain_level(rain_mm: float) -> str:
    if rain_mm >= 30.0:
        return "High"
    if rain_mm >= 12.0:
        return "Moderate"
    return "Low"


def _valve_candidate(row: dict[str, Any]) -> dict[str, Any] | None:
    moisture = _number(row.get("soil_moisture_pct"), _number(row.get("moisture_target_pct"), 0.0))
    min_moisture = _number(row.get("moisture_min_pct"), 0.0)
    target = _number(row.get("moisture_target_pct"), min_moisture)
    urgency = max(0.0, min_moisture - moisture)
    requires_run = urgency > 0
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
        "sun_exposure": row["sun_exposure"],
        "plant_type_label": row["plant_type_label"],
        "moisture_pct": round(moisture, 1),
        "moisture_min_pct": round(min_moisture, 1),
        "moisture_target_pct": round(target, 1),
        "planned_volume_ml": planned_volume_ml,
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


def _window_minutes(next_window: dict[str, Any] | None) -> float | None:
    if not next_window or not next_window.get("start_at") or not next_window.get("end_at"):
        return None
    try:
        start = datetime.fromisoformat(str(next_window["start_at"]))
        end = datetime.fromisoformat(str(next_window["end_at"]))
    except ValueError:
        return None
    return max(0.0, (end - start).total_seconds() / 60.0)


def _freshness_percent(latest_at: datetime | None, now: datetime) -> int:
    if latest_at is None:
        return 0
    age_minutes = max(0.0, (now - latest_at).total_seconds() / 60.0)
    if age_minutes <= 30:
        return 98
    if age_minutes <= 120:
        return 95
    if age_minutes <= 24 * 60:
        return 82
    if age_minutes <= 7 * 24 * 60:
        return 68
    return 45


def _confidence_score(freshness_percent: int, sensor_count: int, weather_rows: int) -> float:
    sensor_factor = min(1.0, sensor_count / 4.0)
    weather_factor = min(1.0, weather_rows / 72.0)
    freshness_factor = max(0.0, min(1.0, freshness_percent / 100.0))
    return round(0.2 + 0.45 * freshness_factor + 0.25 * sensor_factor + 0.1 * weather_factor, 2)


def _number(value: Any, default: float) -> float:
    if value is None:
        return default
    return float(value)


def _json_ready(value: Any) -> Any:
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value

class SensorPlacementRepository:
    """Persistence for the current sensor placement recommendation set."""

    def active_pots(self) -> list[dict[str, Any]]:
        with get_connection(row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT
                    p.id,
                    p.pot_code,
                    p.label,
                    p.size_class,
                    p.small_subtype,
                    p.plant_type_code,
                    pt.label AS plant_type_label,
                    pt.water_need_level,
                    pt.heat_sensitive,
                    pt.allows_second_watering,
                    p.default_location,
                    p.winter_location,
                    p.balcony_zone,
                    p.sun_exposure,
                    p.wind_exposure,
                    p.container_material,
                    p.soil_profile,
                    p.drip_flow_ml_min,
                    p.moisture_min_pct,
                    p.moisture_target_pct,
                    p.moisture_max_pct,
                    ps.volume_l,
                    ps.evaporation_factor,
                    ps.retention_factor
                FROM pots p
                JOIN plant_types pt ON pt.code = p.plant_type_code
                JOIN pot_size_profiles ps
                  ON ps.code = CASE
                        WHEN p.size_class = 'small' THEN 'small_' || p.small_subtype
                        ELSE p.size_class
                     END
                WHERE p.active = true
                ORDER BY p.id
                """
            ).fetchall()
            return [_json_ready(row) for row in rows]

    def current(self) -> dict[str, Any]:
        with get_connection(row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT
                    r.id,
                    r.requested_sensor_count,
                    r.rank,
                    r.pot_id,
                    r.score,
                    r.reason,
                    r.criteria,
                    r.created_at,
                    p.pot_code,
                    p.label AS pot_label,
                    p.size_class,
                    p.small_subtype,
                    p.plant_type_code,
                    pt.label AS plant_type_label,
                    p.default_location,
                    p.winter_location,
                    p.balcony_zone,
                    p.sun_exposure,
                    p.wind_exposure,
                    p.container_material,
                    p.drip_flow_ml_min,
                    p.moisture_target_pct
                FROM sensor_location_recommendations r
                JOIN pots p ON p.id = r.pot_id
                JOIN plant_types pt ON pt.code = p.plant_type_code
                ORDER BY r.rank
                """
            ).fetchall()
            counts = conn.execute(
                """
                SELECT
                    (SELECT count(*) FROM pots WHERE active = true) AS active_pot_count,
                    (
                        SELECT count(DISTINCT sr.sensor_id)
                        FROM sensor_readings sr
                        JOIN pots p ON p.id = sr.sensor_id
                        WHERE p.active = true
                    ) AS sensor_reading_pot_count
                """
            ).fetchone()
        items = [_json_ready(row) for row in rows]
        stored_sensor_count = items[0]["requested_sensor_count"] if items else 0
        sensor_reading_pot_count = int(counts["sensor_reading_pot_count"] or 0)
        return {
            "sensor_count": stored_sensor_count or sensor_reading_pot_count,
            "stored_sensor_count": stored_sensor_count,
            "sensor_reading_pot_count": sensor_reading_pot_count,
            "items": items,
            "updated_at": items[0]["created_at"] if items else None,
            "active_pot_count": int(counts["active_pot_count"] or 0),
        }

    def replace(self, requested_sensor_count: int, recommendations: list[dict[str, Any]]) -> dict[str, Any]:
        with get_connection() as conn:
            conn.execute("DELETE FROM sensor_location_recommendations")
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO sensor_location_recommendations (
                        sensor_id, requested_sensor_count, rank, pot_id, score, reason, criteria
                    )
                    VALUES (
                        %(sensor_id)s, %(requested_sensor_count)s, %(rank)s, %(pot_id)s, %(score)s, %(reason)s, %(criteria)s
                    )
                    """,
                    [
                        {
                            "sensor_id": item.get("sensor_id") or item["rank"],
                            "requested_sensor_count": requested_sensor_count,
                            "rank": item["rank"],
                            "pot_id": item["pot_id"],
                            "score": item["score"],
                            "reason": item["reason"],
                            "criteria": Jsonb(item["criteria"]),
                        }
                        for item in recommendations
                    ],
                )
            conn.commit()
        return self.current()

    def selected_pot_ids(self, candidate_pot_ids: list[int] | None = None) -> list[int]:
        filters = []
        params: dict[str, Any] = {}
        if candidate_pot_ids:
            filters.append("r.pot_id = ANY(%(candidate_pot_ids)s)")
            params["candidate_pot_ids"] = candidate_pot_ids
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        with get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT r.pot_id
                FROM sensor_location_recommendations r
                JOIN pots p ON p.id = r.pot_id
                {where_clause}
                ORDER BY r.rank
                """,
                params,
            ).fetchall()
        return [int(row[0]) for row in rows]


def _json_ready(value):
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
