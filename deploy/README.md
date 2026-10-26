# 阶段二：Docker Compose + Airflow 运维手册

阶段二在 `deploy/` 下维护一套**独立**环境，不与阶段一的 `weather-pg` 混用：

- 阶段一：宿主机 `5432`，容器 `weather-pg`，数据卷 `weather_pgdata`。
- 阶段二：宿主机 `5433`，容器 `weather-pg-stage2`，数据卷 `city-weather-stage2-pgdata`。
- Airflow：Web `8080`，元数据库容器 `city-weather-airflow-postgres-1`，元数据卷由 Compose 项目名管理。

两套 PostgreSQL、两个数据卷互不影响，可以在同一台机器上并存。

## 1. 组件与文件

| 文件 / 服务 | 用途 |
|---|---|
| [`docker-compose.yaml`](docker-compose.yaml) | 天气业务库 `postgres` + 一次性客户端 `etl`（profile） |
| [`Dockerfile`](Dockerfile) | `etl` 镜像：`python:3.12.11-slim-bookworm` + 精确固定的依赖 |
| [`.env.example`](.env.example) | 天气库、宿主机端口、时区模板 |
| [`airflow/docker-compose.yaml`](airflow/docker-compose.yaml) | Airflow 3.3.2：api-server、scheduler、dag-processor、worker、triggerer、init、redis、元数据库 |
| [`airflow/Dockerfile`](airflow/Dockerfile) | 在官方镜像上追加项目 Python 依赖 |
| [`airflow/.env.example`](airflow/.env.example) | Airflow 密钥、管理员、元数据库、天气库连接模板 |
| [`airflow/dags/weather_etl.py`](airflow/dags/weather_etl.py) | `extract -> transform -> load` DAG |
| [`../.dockerignore`](../.dockerignore) | 构建上下文排除 `.env`、`.venv`、`data/`、`logs/` 等 |
| [`../.gitignore`](../.gitignore) | 忽略 `.env`、Airflow 生成的 `config/airflow.cfg`、日志、数据 |

### 服务清单（Airflow 栈）

| 服务 | 作用 | 挂了会怎样 |
|---|---|---|
| `postgres` | Airflow 元数据库 | 全站不可用 |
| `redis` | Celery broker | 任务一直 `queued` |
| `airflow-apiserver` | UI + REST API + Execution API | 打不开 `8080`，worker 无法回写状态 |
| `airflow-scheduler` | 生成 DagRun、派发 TaskInstance | 到点不跑 |
| `airflow-dag-processor` | 解析 `dags/*.py` → serialized DAG | 新 DAG 不出现、改代码不生效 |
| `airflow-worker` | 从 Redis 取任务并执行 | 任务卡 `queued` |
| `airflow-triggerer` | deferrable 任务的 async 循环 | sensor 类任务卡住 |
| `airflow-init` | 迁移元数据库 + 建管理员，跑完退出 | 其他服务不启动 |

`airflow-init` 是 one-shot 容器，`docker compose ps -a` 里显示 `Exited (0)` 是正确的；`docker compose ps` 默认不显示已退出容器。

## 2. 首次启动（按顺序）

### 2.1 天气业务库

```powershell
cd deploy
Copy-Item .env.example .env
# 编辑 .env，把 POSTGRES_PASSWORD 换成长随机串（只使用字母、数字、-、_）
docker compose up -d
docker compose ps
```

预期：

```text
NAME                 ... STATUS
weather-pg-stage2    ... Up ... (healthy)
```

端口应为 `0.0.0.0:5433->5432/tcp`。健康检查通过才说明 PostgreSQL 能接受连接，而不是“进程起来了”。

### 2.2 Airflow 环境变量

```powershell
cd deploy/airflow
Copy-Item .env.example .env
```

至少填写以下值：

| 变量 | 说明 |
|---|---|
| `FERNET_KEY` | 32 字节 urlsafe-base64；用于加密连接密码/Variable |
| `AIRFLOW__API_AUTH__JWT_SECRET` | 长随机串 |
| `_AIRFLOW_WWW_USER_PASSWORD` | 首次 init 创建的管理员密码 |
| `AIRFLOW_DB_PASSWORD` | Airflow 元数据库密码；只使用字母、数字、`-`、`_` |
| `WEATHER_DB_PASSWORD` | **必须等于** `deploy/.env` 的 `POSTGRES_PASSWORD` |
| `WEATHER_DB_HOST` | 默认 `weather-pg-stage2`（容器名，不是 `localhost`） |
| `WEATHER_DB_PORT` | 容器内端口 `5432`（不是宿主机 `5433`） |

