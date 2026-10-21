"""项目级配置：路径、城市清单、数据库连接与日期参数。

所有模块只从这里取配置，不再各自硬编码；密钥只从环境变量/.env 读取。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# ---------------------------------------------------------------- 路径
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
SQL_DIR = BASE_DIR / "sql"
SCHEMA_PATH = SQL_DIR / "schema.sql"

# override=False：已存在的系统环境变量优先，便于测试时临时覆盖 .env
load_dotenv(BASE_DIR / ".env", override=False)

DEFAULT_HISTORY_DAYS = 14
FORECAST_MAX_DAYS_AHEAD = 16

# ---------------------------------------------------------------- 城市
CITIES: dict[str, dict[str, object]] = {
    "shenzhen": {
        "name": "深圳",
        "latitude": 22.5431,
        "longitude": 114.0579,
        "timezone": "Asia/Shanghai",
    },
    "hong_kong": {
        "name": "香港",
        "latitude": 22.3193,
        "longitude": 114.1694,
        "timezone": "Asia/Hong_Kong",
    },
    "singapore": {
        "name": "新加坡",
        "latitude": 1.3521,
        "longitude": 103.8198,
        "timezone": "Asia/Singapore",
    },
}

REQUIRED_DB_ENV = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")


class ConfigError(RuntimeError):
    """配置缺失或参数非法，属于启动前就该暴露的错误。"""


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    connect_timeout: int = 5

    @property
    def endpoint(self) -> str:
        """用于日志的脱敏连接串，不包含密码。"""
        return f"postgresql://{self.user}@{self.host}:{self.port}/{self.dbname}"


def ensure_directories() -> None:
    for directory in (RAW_DIR, PROCESSED_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def validate_cities() -> None:
    """启动时校验城市配置，避免跑到一半才因为时区拼错误失败。"""
    for code, info in CITIES.items():
        try:
            ZoneInfo(str(info["timezone"]))
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(
                f"城市 {code} 的时区 {info['timezone']!r} 无法识别"
            ) from exc
        latitude = float(info["latitude"])
        longitude = float(info["longitude"])
        if not -90 <= latitude <= 90:
            raise ConfigError(f"城市 {code} 的纬度 {latitude} 超出 [-90, 90]")
        if not -180 <= longitude <= 180:
            raise ConfigError(f"城市 {code} 的经度 {longitude} 超出 [-180, 180]")


def load_db_config() -> DatabaseConfig:
    """从环境变量读取数据库配置；缺项时一次性列出所有缺失的变量名。"""
    missing = [name for name in REQUIRED_DB_ENV if not os.getenv(name)]
    if missing:
        raise ConfigError(
            "缺少数据库环境变量: "
            + ", ".join(missing)
            + "\n请复制 .env.example 为 .env 并填写，或export这些变量后再运行。"
        )

    port_raw = os.environ["PGPORT"]
    timeout_raw = os.getenv("PGCONNECT_TIMEOUT", "5")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError(f"PGPORT 必须是整数，当前值: {port_raw!r}") from exc
    try:
        connect_timeout = int(timeout_raw)
    except ValueError as exc:
        raise ConfigError(
            f"PGCONNECT_TIMEOUT 必须是整数，当前值: {timeout_raw!r}"
        ) from exc

    return DatabaseConfig(
        host=os.environ["PGHOST"],
        port=port,
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        connect_timeout=connect_timeout,
    )


def parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(
            f"--{label} 日期格式错误: {value!r}，应为 YYYY-MM-DD"
        ) from exc


def resolve_date_range(
    start_raw: str | None, end_raw: str | None
) -> tuple[date, date]:
    """默认最近 14 天（含今天）；显式传入时校验先后顺序与预报上限。"""
    today = date.today()
    end = parse_date(end_raw, "end") if end_raw else today
    start = (
        parse_date(start_raw, "start")
        if start_raw
        else end - timedelta(days=DEFAULT_HISTORY_DAYS - 1)
    )

    if start > end:
        raise ConfigError(f"开始日期 {start} 晚于结束日期 {end}")
    latest_allowed = today + timedelta(days=FORECAST_MAX_DAYS_AHEAD)
    if end > latest_allowed:
        raise ConfigError(
            f"结束日期 {end} 超出 forecast 接口范围（最多预报到 {latest_allowed}）"
        )
    return start, end


def resolve_cities(names: list[str] | None) -> list[str]:
    selected = list(names or CITIES.keys())
    unknown = [name for name in selected if name not in CITIES]
    if unknown:
        raise ConfigError(
            f"未知城市: {', '.join(unknown)}；可选: {', '.join(CITIES)}"
        )
    # 去重但保持用户输入顺序
    return list(dict.fromkeys(selected))
