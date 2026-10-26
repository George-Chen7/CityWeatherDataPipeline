# City Weather Data Pipeline

从 Open-Meteo 拉取深圳 / 香港 / 新加坡的逐小时天气，经 Python 清洗与质量打标后，**幂等**写入 PostgreSQL，为后续 SQL 分析提供数据底座。

> **阶段二（容器化）**：基础设施在 [`deploy/`](deploy/) 下独立编排，默认使用宿主机端口 `5433`，不会覆盖阶段一的 `weather-pg`（`5432`）。启动、容器网络、端口映射与 `.env` 配置见 [`deploy/README.md`](deploy/README.md)。

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
│   ├── schema.sql               # 幂等 DDL，与数据库现有结构一致
│   ├── city_summary.sql         # 题 1：城市气温对比
│   ├── daily_precipitation.sql  # 题 2：每日降水 + 最湿日期
│   ├── hottest_periods.sql      # 题 3：最热时段 Top 5
│   ├── temperature_ranking.sql  # 题 4：城市平均气温排名
│   └── rolling_average.sql      # 题 5：7 小时滚动平均
├── data/
│   ├── raw/            # 原始 API 响应，按 城市_开始_结束.json 命名
│   └── processed/      # weather_clean.json / weather_rejected.json / cleaning_report.json
├── legacy/             # Task 1/2 的原始脚本，仅作对照，不参与运行
├── deploy/             # 阶段二：Compose、Dockerfile、环境变量模板
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

## SQL 分析

5 道题各自一个 `.sql` 文件，都能单独执行；文件里的注释写明了为什么这么写（时区转换、LATERAL、`ROWS` 与 `RANGE` 的区别等）。下面的结果是这 5 个文件真实执行后得到的。

**统一口径**

- 只统计 `quality IN ('valid', 'suspect')` 且相关数值字段非空的记录：`missing` 的字段本来就没值，`invalid` 不参与业务统计。
- `observed_at` 存 UTC，按“当地日历”聚合时统一写成 `(observed_at AT TIME ZONE c.timezone)::date`；直接 `::date` 会按 UTC 切天，把当地的 00:00–08:00 算到前一天。
- 本次数据库范围：当地日期 **2026-10-08 ~ 2026-10-21**，三城各 336 条逐小时记录，全部为 `valid`。

执行方式（项目根目录，PowerShell）：

```powershell
Get-Content .\sql\daily_precipitation.sql -Raw |
    docker exec -i weather-pg psql -U weather -d weather -v ON_ERROR_STOP=1
```

### 1. 城市气温对比 — [`sql/city_summary.sql`](sql/city_summary.sql)

每个城市的平均、最高、最低气温（`GROUP BY` + 聚合函数）。

| 城市 | 平均气温(℃) | 最高气温(℃) | 最低气温(℃) |
|---|---|---|---|
| 新加坡 | 27.93 | 34.30 | 23.70 |
| 香港 | 26.81 | 33.20 | 22.90 |
| 深圳 | 26.61 | 34.00 | 21.80 |

**这组数字说明什么**：新加坡是典型的赤道气候——均值最高，但日较差最小（34.30 − 23.70 = 10.6℃），全年都热且波动小。深圳和香港纬度接近，均值低 1℃ 左右，但日较差更大（深圳 12.2℃），说明这 14 天里既有 34℃ 的高温，也有 21.8℃ 的凉夜。最高气温三城都在 33–34℃，差异主要体现在“最低能到多低”，而不是“白天能有多热”。

### 2. 每日降水统计 — [`sql/daily_precipitation.sql`](sql/daily_precipitation.sql)

这个文件有两条查询：

- **2a** 每个城市每天的累计降水量，共 3 城 × 14 天 = 42 行。`有降水的小时数` 用来判断这点雨是“下一小时就停”还是“断断续续下一整天”，单看累计量会误判强度。

  | 城市 | 日期 | 有效观测数 | 有降水的小时数 | 累计降水量(mm) |
  |---|---|---|---|---|
  | 深圳 | 2026-10-12 | 24 | 4 | 0.7 |
  | 深圳 | 2026-10-14 | 24 | 5 | 0.7 |
  | 深圳 | 2026-10-15 | 24 | 6 | 1.4 |

- **2b** 每个城市降水最多的日期（并列全保留）。

  | 城市 | 降水最多的日期 | 累计降水量(mm) |
  |---|---|---|
  | 新加坡 | 2026-10-16 | 11.9 |
  | 香港 | 2026-10-17 | 1.7 |
  | 深圳 | 2026-10-15 | 1.4 |

**这组数字说明什么**：新加坡 14 天里有 13 天出现降水，期间累计 77.0 mm，单日最多 11.9 mm（10-16），是典型的对流性降水——几乎天天有雨、多为午后阵雨。香港和深圳同期几乎没下雨（总计 6.7 mm / 4.6 mm，单日峰值不到 2 mm），属于 10 月偏干的天气。也正因为量级差了一个数量级，“最湿日”在三城之间并没有可比性。

### 3. 最热时段排行 — [`sql/hottest_periods.sql`](sql/hottest_periods.sql)

每个城市气温最高的 5 个观测时刻，共 15 行。

