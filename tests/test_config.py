"""配置层测试：缺失变量、类型错误、日期/城市参数校验、日志脱敏。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src import config


class LoadDbConfigTests(unittest.TestCase):
    def test_missing_variables_are_listed_together(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(config.ConfigError) as ctx:
                config.load_db_config()

        message = str(ctx.exception)
        for name in config.REQUIRED_DB_ENV:
            self.assertIn(name, message)

    def test_non_integer_port_is_rejected(self) -> None:
        env = {
            "PGHOST": "127.0.0.1",
            "PGPORT": "not-a-port",
            "PGDATABASE": "weather",
            "PGUSER": "weather",
            "PGPASSWORD": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(config.ConfigError) as ctx:
                config.load_db_config()

        self.assertIn("PGPORT", str(ctx.exception))

    def test_endpoint_does_not_contain_password(self) -> None:
        cfg = config.DatabaseConfig(
            host="db",
            port=5432,
            dbname="weather",
            user="weather",
            password="do-not-log-me",
        )
        self.assertEqual(cfg.endpoint, "postgresql://weather@db:5432/weather")
        self.assertNotIn(cfg.password, cfg.endpoint)


class DateAndCityTests(unittest.TestCase):
    def test_reversed_date_range_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.resolve_date_range("2026-10-10", "2026-10-09")

    def test_bad_date_format_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError) as ctx:
            config.resolve_date_range("2026/10/10", "2026-10-11")
        self.assertIn("YYYY-MM-DD", str(ctx.exception))

    def test_unknown_city_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError) as ctx:
            config.resolve_cities(["tokyo"])
        self.assertIn("tokyo", str(ctx.exception))

    def test_city_order_is_preserved_and_deduplicated(self) -> None:
        self.assertEqual(
            config.resolve_cities(["singapore", "shenzhen", "singapore"]),
            ["singapore", "shenzhen"],
        )


if __name__ == "__main__":
    unittest.main()
