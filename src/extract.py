"""数据获取层（Extract）：调用 Open-Meteo，原始响应落盘，返回结构化 payload。

对外只暴露 `fetch_city` / `extract_all` / `load_raw_payload`，
下游 transform 不接触 requests，也不解析 API 的原始字典布局。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .config import CITIES, RAW_DIR, ensure_directories

logger = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARIABLES = ("temperature_2m", "precipitation", "wind_speed_10m")
REQUEST_TIMEOUT = 15
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0


class ExtractError(RuntimeError):
    """单个城市的数据获取/读取失败，调用方决定跳过还是终止。"""


@dataclass(frozen=True)
class CityPayload:
    city_code: str
    city_name: str
    timezone: str
    start_date: date
    end_date: date
    utc_offset_seconds: int | None
    hourly: dict[str, list[Any]]
    raw_path: Path | None = None
    fetched_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def record_count(self) -> int:
        return len(self.hourly.get("time", []))


def _validate_payload(city_name: str, data: dict[str, Any]) -> dict[str, list[Any]]:
    """防止结构异常的响应把问题推迟到数据库约束才暴露。"""
    hourly = data.get("hourly")
    if not isinstance(hourly, dict):
        raise ExtractError(f"{city_name} 响应缺少 hourly 字段")

    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        raise ExtractError(f"{city_name} 响应的 hourly.time 为空或类型错误")

    for variable in HOURLY_VARIABLES:
        series = hourly.get(variable)
        if not isinstance(series, list):
            raise ExtractError(f"{city_name} 响应缺少字段 hourly.{variable}")
        if len(series) != len(times):
            raise ExtractError(
                f"{city_name} 字段长度不一致: time={len(times)}, "
                f"{variable}={len(series)}"
            )
    return hourly


def _request(
    session: requests.Session,
    *,
    city_code: str,
    city_info: dict[str, Any],
    start: date,
    end: date,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "latitude": city_info["latitude"],
        "longitude": city_info["longitude"],
        "hourly": ",".join(HOURLY_VARIABLES),
        "timezone": city_info["timezone"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    city_name = str(city_info["name"])
    last_error = "未知错误"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            last_error = f"请求超时（{REQUEST_TIMEOUT} 秒）"
        except requests.exceptions.ConnectionError as exc:
            last_error = f"连接失败（{exc}）"
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            body = exc.response.text[:300] if exc.response is not None else ""
            raise ExtractError(
                f"{city_name} HTTP {status}: {body}；"
                "若日期过早，forecast 接口无法回看，需改用 archive-api"
            ) from exc
        except ValueError as exc:
            raise ExtractError(f"{city_name} 响应不是合法 JSON: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise ExtractError(f"{city_name} 请求异常: {exc}") from exc

        if attempt < MAX_ATTEMPTS:
            delay = RETRY_BACKOFF_SECONDS * attempt
            logger.warning(
                "%s %s，%.0f 秒后第 %d 次尝试", city_name, last_error, delay, attempt + 1
            )
            time.sleep(delay)

    raise ExtractError(f"{city_name} 连续 {MAX_ATTEMPTS} 次失败：{last_error}")


def _save_raw(
    city_code: str, start: date, end: date, data: dict[str, Any]
) -> Path:
    """确定性文件名：同一城市同一区间可覆盖、可回放。"""
    ensure_directories()
    path = RAW_DIR / f"{city_code}_{start.isoformat()}_{end.isoformat()}.json"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)
    return path


def _to_payload(
    *,
    city_code: str,
    data: dict[str, Any],
    start: date,
    end: date,
    raw_path: Path | None,
) -> CityPayload:
    info = CITIES[city_code]
    hourly = _validate_payload(str(info["name"]), data)
    offset = data.get("utc_offset_seconds")
    return CityPayload(
        city_code=city_code,
        city_name=str(info["name"]),
        timezone=str(info["timezone"]),
        start_date=start,
        end_date=end,
        utc_offset_seconds=int(offset) if isinstance(offset, int) else None,
        hourly=hourly,
        raw_path=raw_path,
    )


def fetch_city(
    city_code: str,
    *,
    start: date,
    end: date,
    session: requests.Session | None = None,
) -> CityPayload:
    """抓取单个城市；网络类错误已重试，最终失败抛 ExtractError。"""
    if city_code not in CITIES:
        raise ExtractError(f"未知城市: {city_code}")
    info = CITIES[city_code]
    owns_session = session is None
    session = session or requests.Session()

    try:
        data = _request(
            session, city_code=city_code, city_info=info, start=start, end=end
        )
        raw_path = _save_raw(city_code, start, end, data)
        payload = _to_payload(
            city_code=city_code, data=data, start=start, end=end, raw_path=raw_path
        )
    finally:
        if owns_session:
            session.close()

    logger.info(
        "%s | %s ~ %s | %d 条 | 原始响应 %s",
        payload.city_name,
        start,
        end,
        payload.record_count,
        raw_path,
    )
    return payload


def extract_all(
    city_codes: list[str], *, start: date, end: date
) -> tuple[list[CityPayload], dict[str, str]]:
    """逐城市抓取；单个失败只记录，不中断其他城市。"""
    payloads: list[CityPayload] = []
    failures: dict[str, str] = {}

    with requests.Session() as session:
        for city_code in city_codes:
            try:
                payloads.append(
                    fetch_city(
                        city_code, start=start, end=end, session=session
                    )
                )
            except ExtractError as exc:
                failures[city_code] = str(exc)
                logger.error("城市 %s 获取失败：%s", city_code, exc)

    return payloads, failures


def load_raw_payload(city_code: str, *, start: date, end: date) -> CityPayload:
    """离线回放：读取之前保存的原始响应，不访问网络。"""
    path = RAW_DIR / f"{city_code}_{start.isoformat()}_{end.isoformat()}.json"
    if not path.exists():
        raise ExtractError(
            f"找不到原始文件 {path}；请先不带 --from-raw 运行一次以抓取该区间"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExtractError(f"原始文件 {path} 不是合法 JSON: {exc}") from exc

    payload = _to_payload(
        city_code=city_code, data=data, start=start, end=end, raw_path=path
    )
    logger.info("%s | 从 %s 回放 %d 条", payload.city_name, path.name, payload.record_count)
    return payload
