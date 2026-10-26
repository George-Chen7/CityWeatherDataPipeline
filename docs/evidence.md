# 阶段二验收证据

记录时间：2026-10-26（Asia/Shanghai）。本文件只保留命令、关键日志和查询结果；完整日志在 `deploy/airflow/logs/`（已被 `.gitignore` 忽略），不提交到 Git。

## 0. 环境

| 项 | 值 |
|---|---|
| 宿主机 | Windows + Docker Desktop |
| Docker Compose | v5.1.0 |
| Airflow | 3.3.2（官方镜像 + 项目依赖） |
| Airflow 运行时 Python | 3.13.15 |
| ETL 镜像 Python | 3.12.11 |
| 天气库 / Airflow 元数据库 | PostgreSQL 16.6 |
| 宿主机 Python | 3.12.2 |
| 阶段二天气库 | `weather-pg-stage2`，宿主机 5433，容器 5432 |
| 阶段二 Airflow | `http://localhost:8080`，项目名 `city-weather-airflow` |

## 1. 服务健康与 DAG 识别

命令：

```powershell
cd deploy
docker compose ps
cd airflow
docker compose ps -a
docker compose exec airflow-scheduler airflow dags list-import-errors
```

实际结果（摘要）：

```text
weather-pg-stage2                              Up ... (healthy)   0.0.0.0:5433->5432/tcp
city-weather-airflow-postgres-1                Up ... (healthy)
city-weather-airflow-redis-1                   Up ... (healthy)
city-weather-airflow-airflow-apiserver-1       Up ... (healthy)   0.0.0.0:8080->8080/tcp
city-weather-airflow-airflow-scheduler-1       Up ... (healthy)
city-weather-airflow-airflow-dag-processor-1   Up ... (healthy)
city-weather-airflow-airflow-worker-1          Up ... (healthy)
city-weather-airflow-airflow-triggerer-1       Up ... (healthy)
city-weather-airflow-airflow-init-1            Exited (0)
```

`airflow dags list-import-errors` 输出 `No data found`；`weather_etl` 被识别。API 健康检查 `GET /api/v2/monitor/health` 返回 `200`。

## 2. 计划调度与 ETL 成功

Airflow 元数据库里的实际任务记录（UTC）：

| run_id | task | try_number | state | 时间 |
|---|---|---|---|---|
| `scheduled__2026-10-25T18:00:00+00:00` | extract | 1 | success | 08:03:50–08:03:59 |
| 同上 | transform | 1 | success | 08:03:59 |
| 同上 | load | 1 | success | 08:04:00 |
| `manual__2026-10-26T08:03:52.684470+00:00` | extract/transform/load | 1 | success | 08:04:01–08:04:04 |
| `manual__2026-10-26T08:06:18.583098+00:00` | extract/transform/load | 1 | success | 08:06:19–08:06:22 |
| `manual__2026-10-26T08:40:20.231813+00:00` | extract/transform/load | 1 | success | 08:40:20–08:40:30 |

关键日志摘录：

```text
2026-10-26T08:04:00.740869Z Load 开始 | 城市=shenzhen, hong_kong, singapore | 记录=1008
2026-10-26T08:04:00.754509Z 数据库连接成功 | postgresql://weather@weather-pg-stage2:5432/weather
2026-10-26T08:04:00.810877Z Load 完成 | 提交=1008 | 新增/更新=318 | 未变化=690
```

`transform` 输出 `输入=1008 | 输出=1008 | 剔除=0 | 质量={'valid': 1008, ...}`，说明清洗阶段没有误删数据。

## 3. 幂等性

同一区间（2026-10-13 ~ 2026-10-26，raw 回放）第二次运行：

```text
2026-10-26T08:06:22.250265Z Load 开始 | 记录=1008
2026-10-26T08:06:22.311651Z 批次 1-500   | 500 条 | 受影响 0 行
2026-10-26T08:06:22.331319Z 批次 501-1000| 500 条 | 受影响 0 行
2026-10-26T08:06:22.332368Z 批次 1001-1008| 8 条 | 受影响 0 行
2026-10-26T08:06:22.334121Z Load 完成 | 提交=1008 | 新增/更新=0 | 未变化=1008
```

最终对 2026-10-08 ~ 2026-10-21 再跑一次：

```text
2026-10-26T08:40:30.050727Z Load 完成 | 提交=1008 | 新增/更新=0 | 未变化=1008
```

最终数据库检查（阶段二 `weather-pg-stage2`）：

