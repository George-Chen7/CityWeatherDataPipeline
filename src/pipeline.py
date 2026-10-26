"""Airflow 与 CLI 共用的三阶段管道：文件交接 + 明确抛出异常。

DAG 只负责调度，本模块负责把阶段一已有的函数串成可独立重试的三步：

    extract_stage   抓取（或回放）原始响应 -> data/raw/*.json
    transform_stage 从 raw 重建 payload -> 清洗 -> data/processed/weather_clean.json
    load_stage      从清洗结果读回记录 -> 幂等 upsert 到 PostgreSQL

约定：阶段失败必须抛异常（而不是返回退出码），否则 Airflow 会误判成功、不会重试。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import config, extract, load, transform

logger = logging.getLogger("city_weather.pipeline")


class PipelineError(RuntimeError):
    """阶段级失败：交接文件缺失、产物为空、参数非法等。"""


def _coerce_date(value: date | str, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise PipelineError(f"{label} 日期格式错误: {value!r}，应为 YYYY-MM-DD") from exc


def _validate_range(start: date, end: date) -> None:
    if start > end:
        raise PipelineError(f"开始日期 {start} 晚于结束日期 {end}")


def _clean_path() -> Path:
    return config.PROCESSED_DIR / "weather_clean.json"


def extract_stage(
    city_codes: list[str],
    start: date | str,
    end: date | str,
    *,
    source: str = "api",
) -> dict[str, Any]:
    """抓取/回放原始数据，返回只含小元数据的 manifest。"""
    config.validate_cities()
    start_date = _coerce_date(start, "start")
    end_date = _coerce_date(end, "end")
    _validate_range(start_date, end_date)
    cities = config.resolve_cities(city_codes)

    if source not in {"api", "raw"}:
        raise PipelineError(f"未知 source={source!r}，只支持 api / raw")

    logger.info(
        "Extract 开始 | 城市=%s | 区间=%s ~ %s | source=%s",
        ", ".join(cities),
        start_date,
        end_date,
        source,
    )

    if source == "raw":
        try:
            payloads = [
                extract.load_raw_payload(city, start=start_date, end=end_date)
                for city in cities
            ]
        except extract.ExtractError as exc:
            raise PipelineError(f"raw 回放失败: {exc}") from exc
    else:
        payloads, failures = extract.extract_all(
            cities, start=start_date, end=end_date
        )
        if failures:
            detail = "; ".join(
                f"{city}: {reason}" for city, reason in failures.items()
            )
            raise PipelineError(f"抽取失败 {len(failures)} 个城市：{detail}")

    if not payloads:
        raise PipelineError("没有任何城市取得数据")

    manifest: dict[str, Any] = {
        "cities": [payload.city_code for payload in payloads],
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "source": source,
        "records_extracted": sum(payload.record_count for payload in payloads),
    }
    logger.info(
        "Extract 完成 | 城市=%d | 原始记录=%d | %s ~ %s",
        len(manifest["cities"]),
        manifest["records_extracted"],
        start_date,
        end_date,
    )
    return manifest


def transform_stage(manifest: dict[str, Any]) -> dict[str, Any]:
    """从 raw 文件重建 payload，清洗并落盘，返回清洗结果路径与报告。"""
    if not isinstance(manifest, dict):
        raise PipelineError(f"manifest 类型错误: {type(manifest).__name__}")

    cities = manifest.get("cities") or []
    if not cities:
        raise PipelineError("manifest 缺少 cities")

    start = _coerce_date(manifest.get("start"), "start")
    end = _coerce_date(manifest.get("end"), "end")
    _validate_range(start, end)

    try:
        payloads = [
            extract.load_raw_payload(city, start=start, end=end) for city in cities
        ]
    except extract.ExtractError as exc:
        raise PipelineError(f"读取 raw 失败: {exc}") from exc

    result = transform.transform_all(payloads)
    report = transform.write_outputs(result)

    if not result.records:
        raise PipelineError("清洗后没有可入库的记录")

    clean_path = _clean_path()
    if not clean_path.exists():
        raise PipelineError(f"清洗结果文件不存在: {clean_path}")

    logger.info(
        "Transform 完成 | 输入=%d | 输出=%d | 剔除=%d | 质量=%s",
        report["input_records"],
        report["output_records"],
        report["rejected_records"],
        report["quality_counts"],
    )
    return {
        **manifest,
        "clean_path": str(clean_path),
        "report": report,
    }


def read_clean_records(path: Path) -> list[transform.WeatherRecord]:
    """把 data/processed/weather_clean.json 转回 WeatherRecord，供 load 阶段使用。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PipelineError(f"清洗结果文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineError(f"清洗结果不是合法 JSON: {path}: {exc}") from exc

    if not isinstance(raw, list):
        raise PipelineError(f"清洗结果应为数组，实际是 {type(raw).__name__}")

    records: list[transform.WeatherRecord] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PipelineError(f"第 {index} 条清洗结果不是对象")

        city_code = item.get("city")
        if city_code not in config.CITIES:
            raise PipelineError(f"第 {index} 条记录城市未知: {city_code!r}")

        raw_time = item.get("observed_at_utc")
        try:
            observed_at = datetime.fromisoformat(str(raw_time))
        except (TypeError, ValueError) as exc:
            raise PipelineError(
                f"第 {index} 条记录 observed_at_utc 非法: {raw_time!r}"
            ) from exc
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        observed_at = observed_at.astimezone(timezone.utc)

        quality = item.get("quality")
        if quality not in transform.QUALITY_VALUES:
            raise PipelineError(f"第 {index} 条记录 quality 非法: {quality!r}")

        def number(field: str) -> float | None:
            value = item.get(field)
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise PipelineError(
                    f"第 {index} 条记录 {field} 非法: {value!r}"
                ) from exc

        records.append(
            transform.WeatherRecord(
                city_code=str(city_code),
                observed_at=observed_at,
                temperature_c=number("temperature_c"),
                precipitation_mm=number("precipitation_mm"),
                wind_speed_kmh=number("wind_speed_kmh"),
                quality=str(quality),
                issues=tuple(str(issue) for issue in (item.get("issues") or [])),
            )
        )

    return records


def load_stage(manifest: dict[str, Any]) -> dict[str, Any]:
    """读取清洗产物并幂等写库；失败让 DatabaseError 直接抛给 Airflow。"""
    if not isinstance(manifest, dict):
        raise PipelineError(f"manifest 类型错误: {type(manifest).__name__}")

    clean_path = Path(manifest.get("clean_path") or _clean_path())
    records = read_clean_records(clean_path)
    if not records:
        raise PipelineError("清洗结果为空，拒绝写入数据库")

    cities = manifest.get("cities") or sorted(
        {record.city_code for record in records}
    )

    logger.info(
        "Load 开始 | 城市=%s | 记录=%d | 文件=%s",
        ", ".join(cities),
        len(records),
        clean_path,
    )
    stats = load.load_all(records, cities)
    result = {
        "rows_received": stats.rows_received,
        "rows_affected": stats.rows_affected,
        "rows_unchanged": stats.rows_unchanged,
        "cities_upserted": stats.cities_upserted,
    }
    logger.info(
        "Load 完成 | 提交=%d | 新增/更新=%d | 未变化=%d",
        stats.rows_received,
        stats.rows_affected,
        stats.rows_unchanged,
    )
    return result
