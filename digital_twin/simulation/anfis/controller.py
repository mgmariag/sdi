from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from digital_twin.simulation.anfis.model import ANFIS
from digital_twin.simulation.soil_model import clamp


@dataclass
class AnfisProbabilityCalibrator:
    points: list[tuple[float, float]]

    @classmethod
    def fit(cls, model: ANFIS, dataset: list[dict[str, float | str]], max_bins: int = 8) -> "AnfisProbabilityCalibrator":
        pairs = sorted(
            (
                clamp(model.predict(item), 0.0, 1.0),
                clamp(float(item["target_probability"]), 0.0, 1.0),
            )
            for item in dataset
            if item.get("target_probability") is not None
        )
        if len(pairs) < 30:
            return cls([])

        bin_count = min(max_bins, max(3, len(pairs) // 45))
        bin_size = max(1, math.ceil(len(pairs) / bin_count))
        bins = []
        for index in range(0, len(pairs), bin_size):
            chunk = pairs[index:index + bin_size]
            if not chunk:
                continue
            raw_mean = sum(raw for raw, _ in chunk) / len(chunk)
            target_mean = sum(target for _, target in chunk) / len(chunk)
            bins.append([raw_mean, target_mean, len(chunk)])

        pooled: list[list[float]] = []
        for raw_mean, target_mean, weight in bins:
            pooled.append([raw_mean, target_mean, float(weight)])
            while len(pooled) >= 2 and pooled[-2][1] > pooled[-1][1]:
                right = pooled.pop()
                left = pooled.pop()
                merged_weight = left[2] + right[2]
                pooled.append(
                    [
                        (left[0] * left[2] + right[0] * right[2]) / merged_weight,
                        (left[1] * left[2] + right[1] * right[2]) / merged_weight,
                        merged_weight,
                    ]
                )

        points = [(float(raw), clamp(float(target), 0.0, 1.0)) for raw, target, _ in pooled]
        return cls(points)

    def predict(self, raw_probability: float) -> float:
        raw = clamp(float(raw_probability), 0.0, 1.0)
        if not self.points:
            return raw
        if raw <= self.points[0][0]:
            return self.points[0][1]
        if raw >= self.points[-1][0]:
            return self.points[-1][1]
        for index in range(1, len(self.points)):
            left_raw, left_target = self.points[index - 1]
            right_raw, right_target = self.points[index]
            if raw <= right_raw:
                span = max(right_raw - left_raw, 1e-9)
                ratio = (raw - left_raw) / span
                return clamp(left_target + (right_target - left_target) * ratio, 0.0, 1.0)
        return raw

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.points),
            "bin_count": len(self.points),
            "points": [
                {"raw": round(raw, 4), "calibrated": round(calibrated, 4)}
                for raw, calibrated in self.points
            ],
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "points": [
                {"raw": float(raw), "calibrated": float(calibrated)}
                for raw, calibrated in self.points
            ],
        }

    @classmethod
    def deserialize(cls, payload: dict[str, Any] | None) -> "_AnfisProbabilityCalibrator":
        points = []
        for item in (payload or {}).get("points") or []:
            points.append((float(item["raw"]), float(item["calibrated"])))
        return cls(points)


@dataclass
class AnfisModelController:
    global_model: ANFIS
    global_calibrator: AnfisProbabilityCalibrator
    zone_models: dict[str, ANFIS]
    zone_calibrators: dict[str, AnfisProbabilityCalibrator]

    def predict(self, inputs: dict[str, float], zone: str | None = None) -> float:
        raw = self.raw_predict(inputs, zone)
        return self.calibrator_for_zone(zone).predict(raw)

    def raw_predict(self, inputs: dict[str, float], zone: str | None = None) -> float:
        return self.model_for_zone(zone).predict(inputs)

    def model_for_zone(self, zone: str | None) -> ANFIS:
        return self.zone_models.get(str(zone or ""), self.global_model)

    def calibrator_for_zone(self, zone: str | None) -> AnfisProbabilityCalibrator:
        return self.zone_calibrators.get(str(zone or ""), self.global_calibrator)

    def summary(self) -> dict[str, Any]:
        return {
            "trained_per_valve_zone": bool(self.zone_models),
            "zone_model_count": len(self.zone_models),
            "zone_models": sorted(self.zone_models),
            "global_probability_calibration": self.global_calibrator.summary(),
            "zone_probability_calibration": {
                zone: calibrator.summary()
                for zone, calibrator in sorted(self.zone_calibrators.items())
            },
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "global_model": self.global_model.serialize(),
            "global_calibrator": self.global_calibrator.serialize(),
            "zone_models": {
                zone: model.serialize()
                for zone, model in sorted(self.zone_models.items())
            },
            "zone_calibrators": {
                zone: calibrator.serialize()
                for zone, calibrator in sorted(self.zone_calibrators.items())
            },
        }

    @classmethod
    def deserialize(cls, payload: dict[str, Any]) -> "_AnfisModelController":
        return cls(
            global_model=ANFIS.deserialize(payload["global_model"]),
            global_calibrator=AnfisProbabilityCalibrator.deserialize(payload.get("global_calibrator")),
            zone_models={
                str(zone): ANFIS.deserialize(model_payload)
                for zone, model_payload in (payload.get("zone_models") or {}).items()
            },
            zone_calibrators={
                str(zone): AnfisProbabilityCalibrator.deserialize(calibrator_payload)
                for zone, calibrator_payload in (payload.get("zone_calibrators") or {}).items()
            },
        )


@dataclass
class AnfisTrainingResult:
    model: AnfisModelController
    evaluation: dict[str, Any]
    metadata: dict[str, Any]


def serialize_trained_anfis_model(model: AnfisModelController) -> dict[str, Any]:
    return model.serialize()


def deserialize_trained_anfis_model(payload: dict[str, Any]) -> AnfisModelController:
    return AnfisModelController.deserialize(payload)

