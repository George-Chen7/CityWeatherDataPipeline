# City Weather Data Pipeline

从 Open-Meteo 拉取深圳 / 香港 / 新加坡的逐小时天气，经 Python 清洗与质量打标后，**幂等**写入 PostgreSQL；阶段二用 Docker Compose 容器化，并用 Airflow 编排 `extract -> transform -> load`。

> 当前状态：阶段二任务 01–06 已完成。基础设施在 [`deploy/`](deploy/)；启动、端口、网络、密钥和故障演练见 [`deploy/README.md`](deploy/README.md)；真实验收记录见 [`docs/evidence.md`](docs/evidence.md)。

## 架构

```text
Open-Meteo API
      │  HTTPS（或 data/raw/*.json 离线回放）
      ▼
  extract.py ──► data/raw/<city>_<start>_<end>.json
      │
      ▼
 transform.py ──► data/processed/weather_clean.json
      │            weather_rejected.json / cleaning_report.json
      ▼
   load.py ──► PostgreSQL（cities + weather_hourly，UPSERT）

Airflow DAG weather_etl：extract ─► transform ─► load
  └─ 只负责调度/依赖/重试；业务逻辑仍在 src/
```

## 目录结构

```text
CityWeatherDataPipeline/
├── src/                 # ETL 业务代码（配置、抽取、清洗、写入、管道、CLI）
├── sql/                 # schema.sql + 5 道分析题
├── deploy/              # 阶段二：天气库 Compose、ETL 镜像、运行手册
│   └── airflow/         # Airflow Compose、Dockerfile、DAG
├── tests/               # 单元测试 + 可选数据库集成测试
│   └── airflow/         # 失败注入夹具（不会被 Airflow 自动加载）
├── docs/                # 验收证据、SQL 分析说明
├── data/                # raw / processed（不入库，保留 .gitkeep）
├── legacy/              # 阶段一原始脚本，仅作对照
├── .env.example
├── requirements.txt
└── README.md
```

## 环境要求

- Windows + Docker Desktop（Compose v2+），建议给 Docker 至少 4 GB 内存。
- PowerShell 7（示例命令使用 `pwsh` 语法）。
- 只在宿主机跑测试/CLI 时需要 Python 3.12；容器内使用固定镜像。

## 快速开始（阶段二，推荐）

### 1. 启动天气业务库

```powershell
cd deploy
Copy-Item .env.example .env
# 编辑 .env，把 POSTGRES_PASSWORD 换成长随机串
docker compose up -d
docker compose ps
```

预期 `weather-pg-stage2` 为 `Up ... (healthy)`，端口 `0.0.0.0:5433->5432/tcp`。阶段一的 `weather-pg`（5432）可以同时运行，互不影响。

### 2. 配置并启动 Airflow

```powershell
cd deploy/airflow
Copy-Item .env.example .env
```

至少填写：

- `FERNET_KEY`、`AIRFLOW__API_AUTH__JWT_SECRET`：按 `.env.example` 里的命令生成。
- `AIRFLOW_DB_PASSWORD`：Airflow 元数据库密码。
- `_AIRFLOW_WWW_USER_PASSWORD`：Web 管理员密码。
- `WEATHER_DB_PASSWORD`：**必须等于** `deploy/.env` 的 `POSTGRES_PASSWORD`。
- `WEATHER_DB_HOST=weather-pg-stage2`、`WEATHER_DB_PORT=5432`。

```powershell
docker compose build
docker compose up -d
docker compose ps -a
```

`airflow-init` 显示 `Exited (0)` 是正常的；其余 7 个服务应为 `healthy`。打开 <http://localhost:8080> 登录。

### 3. 触发天气 ETL

Web UI 中打开 `weather_etl`，Unpause，然后 Trigger：

```json
{"start": "2026-10-08", "end": "2026-10-21", "source": "raw"}
```

`source=raw` 需要 `data/raw/` 下已有同区间的文件；干净 clone 没有这些文件时，去掉 `source` 让它在线抓取：

```json
{"start": "2026-10-20", "end": "2026-10-21"}
```

也可以完全用 CLI：

```powershell
docker compose exec airflow-scheduler airflow dags unpause weather_etl
docker compose exec airflow-scheduler airflow dags trigger weather_etl --conf '{"start":"2026-10-20","end":"2026-10-21"}'
```

