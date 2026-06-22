from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class Pot:
    id: int
    pot_code: str
    plant_type_code: str
    water_need_level: str
    heat_sensitive: bool
    allows_second_watering: bool
    size_class: str
    small_subtype: str | None
    balcony_zone: str
    rain_exposure: str
    sun_exposure: str
    moisture_min_pct: float
    moisture_target_pct: float
    moisture_max_pct: float
    winter_moisture_target_pct: float
    volume_l: float
    retention_factor: float
    drip_flow_ml_min: float
    cycle_soak_enabled: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | "Pot") -> "Pot":
        if isinstance(value, cls):
            return value
        return cls(
            id=int(value.get("id") or 0),
            pot_code=str(value.get("pot_code") or ""),
            plant_type_code=str(value.get("plant_type_code") or ""),
            water_need_level=str(value.get("water_need_level") or "medium"),
            heat_sensitive=bool(value.get("heat_sensitive")),
            allows_second_watering=bool(value.get("allows_second_watering")),
            size_class=str(value.get("size_class") or "medium"),
            small_subtype=value.get("small_subtype"),
            balcony_zone=str(value.get("balcony_zone") or ""),
            rain_exposure=str(value.get("rain_exposure") or ""),
            sun_exposure=str(value.get("sun_exposure") or ""),
            moisture_min_pct=float(value.get("moisture_min_pct") or 0.0),
            moisture_target_pct=float(value.get("moisture_target_pct") or 0.0),
            moisture_max_pct=float(value.get("moisture_max_pct") or 100.0),
            winter_moisture_target_pct=float(value.get("winter_moisture_target_pct") or 15.0),
            volume_l=float(value.get("volume_l") or 0.0),
            retention_factor=float(value.get("retention_factor") or 1.0),
            drip_flow_ml_min=float(value.get("drip_flow_ml_min") or 0.0),
            cycle_soak_enabled=bool(value.get("cycle_soak_enabled")),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pot_code": self.pot_code,
            "plant_type_code": self.plant_type_code,
            "water_need_level": self.water_need_level,
            "heat_sensitive": self.heat_sensitive,
            "allows_second_watering": self.allows_second_watering,
            "size_class": self.size_class,
            "small_subtype": self.small_subtype,
            "balcony_zone": self.balcony_zone,
            "rain_exposure": self.rain_exposure,
            "sun_exposure": self.sun_exposure,
            "moisture_min_pct": self.moisture_min_pct,
            "moisture_target_pct": self.moisture_target_pct,
            "moisture_max_pct": self.moisture_max_pct,
            "winter_moisture_target_pct": self.winter_moisture_target_pct,
            "volume_l": self.volume_l,
            "retention_factor": self.retention_factor,
            "drip_flow_ml_min": self.drip_flow_ml_min,
            "cycle_soak_enabled": self.cycle_soak_enabled,
        }

    def target_moisture_for_slot(self, slot: str) -> float:
        if slot == "winter_check":
            return self.winter_moisture_target_pct
        return self.moisture_target_pct

    def winter_trigger_threshold(self) -> float:
        return max(7.0, min(self.winter_moisture_target_pct - 5.0, 12.0))

    def critical_low_threshold(self, slot: str) -> float:
        if slot == "winter_check":
            return max(7.0, self.winter_moisture_target_pct - 6.0)
        return max(8.0, self.moisture_min_pct - 8.0)

    def is_high_need(self) -> bool:
        return (
            self.water_need_level == "high"
            or self.heat_sensitive
            or self.plant_type_code in {"vegetables", "herbs", "tomatoes", "cucumbers", "flowering"}
        )

    def is_heat_priority(self) -> bool:
        return (
            self.is_high_need()
            or self.size_class == "small"
            or self.small_subtype == "hanging"
            or self.balcony_zone == "hanging_row"
        )

    def allows_second_watering_in_heat(self) -> bool:
        return self.is_heat_priority() or self.allows_second_watering

    def flow_rate_multiplier(self) -> float:
        return 1.0

    def effective_flow_rate_ml_min(self) -> float:
        return max(self.drip_flow_ml_min * self.flow_rate_multiplier(), 1.0)

    def runtime_min_for_volume(self, volume_ml: float) -> float:
        return max(0.0, volume_ml) / self.effective_flow_rate_ml_min()

    def cycle_count_for_runtime(self, runtime_min: float) -> int:
        return 2 if self.cycle_soak_enabled and runtime_min >= 10 else 1

    def soak_pause_min_for_runtime(self, runtime_min: float) -> int:
        return 10 if self.cycle_count_for_runtime(runtime_min) == 2 else 0

    def moisture_gain_for_volume(self, volume_ml: float) -> float:
        return max(0.0, volume_ml) * max(self.retention_factor, 0.1) / max(self.volume_l * 10.0, 1.0)

    def moisture_after_volume(self, current_moisture: float, volume_ml: float) -> float:
        return min(max(float(current_moisture) + self.moisture_gain_for_volume(volume_ml), 0.0), 100.0)

    def volume_for_moisture_deficit(
        self,
        current_moisture: float,
        target_moisture: float,
        max_runtime_min: float,
    ) -> float:
        need_pct = max(0.0, float(target_moisture) - float(current_moisture))
        requested_volume_ml = need_pct * self.volume_l * 10.0 / max(self.retention_factor, 0.1)
        max_volume_ml = self.effective_flow_rate_ml_min() * max(0.0, float(max_runtime_min))
        return min(max(0.0, requested_volume_ml), max_volume_ml)

    def surface_area_m2(self) -> float:
        if self.size_class == "small":
            return {
                "window_box": 0.06,
                "hanging": 0.04,
                "tabletop": 0.025,
            }.get(self.small_subtype, 0.04)
        return {
            "medium": 0.09,
            "large": 0.18,
            "huge": 0.32,
        }[self.size_class]

    def is_outdoor(self, day: date) -> bool:
        _ = day
        return True

    def rain_exposure_factor(self, day: date) -> float:
        if not self.is_outdoor(day):
            return 0.0
        if self.rain_exposure:
            return {
                "covered": 0.0,
                "partially_exposed": 0.5,
                "fully_exposed": 1.0,
            }.get(self.rain_exposure, 0.5)

        if self.balcony_zone in {"north_shelter"}:
            return 0.0
        if self.balcony_zone in {"west_wall", "east_corner"}:
            return 0.5
        if self.balcony_zone in {"south_rail", "hanging_row"}:
            return 1.0
        if self.sun_exposure in {"full", "reflected_heat"}:
            return 1.0
        return 0.5


