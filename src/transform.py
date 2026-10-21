"""数据清洗层（Transform）：纯函数式地把 payload 转成结构化记录。

设计原则：
- 不删除异常记录，只打四态标签（valid / missing / suspect / invalid）并记录 issues；
- 时间统一转成 UTC 的 aware datetime，供 TIMESTAMPTZ 存储；
- 无法解析时间的记录无法进入事实表，单独收集为 rejected，不静默丢弃。
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import CITIES, PROCESSED_DIR, ensure_directories
from .extract import HOURLY_VARIABLES, CityPayload

logger = logging.getLogger(__name__)

NUMERIC_FIELDS = HOURLY_VARIABLES
QUALITY_VALUES = ("valid", "missing", "suspect", "invalid")

# 阈值规则：hard_min 直接判 invalid；suspect_* 仅标记，数据仍然保留
FIELD_RULES: dict[str, dict[str, float]] = {
    "temperature_2m": {"suspect_min": -60.0, "suspect_max": 60.0},
    "precipitation": {"hard_min": 0.0, "suspect_max": 500.0},
    "wind_speed_10m": {"hard_min": 0.0, "suspect_max": 300.0},
}


@dataclass(frozen=True)
class WeatherRecord:
    city_code: str
    observed_at: datetime  # UTC aware
    temperature_c: float | None
    precipitation_mm: float | None
    wind_speed_kmh: float | None
    quality: str
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        city_name = str(CITIES[self.city_code]["name"])
        tz = ZoneInfo(str(CITIES[self.city_code]["timezone"]))
        return {
            "city": self.city_code,
            "city_name": city_name,
            "observed_at_utc": self.observed_at.isoformat(),
            "observed_at_local": self.observed_at.astimezone(tz).isoformat(),
            "temperature_c": self.temperature_c,
            "precipitation_mm": self.precipitation_mm,
            "wind_speed_kmh": self.wind_speed_kmh,
            "quality": self.quality,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class RejectedRecord:
    city_code: str
    raw_time: Any
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "city": self.city_code,
            "raw_time": self.raw_time,
            "issues": list(self.issues),
        }


@dataclass
class TransformResult:
    records: list[WeatherRecord] = field(default_factory=list)
    rejected: list[RejectedRecord] = field(default_factory=list)
    input_records: int = 0
    duplicates_removed: int = 0
    quality_counts: Counter[str] = field(default_factory=Counter)
    per_city: dict[str, Counter[str]] = field(default_factory=dict)

    @property
    def output_records(self) -> int:
        return len(self.records)


def _get_at(series: Any, index: int) -> Any:
    return series[index] if isinstance(series, list) and index < len(series) else None


def _parse_time(
    raw_time: Any,
    *,
    timezone_name: str,
    expected_utc_offset: int | None,
    issues: list[str],
) -> datetime | None:
    """返回 UTC aware datetime；失败时记录 issue 并返回 None。

    支持 Open-Meteo 的 ISO 本地时间字符串，以及 timeformat=unixtime 的整数，
    这样历史 raw 与新请求都能回放。
    """
    if raw_time is None or isinstance(raw_time, bool):
        issues.append("invalid:time_missing")
        return None

    if isinstance(raw_time, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw_time), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            issues.append("invalid:time_value")
            return None

    if not isinstance(raw_time, str) or not raw_time.strip():
        issues.append("invalid:time_format")
        return None

    try:
        parsed = datetime.fromisoformat(raw_time)
    except ValueError:
        issues.append("invalid:time_format")
        return None

    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            issues.append("invalid:timezone_unknown")
            return None

    # API 同时返回 utc_offset_seconds；不一致说明时区/夏令时假设有问题
    offset = parsed.utcoffset()
    if (
        expected_utc_offset is not None
        and offset is not None
        and int(offset.total_seconds()) != expected_utc_offset
    ):
        issues.append("suspect:utc_offset_mismatch")

    return parsed.astimezone(timezone.utc)


def _to_float(value: Any, *, field_name: str, issues: list[str]) -> float | None:
    """类型转换 + 合理性检查；bool 是 int 子类，必须单独排除。"""
    if value is None:
        issues.append(f"missing:{field_name}")
        return None
    if isinstance(value, bool):
        issues.append(f"invalid:{field_name}_type")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        issues.append(f"invalid:{field_name}_type")
        return None
    if math.isnan(number) or math.isinf(number):
        issues.append(f"invalid:{field_name}_value")
        return None

    rule = FIELD_RULES.get(field_name, {})
    if "hard_min" in rule and number < rule["hard_min"]:
        issues.append(f"invalid:{field_name}_below_min")
    if "suspect_min" in rule and number < rule["suspect_min"]:
        issues.append(f"suspect:{field_name}_out_of_range")
    if "suspect_max" in rule and number > rule["suspect_max"]:
        issues.append(f"suspect:{field_name}_out_of_range")
    return number


def _decide_quality(issues: list[str]) -> str:
    if any(issue.startswith("invalid") for issue in issues):
        return "invalid"
    if any(issue.startswith("suspect") for issue in issues):
        return "suspect"
    if any(issue.startswith("missing") for issue in issues):
        return "missing"
    return "valid"


def _unique(issues: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            ordered.append(issue)
    return tuple(ordered)


def _build_record(
    payload: CityPayload, index: int
) -> WeatherRecord | RejectedRecord:
    issues: list[str] = []
    hourly = payload.hourly
    raw_time = _get_at(hourly.get("time"), index)

    observed_at = _parse_time(
        raw_time,
        timezone_name=payload.timezone,
        expected_utc_offset=payload.utc_offset_seconds,
        issues=issues,
    )

    values = {
        field_name: _to_float(
            _get_at(hourly.get(field_name), index),
            field_name=field_name,
            issues=issues,
        )
        for field_name in NUMERIC_FIELDS
    }

    if all(value is None for value in values.values()):
        issues.append("invalid:no_measurement")

    unique_issues = _unique(issues)

    if observed_at is None:
        return RejectedRecord(
            city_code=payload.city_code,
            raw_time=raw_time,
            issues=unique_issues,
        )

    return WeatherRecord(
        city_code=payload.city_code,
        observed_at=observed_at,
        temperature_c=values["temperature_2m"],
        precipitation_mm=values["precipitation"],
        wind_speed_kmh=values["wind_speed_10m"],
        quality=_decide_quality(list(unique_issues)),
        issues=unique_issues,
    )


def transform_all(payloads: list[CityPayload]) -> TransformResult:
    """合并多个城市的 payload，按 (city, observed_at) 去重后排序。"""
    result = TransformResult()
    seen: set[tuple[str, datetime]] = set()

    for payload in payloads:
        city_counter = result.per_city.setdefault(payload.city_code, Counter())
        for index in range(payload.record_count):
            result.input_records += 1
            record = _build_record(payload, index)

            if isinstance(record, RejectedRecord):
                result.rejected.append(record)
                logger.warning(
                    "%s 时间无法解析，已剔除 | raw_time=%r | issues=%s",
                    record.city_code,
                    record.raw_time,
                    ", ".join(record.issues),
                )
                continue

            key = (record.city_code, record.observed_at)
            if key in seen:
                result.duplicates_removed += 1
                logger.debug("重复记录 | %s @ %s", record.city_code, record.observed_at)
                continue
            seen.add(key)

            result.records.append(record)
            result.quality_counts[record.quality] += 1
            city_counter[record.quality] += 1

    result.records.sort(key=lambda item: (item.city_code, item.observed_at))
    return result


def build_report(result: TransformResult) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_records": result.input_records,
        "duplicates_removed": result.duplicates_removed,
        "output_records": result.output_records,
        "rejected_records": len(result.rejected),
        "quality_counts": {
            quality: result.quality_counts.get(quality, 0)
            for quality in QUALITY_VALUES
        },
        "per_city": {
            city: {
                "records": sum(counter.values()),
                "quality_counts": {
                    quality: counter.get(quality, 0)
                    for quality in QUALITY_VALUES
                },
            }
            for city, counter in sorted(result.per_city.items())
        },
    }


def write_outputs(result: TransformResult) -> dict[str, Any]:
    """把清洗结果落盘，供排查与回放；返回报告字典。"""
    ensure_directories()
    report = build_report(result)

    clean_path = PROCESSED_DIR / "weather_clean.json"
    rejected_path = PROCESSED_DIR / "weather_rejected.json"
    report_path = PROCESSED_DIR / "cleaning_report.json"

    for path, payload in (
        (clean_path, [record.to_dict() for record in result.records]),
        (rejected_path, [record.to_dict() for record in result.rejected]),
        (report_path, report),
    ):
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    logger.info(
        "清洗结果已写入 %s / %s / %s",
        clean_path.name,
        rejected_path.name,
        report_path.name,
    )
    return report
