from __future__ import annotations

from typing import Any

from digital_twin.domain.pot import Pot
from digital_twin.domain.weather import local_observed_at
from digital_twin.simulation.irrigation_controller.domain_policy import (
    IrrigationDomainPolicy,
    PotInput,
)
from digital_twin.simulation.shared.types import PotState


class BaselineIrrigationPolicy:
    """Default threshold-based irrigation decision policy."""

    def __init__(self, domain_policy: IrrigationDomainPolicy | None = None) -> None:
        self.domain_policy = domain_policy or IrrigationDomainPolicy()

    def make_decision(
        self,
        state: PotState,
        pot: PotInput,
        weather: dict[str, Any],
        day_profile: dict[str, Any],
        slot: str,
    ) -> dict[str, Any]:
        domain_pot = Pot.from_mapping(pot)
        observed_local = local_observed_at(weather)
        target = self.domain_policy.target_moisture(domain_pot, day_profile, slot)
        threshold = self.domain_policy.trigger_threshold(domain_pot, day_profile, slot)
        critical_low = self.domain_policy.critically_low(state, domain_pot, slot)
        rain_policy = self.domain_policy.rain_policy(domain_pot, observed_local.date(), day_profile)
        cadence_days = self.domain_policy.cadence_days(day_profile)
        cadence_ok = self.domain_policy.cadence_allows(domain_pot, observed_local.date(), cadence_days)
        dose_factor = min(self.domain_policy.temperature_dose_factor(day_profile), rain_policy["dose_factor"])
        reason_code = "moisture_ok"
        reason_detail = "Moisture is above the baseline decision threshold."
        should_irrigate = False

        if day_profile["freeze_risk"]:
            reason_code = "freeze_risk"
            reason_detail = "Skipped because freezing temperatures are present or forecast."
        elif slot == "winter_check" and not self.domain_policy.winter_irrigation_allowed(state, day_profile):
            reason_code = "winter_conditions_not_met"
            reason_detail = "Winter watering requires very dry soil, 14 days above 5C, and no meaningful precipitation."
        elif slot == "evening" and not self.domain_policy.second_watering_allowed(state, domain_pot, day_profile):
            reason_code = "second_watering_not_needed"
            reason_detail = "Evening baseline watering is reserved for hot/extreme days and eligible or still-low pots."
        elif rain_policy["skip"] and not critical_low:
            reason_code = "rain_sufficient"
            reason_detail = (
                f"Skipped because effective rain is {rain_policy['effective_rain_mm']:.1f} mm "
                f"at {rain_policy['rain_probability_pct']:.0f}% probability."
            )
        elif self.domain_policy.is_covered_rain_day(rain_policy, day_profile) and state.moisture < target:
            should_irrigate = True
            reason_code = "covered_rain_day"
            reason_detail = (
                f"Rain is present but this pot is not exposed; moisture {state.moisture:.1f}% "
                f"is below target {target:.1f}%, so baseline waters 25% less."
            )
        elif state.moisture < threshold or critical_low:
            should_irrigate = True
            if rain_policy["skip"] and critical_low:
                dose_factor = min(self.domain_policy.temperature_dose_factor(day_profile), 0.5)
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

        dose_factor = self.domain_policy.allowed_dose_factor(dose_factor)
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
            "threshold_pct": round(threshold, 2),
            "target_moisture_pct": round(target, 2),
            "weather_hourly_id": weather["id"],
            "dose_factor": round(dose_factor, 3),
            "effective_rain_mm": round(rain_policy["effective_rain_mm"], 2),
            "rain_exposure_factor": round(rain_policy["rain_exposure_factor"], 2),
            "rain_reduction_pct": round((1.0 - rain_policy["dose_factor"]) * 100.0, 1),
            "baseline_cadence_days": cadence_days,
        }
