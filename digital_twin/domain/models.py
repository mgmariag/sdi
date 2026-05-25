from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class PlantType:
    code: str
    label: str
    water_need_level: str
    moisture_min_pct: Decimal
    moisture_target_pct: Decimal
    moisture_max_pct: Decimal
    winter_moisture_target_pct: Decimal
    heat_sensitive: bool
    allows_second_watering: bool
    notes: str = ""


@dataclass(frozen=True)
class Pot:
    id: int
    pot_code: str
    label: str
    size_class: str
    plant_type_code: str
    default_location: str
    winter_location: str
    balcony_zone: str
    sun_exposure: str
    wind_exposure: str
    drip_flow_ml_min: Decimal
    moisture_min_pct: Decimal
    moisture_target_pct: Decimal
    moisture_max_pct: Decimal
    small_subtype: str | None = None


@dataclass(frozen=True)
class WeatherHourly:
    id: int | None
    location_name: str
    observed_at: datetime
    source: str
    is_forecast: bool
    temperature_c: Decimal | None = None
    relative_humidity_pct: Decimal | None = None
    precipitation_mm: Decimal | None = None
    wind_speed_kmh: Decimal | None = None
    wind_gust_kmh: Decimal | None = None
    cloud_cover_pct: Decimal | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SensorReading:
    pot_id: int
    recorded_at: datetime
    soil_moisture_pct: Decimal
    source: str
    air_temperature_c: Decimal | None = None
    air_humidity_pct: Decimal | None = None
    substrate_temperature_c: Decimal | None = None


@dataclass(frozen=True)
class IrrigationDecision:
    pot_id: int
    decided_at: datetime
    decision_date: date
    decision_slot: str
    should_irrigate: bool
    reason_code: str
    reason_detail: str


@dataclass(frozen=True)
class IrrigationEvent:
    pot_id: int
    scheduled_start_at: datetime
    scheduled_end_at: datetime
    flow_rate_ml_min: Decimal
    planned_volume_ml: Decimal
    status: str = "planned"


@dataclass(frozen=True)
class ExperimentRequest:
    start: date
    end: date
    persist: bool = False


@dataclass(frozen=True)
class SensorSchedule:
    reading_interval_minutes: int
    source: str
