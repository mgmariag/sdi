from __future__ import annotations

from digital_twin.simulation.irrigation_controller.domain_policy import (
    IrrigationDomainPolicy,
)
from digital_twin.simulation.irrigation_controller.fuzzy_policy import (
    FuzzyIrrigationPolicy,
)

DEFAULT_IRRIGATION_POLICY = IrrigationDomainPolicy()
DEFAULT_FUZZY_POLICY = FuzzyIrrigationPolicy()