生成密钥（PowerShell）：

```powershell
python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"   # FERNET_KEY
python -c "import secrets; print(secrets.token_urlsafe(64))"                           # JWT / 密码
```

> `AIRFLOW_DB_PASSWORD` 会拼进 SQLAlchemy URL，避免使用 `@ : / # %` 等字符。
> 如果已经初始化过元数据卷，再改这个密码**不会**修改数据库里已存在的角色密码，见第 7 节。

### 2.3 构建并启动 Airflow

```powershell
docker compose build
docker compose up -d
docker compose ps -a
```

第一次启动 `airflow-init` 会执行 `airflow db migrate` 并创建管理员，通常需要 30–90 秒。等到以下服务都 `healthy`：

- `airflow-apiserver`
- `airflow-scheduler`
- `airflow-dag-processor`
- `airflow-worker`
- `airflow-triggerer`
- `postgres`
- `redis`

然后打开 <http://localhost:8080>，用 `.env` 里的 `_AIRFLOW_WWW_USER_USERNAME` / `_AIRFLOW_WWW_USER_PASSWORD` 登录。

> `WEATHER_DOCKER_NETWORK` 默认是 `city-weather-stage2_default`，由 2.1 创建。
> **必须先启动天气库，再启动 Airflow**；否则 Airflow 会报 external network 不存在。

## 3. 运行天气 ETL

DAG 创建后默认是暂停状态（`AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=true`）。两种触发方式：

### 3.1 Web UI

1. 打开 `weather_etl`，点 Unpause。
2. Trigger DAG，Conf 留空则按 data interval 取“昨天往前 14 天”。
3. 手动指定区间时，`start` 和 `end` 必须同时提供：

```json
{"start": "2026-10-08", "end": "2026-10-21", "cities": ["shenzhen", "hong_kong", "singapore"]}
```

不访问网络、回放已有的 `data/raw/` 文件：

```json
{"start": "2026-10-08", "end": "2026-10-21", "source": "raw"}
```

### 3.2 CLI

```powershell
docker compose exec airflow-scheduler airflow dags unpause weather_etl
docker compose exec airflow-scheduler airflow dags trigger weather_etl --conf '{"start":"2026-10-08","end":"2026-10-21","source":"raw"}'
docker compose exec airflow-scheduler airflow dags list-runs weather_etl
```

查看任务状态：

```powershell
docker compose exec airflow-scheduler airflow tasks states-for-dag-run weather_etl <run_id>
```

常用排错命令：

```powershell
docker compose logs -f airflow-scheduler
docker compose logs -f airflow-worker
docker compose exec airflow-scheduler airflow dags list-import-errors
docker compose exec airflow-scheduler airflow tasks test weather_etl load 2026-10-21
```

## 4. 验证数据库

在 `deploy/` 目录执行，或从项目根目录用 `docker exec`：

```powershell
docker exec weather-pg-stage2 psql -U weather -d weather -c @"
SELECT count(*) AS rows,
       count(DISTINCT (city_id, observed_at)) AS distinct_keys,
       min(observed_at) AS min_ts,
       max(observed_at) AS max_ts
FROM weather_hourly;
"@

docker exec weather-pg-stage2 psql -U weather -d weather -c @"
SELECT c.city_key, count(*) AS rows
FROM weather_hourly w
JOIN cities c USING (city_id)
GROUP BY c.city_key
ORDER BY c.city_key;
"@
```

预期：`rows = distinct_keys`，没有重复业务键；每个城市的行数等于该城市覆盖的小时数。

检查重复键（应为 0 行）：

```powershell
docker exec weather-pg-stage2 psql -U weather -d weather -c @"
SELECT city_id, observed_at, count(*)
FROM weather_hourly
GROUP BY city_id, observed_at
HAVING count(*) > 1;
"@
```

### 4.1 幂等性复跑

对同一区间再触发一次，日志里的 `Load 完成` 应为：

```text
提交=1008 | 新增/更新=0 | 未变化=1008
```

