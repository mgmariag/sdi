# Implementation Review

## Supported claims

- The project implements a simulated digital twin for irrigation control with closed-loop soil-moisture feedback.
- Soil moisture, temperature, humidity, and rain prediction are represented as twin inputs.
- Irrigation is controlled through a threshold-based decision rule, with optional hysteresis.
- Experiments compare baseline threshold control with sparse sampling.
- An ANFIS-GA style controller is implemented as a prototype and trained on fuzzy low/medium/high irrigation requirement classes.
- Baseline, sampling, and ANFIS experiments can use the same reproducible synthetic weather scenario through a shared scenario seed.
- Experiment outputs are cached by parameter set so repeated requests can reuse previous results instead of recomputing.
- The SAP UI5 frontend can run baseline, sampling, and ANFIS experiments through the FastAPI backend.

## Claims that need careful wording

- The current implementation uses synthetic sensor and weather data. It should not be described as a deployed IoT system unless real sensor ingestion is added.
- Rain prediction is currently random, not a live meteorological API or trained weather model.
- Water savings are simulated estimates based on irrigation steps and configured flow rate. They are not field-measured consumption values.
- The ANFIS-GA model is a lightweight local approximation trained on generated fuzzy irrigation labels, not a validated agronomic decision model.
- Crop type, soil type, field geometry, pump constraints, and plant growth/yield feedback are not modeled.

## Recommended dissertation wording

Use language such as "prototype", "simulation", "synthetic sensor stream", "comparative experiment", and "proof of concept".

Avoid claiming "real-time IoT deployment", "field validation", "actual water savings", or "crop yield optimization" unless those features are added and measured.

## Main implementation gaps

- Add a real data source for weather and/or physical sensors.
- Persist experiment configurations and results with reproducible seeds.
- Calibrate soil dynamics against soil/crop parameters.
- Validate ANFIS-GA output against real or expert-labeled irrigation decisions.
- Add automated backend and frontend tests.