```text
 rows | distinct_keys |         min_ts         |         max_ts
------+---------------+------------------------+------------------------
 1368 |          1368 | 2026-10-08 00:00:00+08 | 2026-10-26 23:00:00+08

 city_key  | count
-----------+-------
 hong_kong |   456
 shenzhen  |   456
 singapore |   456

 duplicate_groups
------------------
                0

             checksum
----------------------------------
 2d60d01d6107b4d54a998d78030d5e43
```

`rows = distinct_keys` 且 `duplicate_groups = 0`，说明唯一约束和 UPSERT 没有被绕过。校验和在最后一次 `0 行受影响` 的运行后保持不变。

## 4. 失败重试与恢复（本次实际演练）

演练使用 [`../tests/airflow/retry_probe.py`](../tests/airflow/retry_probe.py)：一个不会自动加载的临时 DAG，`retries=2`、`retry_delay=10s`，靠 `data/processed/retry_probe_ready` 标记文件模拟“故障 -> 修复”。

### 4.1 故障运行

任务第一次运行 `run_id=manual__2026-10-26T08:26:07.459901+00:00`：

| 项目 | 值 |
|---|---|
| task | `flaky` |
| try_number | `3` |
| 最终状态 | `failed` |
| 三次尝试时间 | 08:26:09 / 08:26:20 / 08:26:30 UTC |

日志摘录：

```text
attempt=1 try_number=1 Task instance in failure state
Failure caused by retry_probe: 标记文件不存在，这是人为制造的预期失败

attempt=2 try_number=2 Task instance in failure state
Failure caused by retry_probe: 标记文件不存在，这是人为制造的预期失败

attempt=3 try_number=3 Task instance in failure state
Failure caused by retry_probe: 标记文件不存在，这是人为制造的预期失败
```

Airflow 元数据查询：

```text
 retry_probe | manual__2026-10-26T08:26:07.459901+00:00 | flaky | 3 | failed
```

`try_number=3` 证明前两次失败后自动重试了两次，第 3 次达到 `retries + 1` 后进入最终失败状态。

### 4.2 修复后恢复

创建标记文件 `data/processed/retry_probe_ready`，再触发一次：

```text
2026-10-26T08:27:02.372905Z retry_probe: 找到 /opt/project/data/processed/retry_probe_ready，故障已修复
2026-10-26T08:27:02.426397Z Task instance in success state
```

元数据查询：

```text
 retry_probe | manual__2026-10-26T08:27:00.876248+00:00 | flaky | 1 | success
```

演练结束后已删除 `deploy/airflow/dags/retry_probe.py` 和标记文件，并执行 `airflow dags delete retry_probe`，避免它出现在交付的 DAG 列表中；上述日志仍保留在本地 `logs/` 中作为证据。

## 5. 干净环境启动（隔离验证）

为避免影响已有阶段二数据，验收使用独立项目名、端口和卷：

| 资源 | 隔离值 |
|---|---|
| 阶段二项目 | `city-weather-clean` |
| 天气库容器 | `weather-pg-clean` |
| 宿主机端口 | `5434` |
| 天气数据卷 | `city-weather-clean-pgdata` |
| Airflow 项目 | `city-weather-airflow-clean` |
| Airflow Web | `8081` |
| Airflow 元数据卷 | `city-weather-airflow-clean_postgres-db-volume` |

干净的代码副本位于 `.tmp/clean-repo`（排除 `.git`、`.venv`、`.env`、`data/`、`logs/`、生成的 `airflow.cfg`）。

### 5.1 天气库 + ETL

```text
docker compose ... up -d postgres
-> weather-pg-clean  Up (healthy)  0.0.0.0:5434->5432/tcp

docker compose ... run --rm etl python -m src.main --cities shenzhen --start 2026-10-20 --end 2026-10-21
-> 深圳 ... 48 条 | 原始响应 /app/data/raw/shenzhen_2026-10-20_2026-10-21.json
-> 入库完成 | 城市维度=1 | 提交=48 | 新增/更新=48 | 未变化=0
-> exit=0
```

数据库：

```text
 rows | distinct_keys | min_ts                  | max_ts
------+---------------+-------------------------+-------------------------
   48 |            48 | 2026-10-20 00:00:00+08  | 2026-10-21 23:00:00+08

duplicate_groups = 0
checksum = b9e861406e8d1230e694e20d3f2f534b
```

重启持久化：

```text
docker restart weather-pg-clean
health = healthy
SELECT count(*) FROM weather_hourly;  -> 48
```