`updated_at` 不会被刷新，因为 `ON CONFLICT ... DO UPDATE ... WHERE 业务列 IS DISTINCT FROM ...` 在第一轮之后不满足更新条件。实测见 [`../docs/evidence.md`](../docs/evidence.md)。

### 4.2 重启持久化

```powershell
docker restart weather-pg-stage2
# 等 healthy 后再查
docker exec weather-pg-stage2 psql -U weather -d weather -c "SELECT count(*) FROM weather_hourly;"
```

`restart` 复用同一个容器和同一个命名卷；`down` 只删容器保留卷；`down -v` 才会删卷并丢数据。

## 5. 失败重试与恢复

`weather_etl` 的默认参数：

| 任务 | retries | retry_delay | 说明 |
|---|---|---|---|
| `extract` | 3 | 2 分钟，指数退避，上限 10 分钟 | 网络/API 抖动 |
| `transform` | 1 | 2 分钟 | 文件交接/解析问题 |
| `load` | 2 | 2 分钟 | 数据库短暂不可用 |

任务失败后，Airflow 会按 `try_number` 自动重试；达到 `retries + 1` 次后状态变为 `failed`。修复原因后，可在 UI 上 Clear 该 task（或整个 run）重新执行；`load` 的 UPSERT 保证重复执行不会产生重复记录。

可复现的失败演练脚本在 [`../tests/airflow/retry_probe.py`](../tests/airflow/retry_probe.py)，它是测试夹具，不会被 Airflow 自动加载。演练记录见 [`../docs/evidence.md`](../docs/evidence.md)。

## 6. 网络、端口与 `.env` 分层

Compose 会创建以项目名命名的 bridge 网络：

```text
city-weather-stage2_default
```

同一网络里的服务可以用**服务名**互相访问。`postgres` 服务的 DNS 别名包括
`postgres`、`weather-pg-stage2` 和容器短 ID，由 Docker 内置 DNS 解析：

```text
Windows 宿主机
  └─ 127.0.0.1:5433
        │  Docker 端口映射 5433 -> 5432
        ▼
  Docker 网络 city-weather-stage2_default
     ├─ postgres 容器（服务名 postgres，容器内监听 5432）
     ├─ etl 容器     （PGHOST=postgres，PGPORT=5432）
     └─ airflow-worker（加入同一网络，PGHOST=weather-pg-stage2）
```

三个容易混淆的点：

1. **`localhost` / `127.0.0.1` 在容器里指的是容器自己**，不是宿主机，也不是 `postgres` 容器。
2. **服务名只在 Docker 网络内有效**。宿主机必须走 `127.0.0.1:5433`。
3. **`depends_on` 不等于数据库已就绪**；所以 `etl` 使用 `condition: service_healthy`。

项目里有三个 `.env`，职责不同：

| 文件 | 谁读取 | 内容 |
|---|---|---|
| `deploy/.env` | 天气库 Compose | `POSTGRES_*`、`POSTGRES_HOST_PORT`、`TZ` |
| 根目录 `.env` | 宿主机上的 Python（`src/config.py`） | `PGHOST`、`PGPORT`、`PGDATABASE`、`PGUSER`、`PGPASSWORD` |
| `deploy/airflow/.env` | Airflow Compose | `FERNET_KEY`、管理员、元数据库、`WEATHER_DB_*` |

`etl` 容器不挂载 `.env`，Compose 只把需要的变量显式注入容器。这样同一份 `src/` 代码只认 `PG*`：

- 容器里：`PGHOST=postgres`、`PGPORT=5432`
- 宿主机：`PGHOST=127.0.0.1`、`PGPORT=5433`
- Airflow worker：`PGHOST=weather-pg-stage2`、`PGPORT=5432`

宿主机上临时连阶段二（不改根 `.env`）：

```powershell
$cfg = @{}
Get-Content .\deploy\.env | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_]\w*)\s*=\s*(.*)\s*$') { $cfg[$matches[1]] = $matches[2].Trim() }
}
$env:PGHOST = '127.0.0.1'
$env:PGPORT = $cfg['POSTGRES_HOST_PORT']
$env:PGDATABASE = $cfg['POSTGRES_DB']
$env:PGUSER = $cfg['POSTGRES_USER']
$env:PGPASSWORD = $cfg['POSTGRES_PASSWORD']

.\.venv\Scripts\python.exe -c 'from src.load import connect; c=connect(); cur=c.cursor(); cur.execute("SELECT current_database()"); print(cur.fetchone()); c.close()'
```