### 4. 验证结果

```powershell
docker exec weather-pg-stage2 psql -U weather -d weather -c @"
SELECT count(*) AS rows,
       count(DISTINCT (city_id, observed_at)) AS distinct_keys,
       min(observed_at) AS min_ts,
       max(observed_at) AS max_ts
FROM weather_hourly;
"@
```

`rows` 应等于 `distinct_keys`；重复键检查应为 0 行：

```powershell
docker exec weather-pg-stage2 psql -U weather -d weather -c @"
SELECT city_id, observed_at, count(*)
FROM weather_hourly
GROUP BY city_id, observed_at
HAVING count(*) > 1;
"@
```

更完整的启动、备份、排错命令见 [`deploy/README.md`](deploy/README.md)。

## 干净环境验收清单

| # | 检查 | 通过标准 |
|---|---|---|
| 1 | `docker compose ps`（deploy） | 天气库 `healthy`，端口 5433 |
| 2 | `docker compose ps`（airflow） | 7 个服务 `healthy`，init `Exited (0)` |
| 3 | Airflow UI / `airflow dags list` | 能看到 `weather_etl`，无 import error |
| 4 | 触发 DAG | `extract / transform / load` 三个 task 全部 `success` |
| 5 | 数据库 | `rows = distinct_keys`，无重复业务键 |
| 6 | 同一区间再跑 | `新增/更新=0`，`未变化=行数` |
| 7 | `docker restart weather-pg-stage2` | 重启后行数不变 |
| 8 | 失败演练 | `try_number` 递增到 `retries+1` 后 `failed`；修复后重跑 `success` |
| 9 | 密钥检查 | `git check-ignore` 命中 `.env` 与生成的 `airflow.cfg` |

本次实际执行记录（含日志摘录和查询结果）在 [`docs/evidence.md`](docs/evidence.md)。

## 自动化测试

```powershell
# 单元测试：配置校验、四态清洗、管道交接、重复键
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

预期：19 个测试通过，数据库集成测试默认跳过（`skipped=1`）。

数据库集成测试会在独立 schema 中建表、写入两次并断言第二次影响 0 行，结束后删除 schema。先设置与阶段二一致的 `PG*`，再运行：

```powershell
$env:RUN_DB_TESTS = "1"
$env:PGHOST = "127.0.0.1"
$env:PGPORT = "5433"
$env:PGDATABASE = "weather"
$env:PGUSER = "weather"
$env:PGPASSWORD = "<deploy/.env 里的 POSTGRES_PASSWORD>"

.\.venv\Scripts\python.exe -m unittest tests.test_load_integration -v
```

## 阶段一：宿主机运行（保留）

阶段一仍可直接在宿主机跑，默认连接 `127.0.0.1:5432` 的 `weather-pg`；根目录 `.env` 默认就是这套配置。

```powershell
# 数据库（仅首次）
docker run -d --name weather-pg `
  -e POSTGRES_USER=weather `
  -e POSTGRES_PASSWORD=weather `
  -e POSTGRES_DB=weather `
  -p 5432:5432 `
  -v weather_pgdata:/var/lib/postgresql/data `
  postgres:16.6

# 虚拟环境（仅首次）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 运行
.\.venv\Scripts\python.exe -m src.main

