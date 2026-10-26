# 阶段二：容器网络与配置（任务 01–02）

阶段二在 `deploy/` 下维护一套**独立**环境，不与阶段一的 `weather-pg` 混用：

- 阶段一：宿主机 `5432`，容器 `weather-pg`，数据卷 `weather_pgdata`。
- 阶段二：宿主机 `5433`，容器 `weather-pg-stage2`，数据卷 `city-weather-stage2-pgdata`。

两套容器、两个卷互不影响，所以可以在同一台机器上并存。

## 目录

| 文件 | 用途 |
|---|---|
| [`docker-compose.yaml`](docker-compose.yaml) | PostgreSQL + `etl` 应用容器 |
| [`Dockerfile`](Dockerfile) | `etl` 镜像：Python 3.12 + 项目依赖 |
| [`.env.example`](.env.example) | Compose / 数据库初始化变量模板 |
| `../.dockerignore` | 构建上下文排除 `.env`、`.venv`、`data/` 等 |
| `../.gitignore` | 忽略任意层级的 `.env`，保留 `.env.example` |

## 快速开始

```powershell
cd deploy
Copy-Item .env.example .env
# 编辑 .env，至少把 POSTGRES_PASSWORD 改成长随机串
docker compose build etl      # 首次构建应用镜像；只改 Python 代码不需要重建
docker compose up -d
docker compose ps
```

预期 `docker compose ps` 的 `PORTS` 一列是：

```text
0.0.0.0:5433->5432/tcp, [::]:5433->5432/tcp
```

`STATUS` 应为 `Up ... (healthy)`。健康检查通过才说明数据库真的可以接受连接，
而不是“容器进程起来了”。

## 1. 容器网络：服务名是怎么解析的

Compose 会创建一个以项目名命名的 bridge 网络：

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
     └─ etl 容器     （PGHOST=postgres，PGPORT=5432）
```

三个容易混淆的点：

1. **`localhost` / `127.0.0.1` 在容器里指的是容器自己**，不是宿主机，也不是
   `postgres` 容器。在 `etl` 容器里连 `127.0.0.1:5432` 会得到 connection refused。
2. **服务名只在 Docker 网络内有效**。在 Windows 宿主机上 `ping postgres` 不会成功，
   宿主机必须走 `127.0.0.1:5433`。
3. **`depends_on` 不等于数据库已就绪**。所以 `etl` 用的是
   `condition: service_healthy`，等 `pg_isready` 健康检查通过后再启动。

## 2. 宿主机端口 vs 容器端口

Compose 的端口写法是 **`宿主机端口:容器端口`**：

```yaml
ports:
  - "${POSTGRES_HOST_PORT:-5433}:5432"
```

- 右边 `5432`：PostgreSQL 在容器里固定的监听端口。
- 左边 `5433`：Docker 在 Windows 上发布的端口，可以改，用来避开阶段一的 `5432`。
- 这个映射是 NAT，数据库自己只知道容器内的 `5432`，不知道宿主机用了 `5433`。

| 从哪里连 | `PGHOST` | `PGPORT` | 走的路径 |
|---|---|---|---|
| Windows 宿主机 | `127.0.0.1` | `5433` | `localhost:5433` → Docker → 容器 `5432` |
| `etl` 容器 | `postgres` | `5432` | Docker 网络内的服务名 → 容器 `5432` |
| 另一个同网络容器 | `postgres` | `5432` | 同上 |

查看实际映射：

```powershell
docker port weather-pg-stage2
# 5432/tcp -> 0.0.0.0:5433
# 5432/tcp -> [::]:5433
```

## 3. `.env` 分层与密钥

项目里有**两个** `.env`，职责不同：

| 文件 | 谁读取 | 内容 |
|---|---|---|
| `deploy/.env` | Docker Compose | `POSTGRES_*`、`POSTGRES_HOST_PORT`、`TZ` |
| 根目录 `.env` | 宿主机上的 Python（`src/config.py`） | `PGHOST`、`PGPORT`、`PGDATABASE`、`PGUSER`、`PGPASSWORD` |

`etl` 容器**不挂载** `.env`。Compose 只把需要的变量显式注入容器：

```yaml
environment:
  PGHOST: postgres
  PGPORT: "5432"
  PGDATABASE: ${POSTGRES_DB:?POSTGRES_DB is required}
  PGUSER: ${POSTGRES_USER:?POSTGRES_USER is required}
  PGPASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}
