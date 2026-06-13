from __future__ import annotations

from datetime import date, datetime, timezone
from unittest import TestCase
from unittest.mock import patch

import digital_twin.application.weather_refresh.ingestion as weather_ingestion


class WeatherIngestionTests(TestCase):
    def test_refresh_start_uses_latest_change_minus_lookback(self) -> None:
        latest_change = datetime(2026, 6, 4, 3, 21, tzinfo=timezone.utc)

        with patch.object(weather_ingestion, "_today_local", return_value=date(2026, 6, 6)):
            refresh_start = weather_ingestion._refresh_start_from_latest_change(latest_change)

        self.assertEqual(refresh_start, date(2026, 5, 28))

    def test_refresh_start_falls_back_to_today_minus_lookback(self) -> None:
        with patch.object(weather_ingestion, "_today_local", return_value=date(2026, 6, 6)):
            refresh_start = weather_ingestion._refresh_start_from_latest_change(None)

        self.assertEqual(refresh_start, date(2026, 5, 30))

    def test_forecast_request_uses_past_days_and_full_forecast_days(self) -> None:
        with patch.object(weather_ingestion, "_today_local", return_value=date(2026, 6, 6)):
            params = weather_ingestion._forecast_request_params(date(2026, 5, 28), date(2026, 6, 21))

        self.assertEqual(params["past_days"], 9)
        self.assertEqual(params["forecast_days"], 16)
        self.assertNotIn("start_date", params)
        self.assertNotIn("end_date", params)


if __name__ == "__main__":
    import unittest

    unittest.main()