# 指定城市/区间；dry-run 只清洗不写库；from-raw 离线回放
.\.venv\Scripts\python.exe -m src.main --cities shenzhen hong_kong --start 2026-10-08 --end 2026-10-21
.\.venv\Scripts\python.exe -m src.main --cities singapore --start 2026-10-01 --end 2026-10-14 --dry-run
.\.venv\Scripts\python.exe -m src.main --start 2026-10-08 --end 2026-10-21 --from-raw
```

退出码：`0` 全部成功；`1` 配置/数据库致命错误；`2` 部分城市抽取失败但其余已入库。

## 幂等性设计

| 机制 | 作用 |
|---|---|
| `UNIQUE (city_id, observed_at)` | 同一城市同一观测时刻只能有一行 |
| `ON CONFLICT ... DO UPDATE ... WHERE 业务列 IS DISTINCT FROM ...` | 数据无变化时不执行 UPDATE，`updated_at` 不被刷新 |
| 不执行 `DELETE` / `TRUNCATE` | 重复运行只 upsert，不会清空重灌 |
| 维度表按 `city_key` upsert | 城市 ID 稳定，配置改名不会产生新维度行 |
| 整批写入包在一个事务里 | 失败回滚，不会留下半批数据 |

已实测（2026-10-08 ~ 2026-10-21，三城市 1008 行）：

```text
第一次：提交 1008 | 新增/更新 960 | 未变化 48
第二次：提交 1008 | 新增/更新 0   | 未变化 1008
重复键检查：0 行
业务列 checksum：b3d48bc52e4649e0e1a4eed9e165c9e0（两次一致）
max(updated_at)：两次一致
```

阶段二 Airflow 日志中的同区间复跑同样是 `新增/更新=0 | 未变化=1008`，见 [`docs/evidence.md`](docs/evidence.md)。

## Airflow 编排

[`deploy/airflow/dags/weather_etl.py`](deploy/airflow/dags/weather_etl.py)：

| 任务 | 作用 | 产物 | retries |
|---|---|---|---|
| `extract` | 抓取/回放原始 JSON | `data/raw/*.json` | 3 |
| `transform` | 清洗、打标、去重 | `data/processed/weather_clean.json` | 1 |
| `load` | 幂等 UPSERT | `cities`、`weather_hourly` | 2 |

- 任务之间只通过 XCom 传 manifest（城市、区间、产物路径），不传 1008 条记录。
- `max_active_runs=1`，避免两个 run 同时写同一份 `weather_clean.json`。
- `catchup=False`，默认按 `data_interval_end - 1 天` 往前取 14 天；手动触发可用 `conf.start` / `conf.end` 覆盖。
- 业务异常必须抛出，不能返回退出码，否则 Airflow 会误判成功、不重试。

## 表结构

- `cities`：`city_id`、`city_key`（唯一）、`city_name`、`latitude`、`longitude`、`timezone`、`created_at`
- `weather_hourly`：`id`、`city_id`（外键）、`observed_at`（UTC）、`temperature_c`、`precipitation_mm`、`wind_speed_kmh`、`quality`、`issues(jsonb)`、`created_at`、`updated_at`

`quality` 四态：`valid` / `missing` / `suspect` / `invalid`。异常数据不删除，只打标签；`invalid` 记录允许保留负值等物理越界值，便于排查。

完整 DDL 见 [`sql/schema.sql`](sql/schema.sql)。数据库初始化由 `load.ensure_schema()` 在每次写入前幂等执行，不依赖手工建表。

## SQL 分析

5 道题各自一个 `.sql` 文件，统一口径：只统计 `quality IN ('valid','suspect')` 且数值非空的记录；`observed_at` 存 UTC，按当地日历聚合时用 `(observed_at AT TIME ZONE c.timezone)::date`。

| # | 文件 | 结果摘要 |
|---|---|---|
| 1 | [`sql/city_summary.sql`](sql/city_summary.sql) | 新加坡均温 27.93℃ 最高，深圳 26.61℃ 最低；三城最高温都在 33–34℃ |
| 2 | [`sql/daily_precipitation.sql`](sql/daily_precipitation.sql) | 新加坡 14 天累计 77.0 mm，单日最多 11.9 mm；深圳/香港同期明显偏干 |
| 3 | [`sql/hottest_periods.sql`](sql/hottest_periods.sql) | 最高温时段集中在当地 12:00–15:00；深圳/香港峰值都在 10-21 |
| 4 | [`sql/temperature_ranking.sql`](sql/temperature_ranking.sql) | 按均温排名：新加坡 > 香港 > 深圳，三城样本量都是 336 |
| 5 | [`sql/rolling_average.sql`](sql/rolling_average.sql) | 7 小时滚动平均体现低通滤波的滞后；窗口按行数开，缺测时应改 `RANGE` |

完整的逐题结果、解释和实现细节见 [`docs/sql-analysis.md`](docs/sql-analysis.md)。这些结果基于 2026-10-08 ~ 2026-10-21 的 1008 行数据；当前阶段二库已扩展到 1368 行（到 2026-10-26），重新跑 SQL 会得到更长区间的结果。

## 密钥与配置

| 文件 | 是否入库 | 用途 |
|---|---|---|
| `.env.example` | 是 | 宿主机 Python 连接模板 |
| `deploy/.env.example` | 是 | 天气库 Compose 模板 |
| `deploy/airflow/.env.example` | 是 | Airflow 密钥/管理员/连接模板 |
| `.env`、`deploy/.env`、`deploy/airflow/.env` | 否 | 真实配置，`.gitignore` 忽略 |
| `deploy/airflow/config/airflow.cfg` | 否 | Airflow 运行时生成，含 `secret_key` / `jwt_secret` |

安全检查：

```powershell
git check-ignore -v .env deploy/.env deploy/airflow/.env deploy/airflow/config/airflow.cfg
git status --short --untracked-files=all
```

`git status` 不应出现任何 `.env` 或 `airflow.cfg`。

## 版本固定

| 组件 | 版本 | 位置 |
|---|---|---|
| Airflow | `3.3.2` | `deploy/airflow/Dockerfile` |
| 天气库 PostgreSQL | `16.6` | `deploy/docker-compose.yaml` |
| Airflow 元数据库 PostgreSQL | `16.6` | `deploy/airflow/docker-compose.yaml` |
| ETL Python | `3.12.11-slim-bookworm` | `deploy/Dockerfile` |
| Python 依赖 | `==` 精确版本 | `requirements.txt` |

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `dockerDesktopLinuxEngine` 权限错误 | Docker Desktop 没启动完 | 启动 Docker Desktop，等引擎就绪 |
| 宿主机 `could not translate host name "postgres"` | 服务名只在 Docker 网络内有效 | 宿主机用 `127.0.0.1` + `5433` |
| 容器内连 `127.0.0.1:5432` 失败 | 容器 loopback 不是宿主机 | 用 `PGHOST=postgres` |
| Airflow 报 external network 不存在 | 先启动了 Airflow | 先 `cd deploy; docker compose up -d` |
| DAG 不出现 | DAG Processor 未刷新 / import error | `airflow dags list-import-errors`，看 `airflow-dag-processor` 日志 |
| 任务一直 `queued` | worker/redis 不健康 | `docker compose ps`、`docker compose logs airflow-worker` |
| 改密码后认证失败 | `POSTGRES_*` 只在空卷初始化时生效 | `ALTER USER` 或删卷重建（会丢数据） |
| 改了 `requirements.txt` 但容器没生效 | 镜像没重建 | `docker compose build` 后 `up -d` |

更多排错见 [`deploy/README.md`](deploy/README.md)。

## 已知限制

- 本地学习环境：CeleryExecutor + 单 worker，无高可用、TLS、外部 secrets、自动备份。
- `schema.sql` 只有 `CREATE TABLE IF NOT EXISTS`，不做列迁移；改表结构需手写 `ALTER TABLE` 或引入迁移工具。
- 不做水位线增量同步，日期范围由 `--start/--end` 或 DAG conf 显式指定；不同区间是叠加而非替换。
- `data/raw`、`data/processed` 不入库；干净 clone 首次运行需联网，或先准备同区间 raw 文件。
- 根目录 `.env` 默认指向阶段一 `5432`；阶段二宿主机验证需用进程环境变量覆盖 `PGPORT=5433`。
- Redis 固定到 `7.2-bookworm` 系列，补丁版本仍可能随上游漂移。
- `requirements.txt` 固定直接依赖；`requests` 的传递依赖（`certifi`、`urllib3` 等）仍可能漂移，暂未引入 `pip-compile` 锁文件。
- 使用 forecast 接口：最多回看约 3 个月、预报 16 天；更早日期需改用 `archive-api.open-meteo.com`。
- 旧 Airflow 元数据卷从 `postgres:16` 升级到 `postgres:16.6` 后可能出现 collation 版本提示；干净环境不受影响，处理方式见 [`deploy/README.md`](deploy/README.md)。

## 文档索引

- [`deploy/README.md`](deploy/README.md)：阶段二启动、网络、密钥、运维、排错
- [`docs/evidence.md`](docs/evidence.md)：阶段二验收日志与数据库证据
- [`docs/sql-analysis.md`](docs/sql-analysis.md)：5 道 SQL 题的完整结果与解释