| 城市 | 排名 | 观测时刻(当地) | 气温(℃) |
|---|---|---|---|
| 新加坡 | 1 | 2026-10-16 12:00 | 34.30 |
| 新加坡 | 2 | 2026-10-09 14:00 | 33.70 |
| 新加坡 | 3 | 2026-10-09 15:00 | 33.60 |
| 新加坡 | 4 | 2026-10-19 15:00 | 33.40 |
| 新加坡 | 5 | 2026-10-09 13:00 | 33.30 |
| 深圳 | 1 | 2026-10-21 14:00 | 34.00 |
| 深圳 | 2 | 2026-10-21 13:00 | 33.90 |
| 深圳 | 3 | 2026-10-21 12:00 | 33.70 |
| 深圳 | 4 | 2026-10-21 15:00 | 33.70 |
| 深圳 | 5 | 2026-10-13 14:00 | 33.30 |
| 香港 | 1 | 2026-10-21 14:00 | 33.20 |
| 香港 | 2 | 2026-10-21 13:00 | 33.10 |
| 香港 | 3 | 2026-10-21 12:00 | 33.00 |
| 香港 | 4 | 2026-10-21 15:00 | 33.00 |
| 香港 | 5 | 2026-10-21 11:00 | 32.40 |

**这组数字说明什么**：最高的时段几乎全部落在当地 12:00–15:00，符合太阳辐射的日变化。深圳、香港的高温集中在 10-21 这一天（深圳当天 12:00–15:00 全部上榜，香港还多一个 11:00），说明是一次区域性升温过程；新加坡则分散在 10-09、10-16、10-19 三天，没有明显的“同一天爆发”。

**实现细节**：`LIMIT 5` 必须对每个城市分别生效，所以用 `CROSS JOIN LATERAL` 让子查询按城市各跑一次；直接在外层 `ORDER BY ... LIMIT 5` 只会得到三城混在一起的 5 行。文件里也给了只用窗口函数的等价写法。深圳第 3、4 名都是 33.70℃，`ROW_NUMBER` 按观测时刻先后切分，所以两行都被保留。

### 4. 城市排名 — [`sql/temperature_ranking.sql`](sql/temperature_ranking.sql)

按研究期间平均气温排名（CTE + `RANK()`）。

| 排名 | 城市 | 平均气温(℃) | 参与统计的观测数 |
|---|---|---|---|
| 1 | 新加坡 | 27.93 | 336 |
| 2 | 香港 | 26.81 | 336 |
| 3 | 深圳 | 26.61 | 336 |

**这组数字说明什么**：就是第 1 题平均气温的一张排行榜。三个城市差距不大（第 1 名只比第 3 名高 1.32℃），但新加坡作为唯一的赤道城市稳定居首。`参与统计的观测数` 都是 336，说明三城样本量一致，排名没有被缺测拖偏。

**实现细节**：先用 CTE 聚合出平均气温，再在聚合结果上开窗排名。`RANK()` 遇到相同值会并列并跳号（1,1,3）；如果业务上要“并列第 2 之后直接第 3”，换成 `DENSE_RANK()`。文件注释里也说明了这个查询可以压成一层，拆 CTE 是为了可读和逐段验证。

### 5. 7 小时滚动平均 — [`sql/rolling_average.sql`](sql/rolling_average.sql)

每个城市每个观测时刻往前 7 小时的平均气温，共 1008 行（3 × 336）。选深圳 2026-10-15 的一天看窗口怎么滚动：

| 城市 | 观测时刻(当地) | 当前气温(℃) | 7小时滚动平均气温(℃) | 窗口内观测数 |
|---|---|---|---|---|
| 深圳 | 2026-10-15 00:00 | 25.00 | 25.00 | 1 |
| 深圳 | 2026-10-15 01:00 | 24.70 | 24.85 | 2 |
| 深圳 | 2026-10-15 02:00 | 24.70 | 24.80 | 3 |
| 深圳 | 2026-10-15 03:00 | 24.30 | 24.68 | 4 |
| 深圳 | 2026-10-15 04:00 | 24.10 | 24.56 | 5 |
| 深圳 | 2026-10-15 05:00 | 24.00 | 24.47 | 6 |
| 深圳 | 2026-10-15 06:00 | 24.00 | 24.40 | 7 |
| 深圳 | 2026-10-15 07:00 | 24.30 | 24.30 | 7 |
| ⋯ | ⋯ | ⋯ | ⋯ | ⋯ |
| 深圳 | 2026-10-15 16:00 | 28.60 | 28.91 | 7 |
| 深圳 | 2026-10-15 17:00 | 27.40 | 28.90 | 7 |
| 深圳 | 2026-10-15 18:00 | 25.70 | 28.51 | 7 |

**这组数字说明什么**：滚动平均把逐小时的抖动抹平，只留下趋势。10-15 当天深圳 00:00 的均值是 25.00℃（窗口里只有它自己），到 06:00 才第一次凑满 7 行；白天升温时均值滞后于瞬时气温——16:00 瞬时 28.60℃、均值 28.91℃ 还在爬，17:00 气温已经开始回落（27.40℃），均值却几乎没动（28.90℃），这就是低通滤波的滞后效应。判断“今天热不热”看均值比看单点更稳。

**实现细节**：`PARTITION BY city_id` 保证窗口不跨城市，`ORDER BY observed_at` 决定“前 6 行”是哪 6 行。每个城市最早 6 个时刻窗口填不满（`窗口内观测数` 为 1~6），是正常边界而非丢数据。若数据存在缺测，按行数开窗会把更早的时刻拉进来，这时应改用 `RANGE BETWEEN INTERVAL '6 hours' PRECEDING AND CURRENT ROW` 按真实时间跨度开窗。

**结论的适用范围**：以上结果只覆盖 14 天、单一季节，极端值（如 34.3℃、11.9 mm）不代表全年气候，比较三城时应理解为“这 14 天的天气差异”，而不是长期气候排名。

## 已知限制

- 使用 forecast 接口：最多回看约 3 个月、预报 16 天；更早日期需改用 `archive-api.open-meteo.com`。
- 不做水位线增量同步，日期范围由 `--start/--end` 显式指定；不同区间是叠加而非替换。
- 时间无法解析的记录无法进入事实表，会写入 `data/processed/weather_rejected.json` 并计数。