```

这样同一份 `src/` 代码只认 `PG*`，运行位置变了只改环境变量：

- 容器里：`PGHOST=postgres`、`PGPORT=5432`
- 宿主机：`PGHOST=127.0.0.1`、`PGPORT=5433`

`src/config.py` 用 `load_dotenv(BASE_DIR / ".env", override=False)` 读取根 `.env`，
所以**进程环境变量优先于 `.env`**。临时验证时可以直接 `$env:PGPORT='5433'`，
不必修改文件；长期配置则写进 `.env`。

### 密钥为什么不会进 Git / 镜像

- `.gitignore` 的 `.env` 规则对任意层级生效，`deploy/.env` 也被忽略；
  `.env.example` 通过 `!.env.example` 保留。
- `.dockerignore` 排除了 `.env`、`**/.env`、`.venv`、`data/` 等，构建上下文只有
  几十 KB，镜像里没有 `.env`。
- `DatabaseConfig.endpoint` 只拼接 `user@host:port/dbname`，日志不打印密码。
- **注意**：`docker compose config` 会把 `${...}` 展开成真实值，输出里有密码，
  不要把它贴到聊天、Issue 或日志里。

## 4. 验收命令（任务 02）

以下命令默认在 `deploy/` 目录下执行；标注“项目根目录”的命令除外。

### 4.1 容器通过服务名访问 PostgreSQL

```powershell
docker compose run --rm etl python -c 'from src.load import connect; c=connect(); cur=c.cursor(); cur.execute("SELECT current_database(), current_user, inet_server_addr()"); print(cur.fetchone()); c.close()'
```

预期类似：

```text
('weather', 'weather', IPv4Address('172.18.0.2'))
```

`172.18.0.2` 是 `postgres` 容器在当前网络里的 IP；换成 `127.0.0.1` 就会失败。

### 4.2 反证：容器内不能用 127.0.0.1

```powershell
docker compose run --rm -e PGHOST=127.0.0.1 etl python -c 'from src.load import connect; connect()'
```

预期报错（第一行）：

```text
无法连接数据库 postgresql://weather@127.0.0.1:5432/weather
```

### 4.3 宿主机通过映射端口访问

先把根目录 `.env` 的 `PGPORT` 改成 `5433`、`PGPASSWORD` 改成 `deploy/.env` 里的值，
然后在**项目根目录**执行（验证完记得改回阶段一的值，否则阶段一脚本会连到阶段二）：

```powershell
.\.venv\Scripts\python.exe -c 'from src.load import connect; c=connect(); cur=c.cursor(); cur.execute("SELECT current_database(), to_regclass(''public._persistence_check'')"); print(cur.fetchone()); c.close()'
```

预期：

```text
('weather', '_persistence_check')
```

如果不想改根 `.env`，可以用进程环境变量覆盖（效果等价，且演示了优先级）。
下面的脚本同样在**项目根目录**执行：

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

.\.venv\Scripts\python.exe -c 'from src.load import connect; c=connect(); cur=c.cursor(); cur.execute("SELECT current_database(), to_regclass(''public._persistence_check'')"); print(cur.fetchone()); c.close()'
```

### 4.4 只改配置、不改代码

同一份代码，只通过环境变量切换目标：

```powershell
# 换数据库：连到 postgres 维护库
docker compose run --rm -e PGDATABASE=postgres etl python -c 'from src.load import connect; c=connect(); cur=c.cursor(); cur.execute("SELECT current_database()"); print(cur.fetchone()); c.close()'

# 换路径：走宿主机映射端口 host.docker.internal:5433（Windows/macOS Docker Desktop）
docker compose run --rm -e PGHOST=host.docker.internal -e PGPORT=5433 etl python -c 'from src.load import connect; c=connect(); cur=c.cursor(); cur.execute("SELECT current_database(), inet_server_addr()"); print(cur.fetchone()); c.close()'
```

两次都不需要改 `src/`。第一条输出 `postgres`，第二条仍然连到
`172.18.0.2` 上的 `weather` 库。

### 4.5 安全检查

在**项目根目录**执行：

```powershell
# 应输出 .gitignore 的匹配规则
git check-ignore -v deploy/.env .env

# 应无输出：镜像里没有 .env
docker run --rm --entrypoint sh city-weather-etl:stage2 -c "find /app -name '.env*' -print"
```

## 5. 实测记录（2026-10-23）

| 检查 | 实际结果 |
|---|---|
| `docker port` | `5432/tcp -> 0.0.0.0:5433`、`[::]:5433` |
| 容器内 DNS `postgres` | `172.18.0.2` |
| 容器内服务名查询 | `('weather', 'weather', IPv4Address('172.18.0.2'), '_persistence_check')` |
| 阶段二标记表 | `_persistence_check` 有 2 行（任务 01 重启前写入） |
| 容器内 `127.0.0.1` | `DatabaseConnectionError`（预期失败） |
| 宿主机 `127.0.0.1:5433` | `db=weather marker_table=_persistence_check`、`marker_rows=2` |
| 宿主机 `127.0.0.1:5432` | 密码认证失败（连到阶段一实例，证明端口隔离） |
| 只改 `PGDATABASE` | `current_database = postgres`，代码未改 |
| 只改 `PGHOST/PGPORT` | `('weather', IPv4Address('172.18.0.2'))`，代码未改 |
| 镜像内容 | `/app` 只有 `requirements.txt`、`sql/`、`src/`；无 `.env` |
| 时区 | 容器内 `ZoneInfo("Asia/Shanghai")` 正常 |

## 6. 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| 宿主机 `could not translate host name "postgres"` | 服务名只在 Docker 网络内有效 | 宿主机用 `127.0.0.1` + `POSTGRES_HOST_PORT` |
| 容器内 `connection refused` 到 `127.0.0.1` | 容器 loopback 不是宿主机 | 用 `PGHOST=postgres` |
| 宿主机 `5432` 密码认证失败 | 连到阶段一容器 | 阶段二用 `5433` |
| `port is already allocated` | 宿主机 `5433` 被占用 | 改 `POSTGRES_HOST_PORT`，同步改根 `.env` 的 `PGPORT` |
| 改了 `POSTGRES_PASSWORD` 仍认证失败 | `POSTGRES_*` 只在数据卷为空时初始化 | 用 `ALTER USER` 改密码，或删卷重建（会丢数据） |
| `docker compose config` 输出密码 | 它是“解析后的最终配置” | 不要分享该输出 |

## 7. 已知限制

- `etl` 容器目前只用于网络/配置验证，还没有接入 Airflow（任务 03–04）。
- 基础镜像用 `python:3.12-slim`，补丁版本会随上游漂移；任务 06 再统一固定。
- 密钥通过环境变量注入，拥有 Docker 权限的人可以用 `docker inspect` 看到；
  生产环境应使用 Docker secrets / 外部密钥管理，本项目暂不引入。
- `etl` 用 bind mount 挂载 `src/`、`sql/`、`data/`，修改代码无需重建镜像；
  但改动 `requirements.txt` 后需要 `docker compose build etl`。
