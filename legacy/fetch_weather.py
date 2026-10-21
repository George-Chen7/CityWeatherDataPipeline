import json
import logging
import os
from datetime import datetime, timezone

import requests

# ============================================================
# 配置区：日期、城市坐标、时区均可修改
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

# 最近 14 天逐小时；如需指定日期区间，改用 start_date / end_date
DATE_PARAMS = {
    "past_days": 14,
    # "start_date": "2025-01-01",
    # "end_date": "2025-01-14",
}

HOURLY_VARIABLES = ["temperature_2m", "precipitation", "wind_speed_10m"]
REQUEST_TIMEOUT = 15  # 秒
RAW_DATA_DIR = "data/raw"
API_BASE_URL = "https://api.open-meteo.com/v1/forecast"

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def ensure_raw_dir():
    os.makedirs(RAW_DATA_DIR, exist_ok=True)


def build_params(city_info: dict) -> dict:
    params = {
        "latitude": city_info["latitude"],
        "longitude": city_info["longitude"],
        "hourly": ",".join(HOURLY_VARIABLES),
        "timezone": city_info["timezone"],
    }
    params.update(DATE_PARAMS)
    return params


def fetch_city_weather(city_key: str, city_info: dict) -> dict | None:
    params = build_params(city_info)
    city_name = city_info["name"]

    try:
        logger.info("请求 %s | URL=%s | params=%s", city_name, API_BASE_URL, params)
        response = requests.get(API_BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("%s 请求超时（%s 秒），跳过该城市", city_name, REQUEST_TIMEOUT)
        return None
    except requests.exceptions.HTTPError as e:
        logger.error(
            "%s 请求返回 HTTP 错误 | status=%s | 响应前 200 字=%s",
            city_name,
            e.response.status_code,
            e.response.text[:200],
        )
        return None
    except requests.exceptions.RequestException as e:
        logger.error("%s 请求异常 | %s", city_name, e)
        return None

    data = response.json()

    # 保存原始响应
    ensure_raw_dir()
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    raw_path = os.path.join(RAW_DATA_DIR, f"{city_key}_{date_tag}.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    hourly = data.get("hourly", {})
    n_records = len(hourly.get("time", []))
    logger.info(
        "成功获取 %s | 条数=%s | 原始响应已保存到 %s",
        city_name,
        n_records,
        raw_path,
    )
    return data


def main():
    logger.info("开始抓取 %s 个城市的逐小时天气数据", len(CITIES))
    results = {}

    for city_key, city_info in CITIES.items():
        data = fetch_city_weather(city_key, city_info)
        results[city_key] = {
            "city_name": city_info["name"],
            "success": data is not None,
            "record_count": len(data["hourly"]["time"]) if data else 0,
        }

    logger.info("抓取结束 | 汇总: %s", results)
    return results


if __name__ == "__main__":
    main()