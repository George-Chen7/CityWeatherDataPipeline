"""写入层（Load）：连接管理、幂等建表、维度与事实 upsert。"""

from __future__ import annotations

import logging

import psycopg
from psycopg.types.json import Jsonb

from .config import CITIES, DatabaseConfig, SCHEMA_PATH, load_db_config
from .transform import WeatherRecord

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

# 维度表沿用 Task 3 的 city_key 作为稳定业务键（Python 侧叫 city_code）
CITY_UPSERT_SQL = """
INSERT INTO cities (city_key, city_name, latitude, longitude, timezone)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (city_key) DO UPDATE SET
    city_name = EXCLUDED.city_name,
    latitude  = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    timezone  = EXCLUDED.timezone
RETURNING city_id
"""

# 幂等的关键：
# 1) 唯一键 (city_id, observed_at) 冲突时更新；
# 2) WHERE 只在业务列真的变化时才执行 UPDATE，重复跑同一批数据 -> 受影响 0 行，
#    updated_at 也不会被无意义地刷新。
WEATHER_UPSERT_SQL = """
INSERT INTO weather_hourly
    (city_id, observed_at, temperature_c, precipitation_mm, wind_speed_kmh, quality, issues)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (city_id, observed_at) DO UPDATE SET
    temperature_c    = EXCLUDED.temperature_c,
    precipitation_mm = EXCLUDED.precipitation_mm,
    wind_speed_kmh   = EXCLUDED.wind_speed_kmh,
    quality          = EXCLUDED.quality,
    issues           = EXCLUDED.issues,
    updated_at       = now()
WHERE weather_hourly.temperature_c    IS DISTINCT FROM EXCLUDED.temperature_c
   OR weather_hourly.precipitation_mm IS DISTINCT FROM EXCLUDED.precipitation_mm
   OR weather_hourly.wind_speed_kmh   IS DISTINCT FROM EXCLUDED.wind_speed_kmh
   OR weather_hourly.quality          IS DISTINCT FROM EXCLUDED.quality
   OR weather_hourly.issues           IS DISTINCT FROM EXCLUDED.issues
"""


class DatabaseError(RuntimeError):
    """数据库写入/DDL 失败。"""


class DatabaseConnectionError(DatabaseError):
    """连接建立失败，对应验收要求里的“清晰错误信息”。"""


class LoadStats:
    def __init__(
        self,
        rows_received: int = 0,
        rows_affected: int = 0,
        rows_unchanged: int = 0,
        cities_upserted: int = 0,
    ) -> None:
        self.rows_received = rows_received
        self.rows_affected = rows_affected
        self.rows_unchanged = rows_unchanged
        self.cities_upserted = cities_upserted

    def __repr__(self) -> str:  # 方便日志
        return (
            f"LoadStats(received={self.rows_received}, affected={self.rows_affected}, "
            f"unchanged={self.rows_unchanged}, cities={self.cities_upserted})"
        )


def connect(cfg: DatabaseConfig | None = None) -> psycopg.Connection:
    """建立连接并做一次探活；失败时抛出带排查建议的异常。"""
    cfg = cfg or load_db_config()

    try:
        conn = psycopg.connect(
            host=cfg.host,
            port=cfg.port,
            dbname=cfg.dbname,
            user=cfg.user,
            password=cfg.password,
            connect_timeout=cfg.connect_timeout,
            application_name="city-weather-etl",
        )
    except psycopg.OperationalError as exc:
        raise DatabaseConnectionError(
            f"无法连接数据库 {cfg.endpoint}\n"
            f"原因: {exc}\n"
            "排查建议:\n"
            "  1) 数据库容器/服务是否在运行（docker ps --filter name=weather-pg）\n"
            f"  2) .env 中 PGHOST/PGPORT 是否为 {cfg.host}:{cfg.port}\n"
            f"  3) 数据库 {cfg.dbname} 与用户 {cfg.user} 是否已创建"
        ) from exc

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.commit()
    except psycopg.Error as exc:
        conn.close()
        raise DatabaseConnectionError(
            f"数据库 {cfg.endpoint} 连接已建立但探活失败: {exc}"
        ) from exc

    logger.info("数据库连接成功 | %s", cfg.endpoint)
    return conn


def _split_sql(text: str) -> list[str]:
    """schema.sql 由纯 DDL 组成（无函数/DO 块），按分号拆分执行即可。"""
    without_comments = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--")
    )
    return [
        statement.strip()
        for statement in without_comments.split(";")
        if statement.strip()
    ]


def ensure_schema(conn: psycopg.Connection) -> None:
    """幂等建表：每次运行都执行，已存在则跳过。"""
    statements = _split_sql(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
    except psycopg.Error as exc:
        raise DatabaseError(
            f"创建表结构失败（SQLSTATE={exc.sqlstate}）: {exc}"
        ) from exc
    logger.info("表结构已就绪（执行 %d 条 DDL）", len(statements))


def upsert_cities(
    conn: psycopg.Connection, city_codes: list[str]
) -> dict[str, int]:
    """维度表按 city_code upsert，返回 code -> city_id。"""
    city_ids: dict[str, int] = {}
    with conn.cursor() as cur:
        for city_code in city_codes:
            info = CITIES[city_code]
            cur.execute(
                CITY_UPSERT_SQL,
                (
                    city_code,
                    info["name"],
                    info["latitude"],
                    info["longitude"],
                    info["timezone"],
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise DatabaseError(f"城市 {city_code} upsert 未返回 city_id")
            city_ids[city_code] = row[0]
    logger.info("城市维度已就绪 | %s", ", ".join(sorted(city_ids)))
    return city_ids


def upsert_weather(
    conn: psycopg.Connection,
    records: list[WeatherRecord],
    city_ids: dict[str, int],
) -> LoadStats:
    """分批 upsert 事实表；整批在同一事务里，异常由调用方回滚。"""
    stats = LoadStats(rows_received=len(records))
    if not records:
        return stats

    with conn.cursor() as cur:
        for offset in range(0, len(records), BATCH_SIZE):
            batch = records[offset : offset + BATCH_SIZE]
            params = [
                (
                    city_ids[record.city_code],
                    record.observed_at,
                    record.temperature_c,
                    record.precipitation_mm,
                    record.wind_speed_kmh,
                    record.quality,
                    Jsonb(list(record.issues)),
                )
                for record in batch
            ]
            cur.executemany(WEATHER_UPSERT_SQL, params)
            affected = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            stats.rows_affected += affected
            logger.info(
                "批次 %d-%d | %d 条 | 受影响 %d 行",
                offset + 1,
                offset + len(batch),
                len(batch),
                affected,
            )

    stats.rows_unchanged = stats.rows_received - stats.rows_affected
    return stats


def load_all(
    records: list[WeatherRecord],
    city_codes: list[str],
    cfg: DatabaseConfig | None = None,
) -> LoadStats:
    """一次完整写入：建表（独立事务）-> 维度 + 事实（同一事务）。"""
    conn = connect(cfg)
    try:
        ensure_schema(conn)
        try:
            with conn.transaction():
                city_ids = upsert_cities(conn, city_codes)
                stats = upsert_weather(conn, records, city_ids)
            stats.cities_upserted = len(city_ids)
            return stats
        except psycopg.Error as exc:
            raise DatabaseError(
                f"写入数据库失败（SQLSTATE={exc.sqlstate}）: {exc}"
            ) from exc
    finally:
        conn.close()
