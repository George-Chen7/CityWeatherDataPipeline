"""清洗层测试：四态标签、UTC 转换、重复去重、无法解析时间单独导出。"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from src.extract import CityPayload
from src.transform import transform_all


def make_payload(hourly: dict, *, city_code: str = "shenzhen") -> CityPayload:
    return CityPayload(
        city_code=city_code,
        city_name="深圳",
        timezone="Asia/Shanghai",
        start_date=date(2026, 10, 8),
        end_date=date(2026, 10, 8),
        utc_offset_seconds=28800,
        hourly=hourly,
    )


class TransformTests(unittest.TestCase):
    def test_quality_labels_and_rejected_records(self) -> None:
        payload = make_payload(
            {
                "time": [
                    "2026-10-08T00:00",
                    "2026-10-08T01:00",
                    "2026-10-08T02:00",
                    "2026-10-08T03:00",
                    "not-a-time",
                ],
                "temperature_2m": [25.0, None, 70.0, 20.0, 20.0],
                "precipitation": [0.0, 0.0, 0.0, -1.0, 0.0],
                "wind_speed_10m": [10.0, 10.0, 400.0, 10.0, 10.0],
            }
        )

        result = transform_all([payload])

        self.assertEqual(result.input_records, 5)
        self.assertEqual(result.output_records, 4)
        self.assertEqual(len(result.rejected), 1)
        self.assertEqual(result.rejected[0].raw_time, "not-a-time")
        self.assertEqual(
            result.quality_counts,
            {"valid": 1, "missing": 1, "suspect": 1, "invalid": 1},
        )
        self.assertEqual(result.per_city["shenzhen"]["valid"], 1)

        first = result.records[0]
        self.assertEqual(
            first.observed_at,
            datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc),
            "00:00 Asia/Shanghai 应转换为前一天 16:00 UTC",
        )

    def test_duplicate_business_key_is_counted_and_removed(self) -> None:
        hourly = {
            "time": ["2026-10-08T00:00"],
            "temperature_2m": [25.0],
            "precipitation": [0.0],
            "wind_speed_10m": [10.0],
        }
        result = transform_all([make_payload(hourly), make_payload(hourly)])

        self.assertEqual(result.output_records, 1)
        self.assertEqual(result.duplicates_removed, 1)

    def test_utc_offset_mismatch_marks_record_suspect(self) -> None:
        payload = CityPayload(
            city_code="shenzhen",
            city_name="深圳",
            timezone="Asia/Shanghai",
            start_date=date(2026, 10, 8),
            end_date=date(2026, 10, 8),
            utc_offset_seconds=0,
            hourly={
                "time": ["2026-10-08T00:00"],
                "temperature_2m": [25.0],
                "precipitation": [0.0],
                "wind_speed_10m": [10.0],
            },
        )

        result = transform_all([payload])

        self.assertEqual(result.records[0].quality, "suspect")
        self.assertIn(
            "suspect:utc_offset_mismatch",
            result.records[0].issues,
        )

    def test_all_numeric_fields_missing_is_invalid(self) -> None:
        payload = make_payload(
            {
                "time": ["2026-10-08T00:00"],
                "temperature_2m": [None],
                "precipitation": [None],
                "wind_speed_10m": [None],
            }
        )

        result = transform_all([payload])

        self.assertEqual(result.records[0].quality, "invalid")
        self.assertIn("invalid:no_measurement", result.records[0].issues)


if __name__ == "__main__":
    unittest.main()