## 7. 停止、清理与备份

| 操作 | 命令 | 数据 |
|---|---|---|
| 停止天气库 | `cd deploy; docker compose down` | 保留命名卷 |
| 停止 Airflow | `cd deploy/airflow; docker compose down` | 保留元数据卷 |
| 删除天气库卷 | `docker compose down -v` | **会丢数据**，先备份 |
| 删除 Airflow 元数据卷 | `docker compose down -v` | **会丢 DAG 运行历史/用户** |
| 查看卷 | `docker volume ls | Select-String 'city-weather'` | |

备份天气库：

```powershell
docker exec weather-pg-stage2 pg_dump -U weather -d weather -Fc > .\weather.dump
```

恢复（目标库为空或确认可覆盖时）：

```powershell
Get-Content .\weather.dump -AsByteStream -Raw |
    docker exec -i weather-pg-stage2 pg_restore -U weather -d weather --clean --if-exists
```

> `pg_restore --clean` 会删除已有对象，属于破坏性操作；先确认目标实例和备份文件。

### 7.1 修改密码后认证失败

`POSTGRES_*` 只在数据卷为空、首次初始化时生效。之后修改 `deploy/.env` 或 `deploy/airflow/.env` 里的密码，不会自动修改数据库角色。可选：

```powershell
# 修改已有天气库角色密码（示例，执行前确认目标实例）
docker exec -it weather-pg-stage2 psql -U weather -d weather -c "ALTER USER weather WITH PASSWORD '新密码';"
```

然后同步修改 `deploy/.env` 的 `POSTGRES_PASSWORD` 和 `deploy/airflow/.env` 的 `WEATHER_DB_PASSWORD`，再重启 Airflow worker：

```powershell
cd deploy/airflow
docker compose up -d --force-recreate airflow-worker airflow-cli
```

Airflow 元数据库同理；如果不想保留历史，可以 `down -v` 后重建（会丢元数据）。

### 7.2 从旧配置升级：示例 DAG 残留

如果旧元数据卷曾经用 `AIRFLOW__CORE__LOAD_EXAMPLES=true` 启动过，后来改成 `false`，
`serialized_dag` 表里仍会保留示例 DAG 的序列化记录。CLI 可能出现：

```text
TimetableNotRegistered: Timetable class
'airflow.example_dags.plugins.workday.AfterWorkdayTimetable' is not registered
```

原因不是天气 DAG 坏了，而是示例 DAG 的 timetable 插件没有随示例一起加载。两条路：

1. **不清理旧元数据**：在 `deploy/airflow/.env` 里保留 `AIRFLOW__CORE__LOAD_EXAMPLES=true`，
   让示例插件继续注册。
2. **清理示例**：确认不再需要示例 DAG 的运行历史后，用 Airflow CLI 删除示例 DAG，
   再把 `AIRFLOW__CORE__LOAD_EXAMPLES` 改为 `false`。删除前先备份元数据库：

   ```powershell
   docker exec city-weather-airflow-postgres-1 pg_dump -U airflow -d airflow -Fc > .\airflow-metadata.dump
   docker exec city-weather-airflow-airflow-scheduler-1 airflow dags list --output plain
   # 按 dag_id 逐个删除示例（示例 DAG 的 dag_id 不在 weather_etl / hello_airflow 里）
   ```

干净环境不会遇到这个问题；`deploy/airflow/.env.example` 默认就是 `false`。

## 8. 版本固定与安全

| 组件 | 固定值 | 位置 |
|---|---|---|
| Airflow | `apache/airflow:3.3.2` | `airflow/Dockerfile` |
| Airflow 自定义镜像 | `city-weather-airflow:3.3.2` | `airflow/.env.example` |
| 天气库 | `postgres:16.6` | `docker-compose.yaml` |
| Airflow 元数据库 | `postgres:16.6` | `airflow/docker-compose.yaml` |
| Redis | `redis:7.2-bookworm` | `airflow/docker-compose.yaml` |
| ETL Python | `python:3.12.11-slim-bookworm` | `Dockerfile` |
| Python 依赖 | `==` 精确版本 | 根目录 `requirements.txt` |

Airflow 3.3.2 的 Python 版本由官方镜像决定（当前为 3.13）；`requirements.txt` 在镜像构建时安装，版本与宿主机 `.venv` 一致。

