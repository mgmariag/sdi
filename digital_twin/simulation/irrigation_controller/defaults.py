from __future__ import annotations

from digital_twin.simulation.irrigation_controller.domain_policy import (
    IrrigationDomainPolicy,
)
from digital_twin.simulation.irrigation_controller.baseline_policy import (
    BaselineIrrigationPolicy,
)
from digital_twin.simulation.irrigation_controller.baseline_step import (
    BaselineIrrigationStep,
)
from digital_twin.simulation.irrigation_controller.baseline_zone_executor import (
    BaselineValveZoneExecutor,
)
from digital_twin.simulation.irrigation_controller.fuzzy_policy import (
    FuzzyIrrigationPolicy,
)
from digital_twin.simulation.irrigation_controller.request_builder import (
    IrrigationRequestBuilder,
)

DEFAULT_IRRIGATION_POLICY = IrrigationDomainPolicy()
DEFAULT_BASELINE_IRRIGATION_POLICY = BaselineIrrigationPolicy(DEFAULT_IRRIGATION_POLICY)
DEFAULT_BASELINE_IRRIGATION_STEP = BaselineIrrigationStep(DEFAULT_BASELINE_IRRIGATION_POLICY)
DEFAULT_FUZZY_POLICY = FuzzyIrrigationPolicy()
DEFAULT_IRRIGATION_REQUEST_BUILDER = IrrigationRequestBuilder()
DEFAULT_BASELINE_VALVE_ZONE_EXECUTOR = BaselineValveZoneExecutor(DEFAULT_IRRIGATION_REQUEST_BUILDER)
