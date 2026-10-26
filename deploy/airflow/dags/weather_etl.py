"""阶段二任务 04：天气 ETL 的 Airflow 编排。

DAG 只负责调度与任务依赖，业务逻辑全部留在 src/：
    extract  ->  data/raw/*.json
    transform -> data/processed/weather_clean.json
    load     ->  PostgreSQL（幂等 upsert）

任务之间只通过 XCom 传 manifest（城市、区间、产物路径），不传 1008 条记录本身。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

from src import config
from src.pipeline import extract_stage, load_stage, transform_stage

TZ = ZoneInfo("Asia/Shanghai")
LOOKBACK_DAYS = 14


def _run_conf(context: dict) -> dict:
    dag_run = context.get("dag_run")
    return (dag_run.conf or {}) if dag_run is not None else {}


def _resolve_window(context: dict) -> tuple[date, date]:
    """优先使用手动触发 conf；否则按 data interval 取“昨天往前 14 天”。"""
    conf = _run_conf(context)
    if conf.get("start") or conf.get("end"):
        if not (conf.get("start") and conf.get("end")):
            raise ValueError("手动触发时 start 和 end 必须同时提供")
        start = date.fromisoformat(str(conf["start"]))
        end = date.fromisoformat(str(conf["end"]))
        if start > end:
            raise ValueError(f"start={start} 晚于 end={end}")
        return start, end

    # 定时运行有 data interval；手动触发且不带 conf 时 Airflow 可能给 None
    interval_end = context.get("data_interval_end") or datetime.now(TZ)
    end = interval_end.astimezone(TZ).date() - timedelta(days=1)
    start = end - timedelta(days=LOOKBACK_DAYS - 1)
    return start, end


def run_extract(**context: dict) -> dict:
    conf = _run_conf(context)
    cities = conf.get("cities") or list(config.CITIES)
    if isinstance(cities, str):
        cities = [city.strip() for city in cities.split(",") if city.strip()]

    start, end = _resolve_window(context)
    source = str(conf.get("source", "api"))
    return extract_stage(cities, start, end, source=source)


def run_transform(**context: dict) -> dict:
    manifest = context["ti"].xcom_pull(task_ids="extract")
    if not manifest:
        raise ValueError("extract 没有返回 manifest，无法进入 transform")
    return transform_stage(manifest)


def run_load(**context: dict) -> dict:
    manifest = context["ti"].xcom_pull(task_ids="transform")
    if not manifest:
        raise ValueError("transform 没有返回 manifest，无法进入 load")
    return load_stage(manifest)


with DAG(
    dag_id="weather_etl",
    description="阶段二任务 04：Open-Meteo -> 清洗 -> PostgreSQL（E/T/L）",
    start_date=datetime(2026, 10, 1, tzinfo=TZ),
    schedule="0 2 * * *",
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=10),
    },
    tags=["stage2", "weather", "etl"],
) as dag:
    extract_task = PythonOperator(
        task_id="extract",
        python_callable=run_extract,
        execution_timeout=timedelta(minutes=15),
    )
    transform_task = PythonOperator(
        task_id="transform",
        python_callable=run_transform,
        retries=1,
        execution_timeout=timedelta(minutes=10),
    )
    load_task = PythonOperator(
        task_id="load",
        python_callable=run_load,
        retries=2,
        execution_timeout=timedelta(minutes=10),
    )

    extract_task >> transform_task >> load_task