安全边界：

- `.env` 在任意层级被 `.gitignore` 忽略；`.env.example` 保留。
- Airflow 运行时生成的 `deploy/airflow/config/airflow.cfg` 含 `secret_key` / `jwt_secret`，已整体忽略，只保留 `.gitkeep`。
- `.dockerignore` 排除 `.env`、`**/.env`、`.venv`、`data/`、`logs/`，镜像里没有密钥。
- `DatabaseConfig.endpoint` 只拼接 `user@host:port/dbname`，日志不打印密码。
- **不要**分享 `docker compose config` 的输出，它会把 `${...}` 展开成真实值。
- 本地 Compose 仍通过环境变量传密码，拥有 Docker 权限的人可以用 `docker inspect` 看到；生产环境应使用 Docker secrets / 外部密钥管理。

## 9. 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| `permission denied ... dockerDesktopLinuxEngine` | Docker Desktop 没启动完 | 启动 Docker Desktop，等引擎就绪 |
| `external network ... not found` | 先启动了 Airflow | 先 `cd deploy; docker compose up -d` |
| 宿主机 `could not translate host name "postgres"` | 服务名只在 Docker 网络内有效 | 宿主机用 `127.0.0.1` + `POSTGRES_HOST_PORT` |
| 容器内 `connection refused` 到 `127.0.0.1` | 容器 loopback 不是宿主机 | 用 `PGHOST=postgres` |
| 宿主机 `5432` 密码认证失败 | 连到阶段一容器 | 阶段二用 `5433` |
| `port is already allocated` | 宿主机端口被占用 | 改 `POSTGRES_HOST_PORT` / `AIRFLOW_WEB_PORT`，同步改根 `.env` |
| `FERNET_KEY is required` | `airflow/.env` 没填 | 用 `.env.example` 生成并填写 |
| `WEATHER_DB_PASSWORD is required` | 没填或空值 | 填写与 `deploy/.env` 一致的密码 |
| `password authentication failed`（Airflow worker） | 两处密码不一致 / 改密码后没改角色 | 对齐 `WEATHER_DB_PASSWORD` 与 `POSTGRES_PASSWORD`，必要时 `ALTER USER` |
| DAG 不出现 | DAG Processor 未刷新 / import error | `airflow dags list-import-errors`，看 `airflow-dag-processor` 日志 |
| 任务一直 `queued` | worker/redis 不健康 | `docker compose ps`、`docker compose logs airflow-worker` |
| `database "airflow" has a collation version mismatch` | 旧元数据卷与升级后的 `postgres:16.6` 排序规则库版本不同 | 干净环境不受影响；旧环境可 `ALTER DATABASE airflow REFRESH COLLATION VERSION;`，或重建元数据卷（会丢历史） |
| `Task exited with return code 1` 且日志有 `无法连接数据库` | 天气库停了或网络不对 | 起天气库，确认 worker 在 `weather` 网络 |
| 改了 `requirements.txt` 但容器里没生效 | 镜像没重建 | `docker compose build` 后 `up -d` |

## 10. 已知限制

- 这是本地学习环境：CeleryExecutor + 单 worker，未做高可用、外部 secrets、TLS、备份自动化。
- Airflow 元数据库和天气业务库是两套独立的 PostgreSQL；不要把它们当成同一个库。
- `schema.sql` 只有 `CREATE TABLE IF NOT EXISTS`，**不做列迁移**；改表结构需要手写 `ALTER TABLE` 或引入迁移工具。
- 数据区间由 DAG conf 或 data interval 决定，不做水位线增量同步；不同区间是叠加而非替换。
- `data/raw` 和 `data/processed` 不入库；干净 clone 首次运行需要联网抓取，或先准备同区间的 raw 文件再 `source=raw`。
- 宿主机根 `.env` 默认指向阶段一 `5432`；阶段二宿主机验证请用第 6 节的进程环境变量覆盖。
- Redis 只固定到 `7.2-bookworm` 系列，补丁版本仍可能随上游漂移；升级前先在本地重建并跑一次 ETL。
- `requirements.txt` 固定的是直接依赖；`requests` 的 `certifi` / `urllib3` 等传递依赖仍可能随 PyPI 漂移，本项目暂未引入 `pip-compile` 锁文件。
- 详细验收记录与日志摘录见 [`../docs/evidence.md`](../docs/evidence.md)。
