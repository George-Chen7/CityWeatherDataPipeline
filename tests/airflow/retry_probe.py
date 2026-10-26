r"""失败注入夹具（不是生产 DAG，也不在 dags/ 目录里，Airflow 不会自动加载）。

用途：为阶段二验收留下真实的“失败 -> 自动重试 -> 修复 -> 成功”记录。

用法（在 deploy/airflow 目录下）：
    Copy-Item ..\..\tests\airflow\retry_probe.py .\dags\
    docker compose exec airflow-scheduler airflow dags list | Select-String retry_probe
    docker compose exec airflow-scheduler airflow dags unpause retry_probe
    docker compose exec airflow-scheduler airflow dags trigger retry_probe
    # 观察 3 次失败（retries=2，间隔 10 秒）：
    docker compose exec airflow-scheduler airflow tasks states-for-dag-run retry_probe <run_id>

    # 创建标记文件 = 修复故障；再触发一次：
    New-Item ..\..\data\processed\retry_probe_ready -ItemType File
    docker compose exec airflow-scheduler airflow dags trigger retry_probe
    # 成功后清理，避免污染交付：
    Remove-Item .\dags\retry_probe.py
    Remove-Item ..\..\data\processed\retry_probe_ready
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

TZ = ZoneInfo("Asia/Shanghai")
MARKER = Path("/opt/project/data/processed/retry_probe_ready")


def flaky_check() -> None:
    if not MARKER.exists():
        raise RuntimeError(
            "retry_probe: 标记文件不存在，这是人为制造的预期失败；"
            f"创建 {MARKER} 后重跑即可恢复"
        )
    print(f"retry_probe: 找到 {MARKER}，故障已修复")


with DAG(
    dag_id="retry_probe",
    description="阶段二验收夹具：验证 retries / retry_delay / 恢复",
    start_date=datetime(2026, 10, 1, tzinfo=TZ),
    schedule=None,
    catchup=False,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(seconds=10),
    },
    tags=["stage2", "verification", "retry"],
) as dag:
    PythonOperator(task_id="flaky", python_callable=flaky_check)