@dataclass(frozen=True)
class PotSizeClass:
    code: str
    sample_weight: float
    cycle_soak: bool = False


@dataclass(frozen=True)
class PotSizeProfile:
    code: str
    label: str
    size_class: str
    small_subtype: str | None
    diameter_cm: int
    volume_l: float
    base_drip_flow_ml_min: int
    evaporation_factor: float
    retention_factor: float
    small_subtype_weight: float = 0.0

    def reference_row(self) -> tuple:
        return (
            self.code,
            self.label,
            self.small_subtype,
            self.diameter_cm,
            self.volume_l,
            self.base_drip_flow_ml_min,
            self.evaporation_factor,
            self.retention_factor,
        )


class PotSizeCatalog:
    def __init__(
        self,
        size_classes: Iterable[PotSizeClass],
        profiles: Iterable[PotSizeProfile],
    ) -> None:
        self.size_classes = tuple(size_classes)
        self.profiles = tuple(profiles)
        self._size_class_by_code = {item.code: item for item in self.size_classes}
        self._profiles_by_code = {item.code: item for item in self.profiles}

    def size_class_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.size_classes)

    def small_subtypes(self) -> tuple[str, ...]:
        return tuple(profile.small_subtype for profile in self.profiles if profile.small_subtype is not None)

    def reference_rows(self) -> list[tuple]:
        return [profile.reference_row() for profile in self.profiles]

    def get_profile(self, code: str) -> PotSizeProfile:
        return self._profiles_by_code[code]

    def weighted_size_distribution(self) -> list[tuple[str, float]]:
        return [(item.code, item.sample_weight) for item in self.size_classes]

    def weighted_small_subtype_distribution(self) -> list[tuple[str, float]]:
        distribution = []
        for profile in self.profiles:
            if profile.small_subtype is not None and profile.small_subtype_weight > 0:
                distribution.append((profile.small_subtype, profile.small_subtype_weight))
        return distribution

    def profile_code(self, size_class: str, small_subtype: str | None = None) -> str:
        return f"{size_class}_{small_subtype}" if size_class == self.small_code and small_subtype else size_class

    def is_small(self, size_class: str) -> bool:
        return size_class == self.small_code

    def uses_cycle_soak(self, size_class: str) -> bool:
        return self._size_class_by_code[size_class].cycle_soak

    @property
    def small_code(self) -> str:
        return "small"


@dataclass(frozen=True)
class PotExposureRules:
    rain_covered: str = "covered"
    rain_partially_exposed: str = "partially_exposed"
    rain_fully_exposed: str = "fully_exposed"
    sun_shade: str = "shade"
    sun_partial: str = "partial"
    sun_full: str = "full"
    sun_reflected_heat: str = "reflected_heat"
    wind_sheltered: str = "sheltered"
    wind_moderate: str = "moderate"
    wind_gusty: str = "gusty"

    def rain_exposures(self) -> tuple[str, str, str]:
        return self.rain_covered, self.rain_partially_exposed, self.rain_fully_exposed

    def sun_exposures(self) -> tuple[str, str, str, str]:
        return self.sun_shade, self.sun_partial, self.sun_full, self.sun_reflected_heat

    def wind_exposures(self) -> tuple[str, str, str]:
        return self.wind_sheltered, self.wind_moderate, self.wind_gusty

    def weighted_sun_distribution(self) -> list[tuple[str, float]]:
        return [
            (self.sun_full, 0.34),
            (self.sun_partial, 0.32),
            (self.sun_reflected_heat, 0.20),
            (self.sun_shade, 0.14),
        ]

    def weighted_wind_distribution(self) -> list[tuple[str, float]]:
        return [
            (self.wind_moderate, 0.46),
            (self.wind_sheltered, 0.32),
            (self.wind_gusty, 0.22),
        ]

    def rain_exposure_for_zone(self, zone: str) -> str:
        if zone == "north_shelter":
            return self.rain_covered
        if zone in {"west_wall", "east_corner"}:
            return self.rain_partially_exposed
        if zone in {"south_rail", "hanging_row"}:
            return self.rain_fully_exposed
        return self.rain_partially_exposed

    def is_hot_gusty(self, sun_exposure: str, wind_exposure: str) -> bool:
        return sun_exposure == self.sun_reflected_heat and wind_exposure == self.wind_gusty

    def flow_adjustment(self, sun_exposure: str, wind_exposure: str) -> Decimal:
        adjustment = Decimal("0.0")
        if sun_exposure == self.sun_reflected_heat:
            adjustment += Decimal("0.12")
        elif sun_exposure == self.sun_shade:
            adjustment -= Decimal("0.08")

        if wind_exposure == self.wind_gusty:
            adjustment += Decimal("0.10")
        elif wind_exposure == self.wind_sheltered:
            adjustment -= Decimal("0.05")
        return adjustment
