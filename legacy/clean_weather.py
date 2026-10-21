import json
import logging
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================================================
# 配置区：与 Task 1 保持一致
# ============================================================
CITIES = {
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

RAW_DATA_DIR = "data/raw"
PROCESSED_DIR = "data/processed"
NUMERIC_FIELDS = ["temperature_2m", "precipitation", "wind_speed_10m"]

# 合理性阈值：hard_min 触发 invalid，suspect_* 触发 suspect（保留但标记）
FIELD_RULES = {
    "temperature_2m": {"suspect_min": -60.0, "suspect_max": 60.0},
    "precipitation": {"hard_min": 0.0, "suspect_max": 500.0},
    "wind_speed_10m": {"hard_min": 0.0, "suspect_max": 300.0},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_time(raw_time, tz_name: str, issues: list):
    """返回 (time_utc_iso, time_local_iso)。API 返回的是城市本地时间，无偏移。"""
    if not raw_time or not isinstance(raw_time, str):
        issues.append("invalid:time_missing")
        return None, None
    try:
        dt = datetime.fromisoformat(raw_time)
    except ValueError:
        issues.append("invalid:time_format")
        return None, None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        except Exception:
            issues.append("invalid:timezone_unknown")
            return None, None
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.isoformat(), dt.isoformat()


def to_float(value, field: str, issues: list):
    """把原始值转为 float；缺失返回 None 并记 missing，类型错误记 invalid。"""
    if value is None:
        issues.append(f"missing:{field}")
        return None
    if isinstance(value, bool):  # bool 是 int 子类，必须单独排除
        issues.append(f"invalid:{field}_type")
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        issues.append(f"invalid:{field}_type")
        return None
    if math.isnan(f) or math.isinf(f):
        issues.append(f"invalid:{field}_value")
        return None

    rule = FIELD_RULES.get(field, {})
    if "hard_min" in rule and f < rule["hard_min"]:
        issues.append(f"invalid:{field}_below_min")
    if "suspect_min" in rule and f < rule["suspect_min"]:
        issues.append(f"suspect:{field}_out_of_range")
    if "suspect_max" in rule and f > rule["suspect_max"]:
        issues.append(f"suspect:{field}_out_of_range")
    return f


def decide_quality(issues: list) -> str:
    if any(i.startswith("invalid") for i in issues):
        return "invalid"
    if any(i.startswith("suspect") for i in issues):
        return "suspect"
    if any(i.startswith("missing") for i in issues):
        return "missing"
    return "valid"


def get_at(arr, idx):
    return arr[idx] if isinstance(arr, list) and idx < len(arr) else None


def build_record(city_key: str, city_info: dict, hourly: dict, idx: int) -> dict:
    issues = []
    raw_time = get_at(hourly.get("time"), idx)
    time_utc, time_local = parse_time(raw_time, city_info["timezone"], issues)

    values = {}
    for field in NUMERIC_FIELDS:
        values[field] = to_float(get_at(hourly.get(field), idx), field, issues)

    # 三个测量值全缺失，记录没有分析价值，标为 invalid 但保留
    if all(v is None for v in values.values()):
        issues.append("invalid:no_measurement")

    # 去重 issues，保持顺序
    seen, uniq = set(), []
    for i in issues:
        if i not in seen:
            seen.add(i)
            uniq.append(i)

    return {
        "city": city_key,
        "city_name": city_info["name"],
        "time_utc": time_utc,
        "time_local": time_local,
        "temperature_2m": values["temperature_2m"],
        "precipitation": values["precipitation"],
        "wind_speed_10m": values["wind_speed_10m"],
        "quality": decide_quality(uniq),
        "issues": uniq,
    }


def load_raw_files():
    files = sorted(Path(RAW_DATA_DIR).glob("*.json"))
    if not files:
        raise FileNotFoundError(
            f"{RAW_DATA_DIR}/ 下没有找到原始数据，请先运行 Task 1 的抓取脚本"
        )
    return files


def main():
    files = load_raw_files()
    logger.info("发现 %s 个原始文件，开始清洗", len(files))

    all_records = []
    seen_keys = set()
    input_count = 0
    duplicates_removed = 0
    per_city_input = Counter()
    per_city_quality = defaultdict(Counter)

    for file_path in files:
        # 文件名形如 shenzhen_20251021.json
        city_key = file_path.stem.rsplit("_", 1)[0]
        if city_key not in CITIES:
            logger.warning("跳过未知城市文件: %s", file_path.name)
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        hourly = data.get("hourly", {})
        times = hourly.get("time", []) or []
        city_info = CITIES[city_key]
        logger.info(
            "读取 %s | 城市=%s | 原始条数=%s",
            file_path.name,
            city_info["name"],
            len(times),
        )

        for idx in range(len(times)):
            input_count += 1
            per_city_input[city_key] += 1
            record = build_record(city_key, city_info, hourly, idx)

            # 按 (城市, UTC 时间) 去重；时间解析失败的记录不去重，全部保留待排查
            dedup_key = (
                (city_key, record["time_utc"]) if record["time_utc"] else None
            )
            if dedup_key is not None and dedup_key in seen_keys:
                duplicates_removed += 1
                logger.debug(
                    "重复记录已跳过 | city=%s | time_utc=%s",
                    city_key,
                    record["time_utc"],
                )
                continue
            if dedup_key is not None:
                seen_keys.add(dedup_key)

            all_records.append(record)
            per_city_quality[city_key][record["quality"]] += 1

    all_records.sort(key=lambda r: (r["city"], r["time_utc"] or ""))

    quality_counts = Counter(r["quality"] for r in all_records)
    issue_counts = Counter(
        issue for r in all_records for issue in r["issues"]
    )
    field_missing_counts = Counter()
    for r in all_records:
        for field in NUMERIC_FIELDS:
            if r[field] is None:
                field_missing_counts[field] += 1

    abnormal = (
        quality_counts.get("missing", 0)
        + quality_counts.get("suspect", 0)
        + quality_counts.get("invalid", 0)
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_records": input_count,
        "duplicates_removed": duplicates_removed,
        "output_records": len(all_records),
        "quality_counts": {
            "valid": quality_counts.get("valid", 0),
            "missing": quality_counts.get("missing", 0),
            "suspect": quality_counts.get("suspect", 0),
            "invalid": quality_counts.get("invalid", 0),
        },
        "abnormal_records": abnormal,
        "field_missing_counts": dict(field_missing_counts),
        "issue_counts": dict(issue_counts),
        "per_city": {
            city: {
                "input_records": per_city_input[city],
                "quality_counts": dict(per_city_quality[city]),
            }
            for city in CITIES
        },
    }

    os.makedirs(PROCESSED_DIR, exist_ok=True)
    clean_path = os.path.join(PROCESSED_DIR, "weather_clean.json")
    invalid_path = os.path.join(PROCESSED_DIR, "weather_invalid.json")
    report_path = os.path.join(PROCESSED_DIR, "cleaning_report.json")

    with open(clean_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)
    with open(invalid_path, "w", encoding="utf-8") as f:
        json.dump(
            [r for r in all_records if r["quality"] == "invalid"],
            f,
            ensure_ascii=False,
            indent=2,
        )
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    logger.info("清洗完成 | 输入=%s | 重复删除=%s | 输出=%s",
                input_count, duplicates_removed, len(all_records))
    logger.info("质量分布 | %s", report["quality_counts"])
    logger.info("异常记录数（missing+suspect+invalid）=%s", abnormal)
    logger.info("字段缺失计数 | %s", report["field_missing_counts"])
    logger.info("问题计数 | %s", report["issue_counts"])
    logger.info("输出文件 | %s | %s | %s", clean_path, invalid_path, report_path)

    return report


if __name__ == "__main__":
    main()