同一区间复跑：

```text
Load 完成 | 提交=48 | 新增/更新=0 | 未变化=48
本次没有任何业务字段变化 —— 幂等命中
```

### 5.2 Airflow 全链路

```text
docker compose ... build        -> exit 0
docker compose ... up -d        -> exit 0
airflow-init                    -> Exited (0)
7 个常驻服务                    -> 全部 healthy
GET http://localhost:8081/api/v2/monitor/health -> 200
```

DAG 与运行：

```text
airflow dags list -> hello_airflow, weather_etl（干净环境没有加载示例 DAG）
airflow dags list-import-errors -> No data found

scheduled__2026-10-25T18:00:00+00:00 -> success
manual__2026-10-26T08:33:24.981449+00:00 -> success
  extract  try_number=1 success
  transform try_number=1 success
  load     try_number=1 success
```

手动 run 的 `extract`/`transform`/`load` 日志：

```text
Extract 完成 | 城市=3 | 原始记录=144 | 2026-10-20 ~ 2026-10-21
Transform 完成 | 输入=144 | 输出=144 | 剔除=0
Load 完成 | 提交=144 | 新增/更新=0 | 未变化=144
```

`load` 影响 0 行是因为同一次干净启动里，前一个 scheduled run 已经写入了覆盖该区间的数据（2026-10-12 ~ 2026-10-25）；这正好再次验证了跨 run 的幂等性。

干净天气库最终状态：

```text
 rows | distinct_keys | min_ts                  | max_ts
------+---------------+-------------------------+-------------------------
 1008 |          1008 | 2026-10-12 00:00:00+08  | 2026-10-25 23:00:00+08

 hong_kong 336 / shenzhen 336 / singapore 336
duplicate_groups = 0
```

再次重启 `weather-pg-clean` 后：`healthy`，`count(*) = 1008`，重复键 `0`。

### 5.3 清理

```text
docker compose -p city-weather-airflow-clean ... down -v
docker compose -p city-weather-clean ... down -v

删除的卷：
  city-weather-airflow-clean_postgres-db-volume
  city-weather-clean-pgdata

保留的正式卷：
  city-weather-airflow_postgres-db-volume
  city-weather-stage2-pgdata
```

正式栈的 `weather-pg-stage2` 和 Airflow 服务在清理后仍为 `healthy`。

## 6. 自动化测试与静态检查

单元测试：

```text
python -m unittest discover -s tests -v
Ran 19 tests in 0.434s
OK (skipped=1)
```

数据库集成测试（独立 schema，测完删除）：

```text
$env:RUN_DB_TESTS = "1"
python -m unittest tests.test_load_integration -v
Ran 1 test in 0.382s
OK
```

静态检查：

```text
docker compose -f deploy/docker-compose.yaml config --quiet          -> exit 0
docker compose -f deploy/airflow/docker-compose.yaml config --quiet  -> exit 0
git check-ignore -v deploy/airflow/config/airflow.cfg deploy/airflow/.env deploy/.env .env
  -> 全部命中 .gitignore

docker run --rm --entrypoint sh city-weather-etl:clean -c "find /app -name '.env*' -print"
  -> 无输出

docker run --rm --entrypoint sh city-weather-airflow-clean:3.3.2 -c "python --version; airflow version"
  -> Python 3.13.15 / 3.3.2
```

`git status --short --untracked-files=all` 只包含本次交付应提交的源码、测试和文档；没有 `.env`、`airflow.cfg`、日志或数据文件。

## 7. 已知限制

- 旧 Airflow 元数据卷从 `postgres:16` 切到 `postgres:16.6` 后出现过一次 collation 版本提示：

  ```text
  database "airflow" has a collation version mismatch
  DETAIL: The database was created using collation version 2.41,
          but the operating system provides version 2.36.
  ```

  干净环境（5.2）没有这个提示。若旧环境需要消除提示，可在确认元数据可重建的前提下执行
  `ALTER DATABASE airflow REFRESH COLLATION VERSION;`，或直接重建元数据卷（会丢历史）。
- 失败演练 DAG 只在本地运行过，未提交到 `dags/`；可复现夹具在 `tests/airflow/retry_probe.py`。
- `requirements.txt` 固定直接依赖；`requests` 的传递依赖仍可能随 PyPI 漂移，未引入完整锁文件。
- 本次干净环境使用隔离端口 5434/8081；正式环境默认仍是 5433/8080。
