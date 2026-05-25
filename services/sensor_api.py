"""Compatibility entrypoint for the sensor ingestion service."""

from digital_twin.api.sensor_app import app, create_app, initialize_sensor_service


__all__ = ["app", "create_app", "initialize_sensor_service"]

