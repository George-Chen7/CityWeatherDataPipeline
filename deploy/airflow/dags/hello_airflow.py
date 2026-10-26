from datetime import datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

with DAG(
    dag_id="hello_airflow",
    description="阶段二任务 03：最小 DAG，只验证解析与执行",
    start_date=datetime(2026, 1, 1),
    schedule=None,      # 只手动触发；schedule / retry 留给任务 04、05
    catchup=False,
    tags=["stage2", "learning"],
) as dag:
    who_am_i = BashOperator(
        task_id="who_am_i",
        bash_command=(
            "echo host=$(hostname); "
            "python --version; "
            'echo "dag={{ dag.dag_id }} task={{ task.task_id }}"'
        ),
    )
    done = BashOperator(task_id="done", bash_command='echo "success at {{ ts }}"')

    who_am_i >> done