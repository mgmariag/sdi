from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from digital_twin.application.weather_refresh.ingestion import WeatherIngestion


class WeatherCsvImportTests(TestCase):
    def test_importer_parses_and_upserts_open_meteo_csv(self) -> None:
        rows = [
            {"observed_at": datetime(2026, 6, 1, 10, tzinfo=timezone.utc)},
            {"observed_at": datetime(2026, 6, 1, 11, tzinfo=timezone.utc)},
        ]
        parsed = {"rows": rows, "skipped_current_conditions_rows": 2}
        stats = {
            "inserted": 1,
            "updated": 1,
            "unchanged": 0,
            "skipped_existing_observed": 0,
        }
        ingestion = WeatherIngestion()

        with (
            patch("digital_twin.application.weather_refresh.ingestion.initialize_database") as initialize_database,
            patch.object(ingestion.rows, "parse_open_meteo_csv", return_value=parsed) as parse_csv,
            patch.object(ingestion.persistence, "upsert_weather_hourly_with_stats", return_value=stats) as upsert_weather,
        ):
            result = ingestion.import_weather_csv(
                Path("open-meteo.csv"),
                skip_existing_observed=False,
            )

        initialize_database.assert_called_once_with()
        parse_csv.assert_called_once_with(Path("open-meteo.csv"))
        upsert_weather.assert_called_once_with(rows, skip_existing_observed=False)
        self.assertEqual(result["file"], "open-meteo.csv")
        self.assertEqual(result["rows_in_file"], 2)
        self.assertEqual(result["first_timestamp"], "2026-06-01T10:00:00+00:00")
        self.assertEqual(result["last_timestamp"], "2026-06-01T11:00:00+00:00")
        self.assertEqual(result["skipped_current_conditions_rows"], 2)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["updated"], 1)

    def test_importer_handles_empty_csv_rows(self) -> None:
        parsed = {"rows": [], "skipped_current_conditions_rows": 0}
        stats = {
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped_existing_observed": 0,
        }
        ingestion = WeatherIngestion()

        with (
            patch("digital_twin.application.weather_refresh.ingestion.initialize_database"),
            patch.object(ingestion.rows, "parse_open_meteo_csv", return_value=parsed),
            patch.object(ingestion.persistence, "upsert_weather_hourly_with_stats", return_value=stats) as upsert_weather,
        ):
            result = ingestion.import_weather_csv("open-meteo.csv")

        upsert_weather.assert_called_once_with([], skip_existing_observed=True)
        self.assertEqual(result["rows_in_file"], 0)
        self.assertIsNone(result["first_timestamp"])
        self.assertIsNone(result["last_timestamp"])
