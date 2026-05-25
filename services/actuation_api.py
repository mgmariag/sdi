"""Compatibility entrypoint for the irrigation actuation service."""

from digital_twin.api.actuation_app import app, create_app, initialize_actuation_service


__all__ = ["app", "create_app", "initialize_actuation_service"]
