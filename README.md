# City Weather Data Pipeline

从 Open-Meteo 拉取深圳 / 香港 / 新加坡的逐小时天气，经 Python 清洗与质量打标后，**幂等**写入 PostgreSQL，为后续 SQL 分析提供数据底座。

## 目录结构

```
CityWeatherDataPipeline/
├── src/
│   ├── config.py       # 城市配置、路径、.env 读取与校验
│   ├── extract.py      # Open-Meteo 请求、重试、原始响应落盘、离线回放
│   ├── transform.py    # 四态质量标签、时间归一化、去重、清洗报告
│   ├── load.py         # 连接管理、幂等建表、维度/事实 upsert
│   └── main.py         # argparse + 流程串联 + 退出码
├── sql/
│   └── schema.sql      # 幂等 DDL，与数据库现有结构一致
├── data/
│   ├── raw/            # 原始 API 响应，按 城市_开始_结束.json 命名
│   └── processed/      # weather_clean.json / weather_rejected.json / cleaning_report.json
├── legacy/             # Task 1/2 的原始脚本，仅作对照，不参与运行
├── tests/              # 自动化测试（下一步补充）
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## 环境准备

```powershell
# 1) 虚拟环境（宿主机，不是 Docker）
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2) 数据库（Docker）
docker run -d --name weather-pg `
  -e POSTGRES_USER=weather `
  -e POSTGRES_PASSWORD=weather `
  -e POSTGRES_DB=weather `
  -p 5432:5432 `
  -v weather_pgdata:/var/lib/postgresql/data `
  postgres:16

# 3) 配置连接
Copy-Item .env.example .env
# 修改 .env 中的 PGPASSWORD 等字段；.env 已被 .gitignore 忽略
```

`.env` 字段：`PGHOST`、`PGPORT`、`PGDATABASE`、`PGUSER`、`PGPASSWORD`、`PGCONNECT_TIMEOUT`。
缺少任一必填项时程序会一次性列出变量名，不会静默使用默认连接。

## 每次启动（日常流程）

初始化过一次之后，**不需要**再 `docker run`、建 venv 或改 `.env`：

```powershell
# 1) 启动 Docker Desktop（若未启动），确认容器状态
docker ps --filter name=weather-pg
#    没有输出 => 容器停了：
docker start weather-pg

# 2) 进入项目根目录（python -m src.main 依赖这个路径）
cd C:\Users\Lenovo\Desktop\code\learning\Data\CityWeatherDataPipeline

# 3a) 直接调用 venv 里的解释器（推荐，绕过 PowerShell 执行策略）
.\.venv\Scripts\python.exe -m src.main

# 3b) 或者先激活再运行
.\.venv\Scripts\Activate.ps1
python -m src.main
```

收工时可停容器，数据在 `weather_pgdata` 卷里不会丢：`docker stop weather-pg`。

只有这几种情况才需要重装依赖（`.\.venv\Scripts\python.exe -m pip install -r requirements.txt`）：
`requirements.txt` 变了、`.venv` 被删了、换了机器。

## 常见启动错误

| 现象 | 原因 | 处理 |
|---|---|---|
| `permission denied ... dockerDesktopLinuxEngine` | Docker Desktop 没启动完 | 启动 Docker Desktop，等引擎就绪再重试 |
| `无法连接数据库 ... Connection refused / timeout` | 容器没运行，或端口不对 | `docker ps -a --filter name=weather-pg`，再 `docker start weather-pg` |
| `No module named src` | 不在项目根目录 | `cd` 到项目根后再执行 `python -m src.main` |
| `Activate.ps1 cannot be loaded` | PowerShell 执行策略限制 | 直接用 `.\.venv\Scripts\python.exe -m src.main` |
| 端口 5432 被占用 | 其他 PostgreSQL/容器 | 改映射端口并同步改 `.env` 的 `PGPORT` |

容器默认不会随 Docker Desktop 自启动；想省掉 `docker start` 可以执行一次：
`docker update --restart unless-stopped weather-pg`

## 运行

```powershell
# 默认：三个城市，最近 14 天（含今天），抓取 + 入库
python -m src.main

# 指定城市与日期范围
python -m src.main --cities shenzhen hong_kong --start 2026-10-08 --end 2026-10-21

# 只抽取 + 清洗，不写数据库
python -m src.main --cities singapore --start 2026-10-01 --end 2026-10-14 --dry-run

# 不访问网络，回放 data/raw 下同区间的原始响应
python -m src.main --start 2026-10-08 --end 2026-10-21 --from-raw
```

退出码：`0` 全部成功；`1` 配置/数据库致命错误；`2` 部分城市抽取失败但其余已入库。

## 幂等性设计

| 机制 | 作用 |
|---|---|
| `UNIQUE (city_id, observed_at)` | 同一城市同一观测时刻只能有一行 |
| `ON CONFLICT ... DO UPDATE ... WHERE 业务列 IS DISTINCT FROM ...` | 数据无变化时不执行 UPDATE，`updated_at` 不被刷新 |
| 不执行任何 `DELETE` / `TRUNCATE` | 重复运行只会 upsert，不会清空重灌 |
| 维度表按 `city_key` upsert | 城市 ID 稳定，配置改名不会产生新维度行 |

`observed_at` 统一存 UTC（`TIMESTAMPTZ`），城市时区保留在 `cities.timezone`；按当地日期聚合时用
`(observed_at AT TIME ZONE c.timezone)::date`。

已实测（2026-10-08 ~ 2026-10-21，三城市 1008 行）：

```text
第一次：提交 1008 | 新增/更新 960 | 未变化 48
第二次：提交 1008 | 新增/更新 0   | 未变化 1008
重复键检查：0 行
业务列 checksum：b3d48bc52e4649e0e1a4eed9e165c9e0（两次一致）
max(updated_at)：两次一致
```

## 表结构

- `cities`：`city_id`、`city_key`（唯一）、`city_name`、`latitude`、`longitude`、`timezone`、`created_at`
- `weather_hourly`：`id`、`city_id`（外键）、`observed_at`（UTC）、`temperature_c`、`precipitation_mm`、`wind_speed_kmh`、`quality`、`issues(jsonb)`、`created_at`、`updated_at`

`quality` 四态：`valid` / `missing` / `suspect` / `invalid`。异常数据不删除，只打标签；
`invalid` 记录允许保留负值等物理越界值，便于排查。

## 已知限制

- 使用 forecast 接口：最多回看约 3 个月、预报 16 天；更早日期需改用 `archive-api.open-meteo.com`。
- 不做水位线增量同步，日期范围由 `--start/--end` 显式指定；不同区间是叠加而非替换。
- 时间无法解析的记录无法进入事实表，会写入 `data/processed/weather_rejected.json` 并计数。
