import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from digital_twin.services.weather import WeatherService


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python tools/import_weather_csv.py <open-meteo-csv-path>", file=sys.stderr)
        return 2

    result = WeatherService().import_csv(sys.argv[1])
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
