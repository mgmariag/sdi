from __future__ import annotations

import json
import sys

from digital_twin.services.weather_service import WeatherService


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m digital_twin.scripts.import_weather_csv <open-meteo-csv-path>", file=sys.stderr)
        return 2

    result = WeatherService().import_csv(sys.argv[1])
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


