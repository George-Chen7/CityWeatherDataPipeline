"""管道交接层测试：清洗产物反序列化、非法输入、阶段前置条件。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src import pipeline


class ReadCleanRecordsTests(unittest.TestCase):
    def _write(self, payload: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "weather_clean.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_valid_record_is_parsed_to_utc(self) -> None:
        path = self._write(
            [
                {
                    "city": "shenzhen",
                    "observed_at_utc": "2026-10-07T16:00:00+00:00",
                    "temperature_c": 25.0,
                    "precipitation_mm": 0.0,
                    "wind_speed_kmh": 10.0,
                    "quality": "valid",
                    "issues": [],
                }
            ]
        )

        records = pipeline.read_clean_records(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].city_code, "shenzhen")
        self.assertEqual(records[0].observed_at.isoformat(), "2026-10-07T16:00:00+00:00")

    def test_unknown_quality_is_rejected(self) -> None:
        path = self._write(
            [
                {
                    "city": "shenzhen",
                    "observed_at_utc": "2026-10-07T16:00:00+00:00",
                    "quality": "perfect",
                    "issues": [],
                }
            ]
        )

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.read_clean_records(path)
        self.assertIn("quality", str(ctx.exception))

    def test_invalid_timestamp_is_rejected(self) -> None:
        path = self._write(
            [
                {
                    "city": "shenzhen",
                    "observed_at_utc": "yesterday",
                    "quality": "valid",
                    "issues": [],
                }
            ]
        )

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.read_clean_records(path)
        self.assertIn("observed_at_utc", str(ctx.exception))

    def test_non_list_payload_is_rejected(self) -> None:
        path = self._write({"records": []})

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.read_clean_records(path)
        self.assertIn("数组", str(ctx.exception))


class StagePreconditionTests(unittest.TestCase):
    def test_transform_stage_requires_cities(self) -> None:
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.transform_stage({})
        self.assertIn("cities", str(ctx.exception))

    def test_unknown_source_is_rejected_before_network(self) -> None:
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.extract_stage(
                ["shenzhen"],
                date(2026, 10, 8),
                date(2026, 10, 9),
                source="ftp",
            )
        self.assertIn("source", str(ctx.exception))

    def test_load_stage_reports_missing_clean_file(self) -> None:
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.load_stage({"clean_path": "/definitely/missing/weather_clean.json"})
        self.assertIn("不存在", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
