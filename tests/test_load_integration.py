"""数据库集成测试：在独立 schema 中验证建表、UPSERT 与幂等性。

默认跳过，避免普通单元测试误连数据库。运行方式（项目根目录）：

    $env:RUN_DB_TESTS = "1"
    $env:PGHOST = "127.0.0.1"; $env:PGPORT = "5433"
    $env:PGDATABASE = "weather"; $env:PGUSER = "weather"
    $env:PGPASSWORD = "<deploy/.env 里的 POSTGRES_PASSWORD>"
    .\\.venv\\Scripts\\python.exe -m unittest tests.test_load_integration -v
"""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timezone

from src import load
from src.transform import WeatherRecord


@unittest.skipUnless(
    os.getenv("RUN_DB_TESTS") == "1",
    "设置 RUN_DB_TESTS=1 后才连接真实 PostgreSQL",
)
class LoadIdempotencyTests(unittest.TestCase):
    def test_second_upsert_affects_zero_rows(self) -> None:
        schema = f"it_{uuid.uuid4().hex}"
        conn = load.connect()
        try:
            conn.execute(f'CREATE SCHEMA "{schema}"')
            conn.execute(f'SET search_path TO "{schema}"')
            load.ensure_schema(conn)

            city_ids = load.upsert_cities(conn, ["shenzhen"])
            records = [
                WeatherRecord(
                    city_code="shenzhen",
                    observed_at=datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc),
                    temperature_c=25.0,
                    precipitation_mm=0.0,
                    wind_speed_kmh=10.0,
                    quality="valid",
                    issues=(),
                ),
                WeatherRecord(
                    city_code="shenzhen",
                    observed_at=datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc),
                    temperature_c=26.0,
                    precipitation_mm=0.2,
                    wind_speed_kmh=11.0,
                    quality="valid",
                    issues=(),
                ),
            ]

            first = load.upsert_weather(conn, records, city_ids)
            conn.commit()
            second = load.upsert_weather(conn, records, city_ids)
            conn.commit()

            self.assertEqual(first.rows_affected, 2)
            self.assertEqual(second.rows_affected, 0)
            self.assertEqual(second.rows_unchanged, 2)

            row = conn.execute(
                "SELECT count(*), count(DISTINCT (city_id, observed_at)) "
                "FROM weather_hourly"
            ).fetchone()
            self.assertEqual(row, (2, 2))
        finally:
            conn.rollback()
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()
            conn.close()


if __name__ == "__main__":
    unittest.main()
