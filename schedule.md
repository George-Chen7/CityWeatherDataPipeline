好，这次我们不继续堆学习资料，直接进入实战。我给你一个接近真实 Data Engineer 工作、但难度适合你目前水平的任务。

我先不提供完整代码，你自己实现。遇到问题可以把代码、报错或 GitHub 仓库发给我，我会按验收标准帮你检查。

# 实战任务 01：城市天气数据 ETL 系统

[Skyline of Shenzhen city and Hong Kong city](https://images.openai.com/static-rsc-4/s94ti9uVKCD9uRQ4O9Mt6l4Ro_NMVY8-NN-sL41NJUmX2hot6axB_EtA2Jsn7oLK5uyN0jtBGtkE46dT4a7P-8tCOQiD0hIx8rJGpL8Zx7zFyJ1RHDSD_DQo-gl2aAXpCmcZiR79ifPad6zAHIbLnmroqj15Xpml34CQbX66nRMyjL4FGtdfQKzm1O-EVzH1?purpose=inline)

## City Weather Data Pipeline

初级 → 中级

预计用时：7–14 天

目标： 从公开 API 获取数据，经过 Python 清洗后存入 PostgreSQL，最后用 SQL 完成分析。

本项目只使用 Python、SQL 和 PostgreSQL。暂时不使用 Airflow、Spark 或 AWS，避免一次学太多东西。

## 一、业务需求

假设你是一名数据工程师，公司想分析深圳、香港和新加坡三个城市的天气情况。

你需要开发一套程序，自动完成：

Open-Meteo 公共 API

获取天气原始数据

Python

请求、清洗、校验、转换

PostgreSQL

存储结构化数据

SQL 分析

输出城市天气统计结果

数据源使用免费的 [Open-Meteo 历史天气 API](https://open-meteo.com/en/docs/historical-weather-api?utm_source=chatgpt.com)，不需要 API Key。

你需要获取的字段：

| 字段               | 含义                 |
| ---------------- | ------------------ |
| `time`           | 观测时间               |
| `temperature_2m` | 气温，摄氏度             |
| `precipitation`  | 降水量，毫米             |
| `wind_speed_10m` | 10 米高度风速，默认单位 km/h |

先获取三个城市连续 14 天的逐小时数据。日期、城市坐标和时区应该可以通过配置修改，而不是写死在处理逻辑里。

## 二、你的开发任务

### Task 1：Python 获取数据

要求：

- 使用 `requests` 请求 API。
- 支持三个城市的数据获取。
- 设置超时，并处理 HTTP 错误。
- 将原始响应保存到 `data/raw/`，方便排查问题。
- 使用 `logging` 记录请求成功、失败和获取的数据条数。

验收标准：程序能成功获取三个城市的数据；某一个城市请求失败时，日志能说明原因。

参考：[Requests Quickstart](https://requests.readthedocs.io/en/latest/user/quickstart/?utm_source=chatgpt.com)

### Task 2：数据清洗与验证

将 API 返回的数据整理为一行一条天气记录的结构。

必须处理：

- 缺失值与异常值。
- 时间格式统一。
- 重复记录。
- 数值字段的数据类型。
- 基本合理性检查，例如降水量不能为负数。

注意：不要直接把所有异常数据删除。 你需要区分缺失值、无效记录和合理的极端天气数据，并记录处理结果。

验收标准：清洗后的数据结构一致，可以明确统计输入记录数、有效记录数、异常记录数。

参考：[Pandas 入门教程](https://pandas.pydata.org/docs/getting_started/intro_tutorials/?utm_source=chatgpt.com)

### Task 3：设计 PostgreSQL 数据库

你至少需要设计两张表。

`cities`：城市维度表。

| 字段          | 含义   |
| ----------- | ---- |
| `city_id`   | 主键   |
| `city_name` | 城市名称 |
| `latitude`  | 纬度   |
| `longitude` | 经度   |
| `timezone`  | 城市时区 |

`weather_hourly`：逐小时天气事实表。

| 字段                 | 含义      |
| ------------------ | ------- |
| `city_id`          | 外键，关联城市 |
| `observed_at`      | 观测时间    |
| `temperature_c`    | 气温      |
| `precipitation_mm` | 降水量     |
| `wind_speed_kmh`   | 风速      |

关键要求：为 `(city_id, observed_at)` 设置唯一约束，避免同一城市同一时刻出现重复记录。

参考：[PostgreSQL 官方教程](https://www.postgresql.org/docs/current/tutorial.html?utm_source=chatgpt.com)

### Task 4：实现完整 ETL

把程序拆成独立模块：

- `extract.py`：获取数据。
- `transform.py`：清洗和验证。
- `load.py`：写入 PostgreSQL。
- `main.py`：串联整个流程。

数据库连接信息放在环境变量中，不要直接写进代码。

要求：

1. 首次运行可以建表并导入数据。
2. 再次运行不能产生重复记录。
3. 数据库连接失败时有清晰错误信息。
4. 能够通过参数指定城市和日期范围。

这里的重点是幂等性：同样的数据处理两次，数据库最终状态应该一致。

## 三、用 SQL 完成 5 道分析题

ETL 完成后，你还必须回答这些业务问题。

### 1

城市气温对比

统计每个城市的平均气温、最高气温和最低气温。

考察：GROUP BY、聚合函数

### 2

每日降水统计

计算每个城市每天的累计降水量，并找出降水最多的日期。

考察：日期处理、GROUP BY

### 3

最热时段排行

找出每个城市气温最高的 5 个观测时段。

考察：ORDER BY、LIMIT

### 4

城市排名

按照研究期间的平均气温给三个城市排名。

考察：CTE、RANK()

### 5

7 小时滚动平均

计算每个城市每个观测时刻往前 7 小时的平均气温。

考察：窗口函数、PARTITION BY

要求：每道题写成独立的 `.sql` 文件，并在 README 中说明查询结果代表什么。

## 四、建议的项目目录

最终项目至少整理成这样：

```
city-weather-pipeline/
├── src/
│   ├── extract.py
│   ├── transform.py
│   ├── load.py
│   └── main.py
├── sql/
│   ├── schema.sql
│   ├── city_summary.sql
│   ├── daily_precipitation.sql
│   ├── temperature_ranking.sql
│   └── rolling_average.sql
├── data/
│   └── raw/
├── tests/
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

这不是要求你一开始就创建全部文件。你可以先实现获取数据，再逐步补充其他模块。

## 五、最终验收清单

### 项目验收进度

### 0/9

能够从 API 获取三个城市的逐小时天气数据

能够处理缺失值、异常值和重复记录

PostgreSQL 表设计合理，包含主键、外键和唯一约束

首次运行可以成功写入数据库

重复运行不会产生重复天气记录

数据库连接失败等异常能被妥善处理

5 道 SQL 分析题全部得到合理结果

有至少 3 个自动化测试，覆盖清洗、异常数据或重复写入

README 包含环境配置、运行步骤、表结构和分析结果

## 六、现在先从第一步开始

今天只做 Task 1 的第一部分，不要一次写完整个项目：

1. 创建 GitHub 仓库 `city-weather-pipeline`。
2. 建立 Python 虚拟环境。
3. 安装 `requests`。
4. 访问 Open-Meteo API，成功获取一个城市、一天的逐小时天气数据。
5. 把原始 JSON 保存到本地文件。

完成后，再扩展到三个城市和 14 天的数据。

你现在的任务不是快速写完，而是独立实现并理解每个模块。 这套项目做完，你就能按照我们设定的第一个阶段验收标准，证明自己具备基础的 Python 数据处理、SQL 分析和 ETL 开发能力。