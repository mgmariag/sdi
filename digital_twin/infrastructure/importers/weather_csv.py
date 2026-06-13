from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from digital_twin.application.weather_refresh.weather_refresh_service import (
    WeatherService,
)


def import_weather_csv(csv_path: str | Path, skip_existing_observed: bool = True) -> dict[str, Any]:
    return WeatherService().import_csv(csv_path=csv_path, skip_existing_observed=skip_existing_observed)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: python -m digital_twin.infrastructure.importers.weather_csv <open-meteo-csv-path>", file=sys.stderr)
        return 2

    result = import_weather_csv(args[0])
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

