"""Compatibility entrypoint for the UI-facing API.

The application is composed in digital_twin.api.main. This module remains so
existing commands such as `uvicorn backend.api:app` keep working.
"""

from digital_twin.api.main import app, create_app, initialize_api


__all__ = ["app", "create_app", "initialize_api"]

