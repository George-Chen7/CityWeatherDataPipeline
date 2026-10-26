# 官方 Airflow 3.3.2 镜像 + 项目直接依赖。
# 依赖版本由根目录 requirements.txt 固定；不要在这里临时 pip install。
FROM apache/airflow:3.3.2

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt
