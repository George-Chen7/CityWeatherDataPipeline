# 阶段二 ETL 应用镜像：只装 Python 业务代码依赖，不含数据库。
# 构建上下文是仓库根目录（见 docker-compose.yaml 的 build.context）。
#
# 目的不是“把整个项目塞进镜像”，而是提供一个和 PostgreSQL 同网络的
# 客户端容器，用来验证：容器内用服务名 postgres + 5432 连接数据库。
#
# 版本固定：Python 补丁版本 + Debian 代号都写死，避免 :slim 随上游漂移。
# 升级时同步修改这里和 README 的版本表。
FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# zoneinfo 依赖系统时区数据库；slim 镜像默认没有 /usr/share/zoneinfo。
# requirements.txt 里的 tzdata 只在 Windows 安装，所以 Linux 侧单独装系统包。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层：requirements.txt 不变时复用缓存，不会每次启动都重装。
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src ./src
COPY sql ./sql

# 默认只打印帮助，避免“一启动就写库”；实际命令由 docker compose run 覆盖。
CMD ["python", "-m", "src.main", "--help"]
