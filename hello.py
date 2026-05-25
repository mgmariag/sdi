"""Compatibility entrypoint.

The active API lives in backend.api. This wrapper keeps older local commands
such as `uvicorn hello:app` working while the project uses package-based
service entrypoints.
"""

from backend.api import app


__all__ = ["app"]
