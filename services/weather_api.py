"""Compatibility entrypoint for the weather ingestion service."""

from digital_twin.api.weather_app import app, create_app, initialize_weather_service


__all__ = ["app", "create_app", "initialize_weather_service"]

