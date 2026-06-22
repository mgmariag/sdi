from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from digital_twin.domain.pot import Pot
from digital_twin.simulation.shared.types import PotState
from digital_twin.domain.soil import DEFAULT_SOIL_MODEL as soil
from digital_twin.simulation.irrigation_controller.request_builder import normalized_dose_factor

PotInput = Mapping[str, Any] | Pot


class IrrigationDomainPolicy:
    """Shared irrigation domain rules that are not owned by one controller."""

    def decision_slot(self, day: date, observed_at: datetime, day_profile: dict[str, Any]) -> str | None:
        hour = observed_at.hour
        max_temp = soil.number(day_profile.get("max_temperature_c"), 20.0)

        if self.dormant_period(day):
            return "winter_check" if hour == 10 else None
        if hour == 6:
            return "morning"
        if max_temp >= 32.0 and hour == 18:
            return "evening"
        return None

    def dormant_period(self, day: date) -> bool:
        return day.month in {12, 1, 2, 3}

    def target_moisture(self, pot: PotInput, day_profile: dict[str, Any], slot: str) -> float:
        domain_pot = Pot.from_mapping(pot)
        if slot == "winter_check":
            return domain_pot.winter_moisture_target_pct

        target = domain_pot.moisture_target_pct
        max_temp = soil.number(day_profile.get("max_temperature_c"), 20.0)
        boost = 0.0
        if 28.0 <= max_temp < 32.0 and (
            domain_pot.is_high_need() or domain_pot.sun_exposure in {"full", "reflected_heat"}
        ):
            boost = 2.0
        elif 32.0 <= max_temp < 35.0:
            boost = 2.0
        elif max_temp >= 35.0 and domain_pot.is_heat_priority():
            boost = 3.0
        if slot == "evening":
            boost = min(boost, 1.5)
        return min(domain_pot.moisture_max_pct - 2.0, target + boost)

    def trigger_threshold(self, pot: PotInput, day_profile: dict[str, Any], slot: str) -> float:
        domain_pot = Pot.from_mapping(pot)
        min_moisture = domain_pot.moisture_min_pct
        target = domain_pot.moisture_target_pct
        max_temp = soil.number(day_profile.get("max_temperature_c"), 20.0)
        avg_temp = soil.number(day_profile.get("avg_temperature_c"), max_temp)

        if slot == "winter_check":
            return domain_pot.winter_trigger_threshold()
        if max_temp < 15.0:
            threshold = max(7.0, min_moisture - 5.0)
        elif avg_temp < 18.0:
            threshold = min_moisture - 1.5
        elif avg_temp < 22.0:
            threshold = min_moisture
        elif max_temp < 28.0:
            threshold = min_moisture + (1.0 if domain_pot.is_high_need() else 0.0)
        elif max_temp < 32.0:
            threshold = min_moisture + 3.0
        elif max_temp < 35.0:
            threshold = min_moisture + 5.0
        else:
            threshold = min_moisture + (7.0 if domain_pot.is_heat_priority() else 5.0)

        if day_profile.get("dry_windy_day") and domain_pot.size_class == "small":
            threshold += 2.0
        if (
            int(day_profile.get("dry_streak_days") or 0) >= 2
            and soil.number(day_profile.get("precipitation_mm"), 0.0) < 0.5
            and max_temp >= 24.0
        ):
            if domain_pot.size_class == "small":
                threshold += 4.0
            elif domain_pot.is_high_need() and domain_pot.sun_exposure in {"full", "reflected_heat"}:
                threshold += 2.0
        if slot == "evening":
            threshold = max(threshold, min_moisture + (4.0 if max_temp >= 35.0 else 2.0))
        return soil.clamp(threshold, 5.0, target - 1.0)

    def temperature_dose_factor(self, day_profile: dict[str, Any]) -> float:
        min_temp = soil.number(day_profile.get("min_temperature_c"), 20.0)
        max_temp = soil.number(day_profile.get("max_temperature_c"), 20.0)
        avg_temp = soil.number(day_profile.get("avg_temperature_c"), max_temp)
        if max_temp <= 15.0 and 5.0 <= min_temp <= 12.0:
            return 0.5
        if avg_temp < 15.0:
            return 0.5
        return 1.0

    def allowed_dose_factor(self, value: float) -> float:
        return normalized_dose_factor(value)

    def rain_policy(self, pot: PotInput, day: date, day_profile: dict[str, Any]) -> dict[str, Any]:
        exposure = Pot.from_mapping(pot).rain_exposure_factor(day)
        rain_mm = soil.number(day_profile.get("precipitation_mm"), 0.0)
        probability = soil.number(day_profile.get("max_precipitation_probability_pct"), 0.0)
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
            soil.number(day_profile.get("precipitation_mm"), 0.0) >= 1.0
            and soil.number(rain_policy.get("rain_probability_pct"), 0.0) >= 40.0
            and soil.number(rain_policy.get("rain_exposure_factor"), 0.0) <= 0.0
        )

    def cadence_days(self, day_profile: dict[str, Any]) -> int:
        avg_temp = soil.number(day_profile.get("avg_temperature_c"), 20.0)
        max_temp = soil.number(day_profile.get("max_temperature_c"), avg_temp)
        rain_mm = soil.number(day_profile.get("precipitation_mm"), 0.0)
        rain_probability = soil.number(day_profile.get("max_precipitation_probability_pct"), 0.0)

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

    def cadence_allows(self, pot: PotInput, day: date, cadence_days: int) -> bool:
        if cadence_days <= 1:
            return True
        return (day.toordinal() + Pot.from_mapping(pot).id) % cadence_days == 0

    def critically_low(self, state: PotState, pot: PotInput, slot: str) -> bool:
        return state.moisture <= Pot.from_mapping(pot).critical_low_threshold(slot)

    def safety_threshold(self, pot: PotInput, day_profile: dict[str, Any], slot: str) -> float:
        domain_pot = Pot.from_mapping(pot)
        if slot == "winter_check":
            return 10.0

        threshold = domain_pot.moisture_min_pct
        if day_profile.get("heatwave_day") and domain_pot.plant_type_code in {"vegetables", "herbs"}:
            threshold += 4.0
        if day_profile.get("dry_windy_day") and domain_pot.size_class == "small":
            threshold += 3.0
        if slot == "evening":
            threshold = max(8.0, threshold - 3.0)
        return threshold

    def has_emergency_dryness(
        self,
        state: PotState,
        pot: PotInput,
        day: date,
        observed_at: datetime,
    ) -> bool:
        domain_pot = Pot.from_mapping(pot)
        if soil.season(day) == "summer" and 11 <= observed_at.hour <= 16:
            return state.moisture < max(8.0, domain_pot.moisture_min_pct - 8.0)
        return False

    def winter_irrigation_allowed(self, state: PotState, day_profile: dict[str, Any]) -> bool:
        return (
            state.moisture < 10.0
            and day_profile.get("min_temperature_next_14_days_c", day_profile["min_temperature_c"]) > 5.0
            and day_profile.get("precipitation_next_14_days_mm", day_profile["precipitation_mm"]) < 1.0
            and day_profile.get("max_precipitation_probability_next_14_days_pct", 0.0) < 40.0
        )

    def second_watering_allowed(self, state: PotState, pot: PotInput, day_profile: dict[str, Any]) -> bool:
        domain_pot = Pot.from_mapping(pot)
        max_temp = soil.number(day_profile.get("max_temperature_c"), 20.0)
        if max_temp < 32.0:
            return False
        eligible = domain_pot.allows_second_watering_in_heat()
        if max_temp >= 35.0:
            return eligible and state.moisture < domain_pot.moisture_target_pct
        return eligible and state.moisture < max(
            domain_pot.moisture_min_pct + 2.0,
            domain_pot.moisture_target_pct - 6.0,
        )

    def high_need_pot(self, pot: PotInput) -> bool:
        return Pot.from_mapping(pot).is_high_need()

    def heat_priority_pot(self, pot: PotInput) -> bool:
        return Pot.from_mapping(pot).is_heat_priority()
