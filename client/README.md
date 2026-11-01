# EvalForge Client

独立安装的本地 Runner。当前实现 **WebSocket 传输层 + 双向 JSON-RPC 2.0 层 + SQLite 异步 ORM 与 Attempt 仓储**。
协议依据：[V1 草案](../docs/EvalForge_Runner_JSON-RPC_Protocol_v1.md)。
Python 3.11+，直接依赖 `websockets`、`jsonrpcserver`、`jsonrpcclient`、`aiosqlite`、`SQLAlchemy[asyncio]`，
使用本地 SQLite 文件；ORM 模型独立于平台 PostgreSQL 模型，不需要外部数据库服务。完整依赖版本记录在 `requirements.lock`。

## 安装与验证

在 `EvalForge/client` 目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -X utf8 -W error -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -X utf8 examples/transport_rpc_demo.py
```

示例启动临时的本地 WebSocket 服务，客户端调用服务端，服务端在处理过程中反向调用客户端，
最终打印相同的中文 JSON 数据；退出时关闭双方连接。它验证通信两层，不模拟 Job 执行。

`python -m evalforge_client` 已提供配置加载、启动恢复、连接和 hello/sync 链路。
会话 ready 后仍不执行评测：JobManager 和 Agent HTTP 调用尚未实现，派发返回 `executor_unavailable`，hello 的 capabilities 暂为空。

## 启动客户端

复制 `config.example.json` 为 `client-config.json`，修改云端地址、Runner ID 和本地目标；然后在 PowerShell 执行：

```powershell
$env:EVALFORGE_RUNNER_TOKEN = "你的 Runner 凭据"
.\.venv\Scripts\python.exe -m evalforge_client --config client-config.json --check-config
.\.venv\Scripts\python.exe -m evalforge_client --config client-config.json
```

`--check-config` 不打开数据库、不联网；`Ctrl+C` 退出并清理连接及数据库资源。
状态文件相对路径按**配置文件所在目录**解析。生产使用 WSS；本机测试可在配置中显式设置 `allow_insecure_localhost=true`，仅允许 loopback WS。
可设置 `rpc_timeout`（默认 30 秒）。进程独占锁暂记 TODO，同一状态库只能手动启动一个 Runner。
配置示例的云端地址为占位，尚未验证真实 Cloud 部署。

## 文件与职责

| 文件 | 状态及职责 |
| --- | --- |
| `__init__.py`、`__main__.py` | 包版本、最小命令行帮助 |
| `transport.py` | 已实现：连接、Bearer 握手认证头、文本消息、保活、关闭、重连退避 |
| `rpc.py` | 已实现：两个 JSON-RPC 库的双向连接适配、超时与断线清理 |
| `app.py`、`config.py` | 已实现：配置读取、进程恢复、组件生命周期、连接循环和退出 |
| `models.py` | 已实现：17 字段的 RunnerAttempt ORM、JSON 转换、状态和一致性约束 |
| `database.py`、`attempts.py` | 已实现：异步 Session、版本化建表、Attempt 仓储 |
| `session.py` | 已实现：hello/sync、响应校验、接单门禁和结果上传；通过 SessionJobs 接口衔接后续执行层 |
| `store.py` | 保留原生 SQL 接口：SQLite 文件连接、事务、关闭 |
| `job_manager.py`、`http_executor.py` | 占位：Job 执行、Agent HTTP 调用 |
| `config.example.json` | 可加载的配置示例；凭据从指定环境变量读取 |
| `tests/test_rpc.py`、`test_transport.py`、`test_store.py`、`test_database.py` | 通信、本地存储与 ORM 仓储测试 |
| `tests/test_session.py` | 会话、结果补交、同步期间完成和真实回环重连测试 |
| `tests/test_job_lifecycle.py`、`test_recovery.py` | 完整 Job 执行及恢复的后续测试位置，仅含占位说明 |

## 传输层

- `WebSocketTransport.open()`：一次连接的异步上下文，退出时自动关闭。
- `WebSocketTransport.connections()`：重连后的新连接流；瞬时故障带抖动指数退避，
  持续稳定连接后重置退避。认证拒绝、证书验证失败等永久错误向上抛出。
- 默认要求 WSS，使用库默认 TLS 证书验证；本地示例显式允许 loopback WS。
- Ping/Pong 默认各为 20 秒；握手超时 10 秒，关闭超时 5 秒，接收消息上限 1 MiB。
- 默认不读取系统代理设置。凭据通过 Authorization 请求头发送，不拼进 URL。
- `TransportClosed` 表示当前连接失效；重连不保留 RPC 等待，也不自动重发消息。

后续会话层可按下面方式接入：

```python
from contextlib import aclosing
from evalforge_client.rpc import JsonRpcPeer
from evalforge_client.transport import WebSocketTransport, TransportClosed

transport = WebSocketTransport(cloud_url, bearer_token=token)
async with aclosing(transport.connections()) as connections:
    async for connection in connections:
        try:
            async with JsonRpcPeer(connection) as peer:
                # 后续：注册业务方法，hello → sync，然后开放接单。
                await peer.wait_closed()
        except TransportClosed:
            continue
```

迭代器只管理物理连接；hello、sync 和结果上传由 `RunnerSession` 管理。
应用通过取消外层任务退出；`aclosing` 确保在 break/取消时关闭当前连接。

## JSON-RPC 层

标准协议处理交给两个库，项目维护连接上的异步调用管理：

| 组件 | 职责 |
| --- | --- |
| `jsonrpcclient.request / notification` | 构造出站请求与通知 |
| `jsonrpcclient.parse` | 将入站响应解析为 Ok / Error |
| `jsonrpcserver.async_dispatch_to_serializable` | 请求 schema 校验、参数绑定、方法查找、通知与错误响应 |
| `JsonRpcPeer` | 双向消息分流、待响应 Future、容量、超时与连接关闭 |

业务处理函数仍是普通 async 函数，直接返回 JSON 值或主动抛出 `RpcError`。
适配器保留原函数签名，转换成库要求的 `Success/Error`，并将非预期异常转换成不带详情的 Internal error。
各 Peer 使用自己的方法字典，不向库的全局注册表注册。

- 用 `async with JsonRpcPeer(connection)` 启动唯一接收循环。
- `register(name, async_handler)` 注册异步方法；`call()` 返回 result 或抛出 `RpcError`。
- `notify()` 不带 id；通知即使处理失败，也不回复消息。
- params 支持对象、数组或省略；按函数签名绑定，参数不匹配返回 `-32602`。
  `params: null` 则由库的请求 schema 判为 Invalid Request（`-32600`）。
- 支持单消息及批量消息；请求 ID 使用 UUID 字符串，和 Job ID 分开。
- 方法在后台异步执行，接收循环持续处理响应；处理函数可以反向发起调用。
- RPC 超时/调用方取消只停止本地等待，不代表远端操作停止。迟到和重复响应被忽略。
- 断线使未完成调用抛出 `TransportClosed`，取消当前连接的处理任务；新连接须创建新 Peer。
- 已知方法抛 `RpcError` 时返回该错误；其他异常或不可 JSON 序列化的输出统一返回 Internal error，
  不把异常详情或堆栈发给对端。
- 默认限制：128 个本地待响应请求、32 个入站处理任务、每批最多 32 条消息。
  同一批请求依次执行；超过处理容量返回 `-32000`，超过批大小返回 Invalid Request。

HTTP/Agent 失败和 `accepted=false` 是后续业务结果，应放在正常 result 中；
RPC 层不负责调度、业务幂等、执行重试或持久化。

当前发布版本 `jsonrpcclient 4.0.3` 的响应解析较宽松，因此保留版本、result/error 互斥及错误字段类型检查。
`jsonrpcserver 5.0.9` 默认可能返回异常文本，因此在方法适配边界先捕获非预期异常。
业务主动抛出的 `RpcError` 保留 code/message/data；合法的负数错误码也保留 data。

参考：[JSON-RPC 2.0 规范](https://www.jsonrpc.org/specification)、
[websockets asyncio Client 文档](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)。

## 本地存储基础层

`aiosqlite` 提供 SQLite 的异步连接和游标，项目用 `SQLiteDatabase` 统一资源和事务管理：

可先在 `client` 目录运行完整示例：

```powershell
.\.venv\Scripts\python.exe -X utf8 examples/sqlite_demo.py
.\.venv\Scripts\python.exe -X utf8 examples/sqlite_demo.py --value "更新后的内容"
```

示例在 `state/storage-demo.sqlite3` 创建独立的 `demo_values` 表，使用参数绑定写入，
提交并关闭后重新打开文件读取。重复运行会更新同一个键；可用 `--db 路径` 指定示例数据库。
`SQLiteDatabase` 本身不创建业务表，表结构和 SQL 由调用方提供。

```python
from evalforge_client.store import SQLiteDatabase

async with SQLiteDatabase("state/runner.sqlite3") as database:
    async with database.transaction() as connection:
        async with connection.execute("SELECT ? AS value", ("hello",)) as cursor:
            row = await cursor.fetchone()
            print(row["value"])
```

- 首次使用自动创建父目录和数据库文件；不会自动建 Job 或结果表。
- 一个对象持有一条连接，整个事务块通过 asyncio.Lock 串行执行；驱动的 SQL 队列不能代替事务锁。
- 显式 `BEGIN IMMEDIATE`，正常退出提交，异常或取消时尝试回滚，SQLite 错误向上抛出。
  取消发生在 commit 已被 SQLite 执行之后时，不能撤销已提交内容；调用方不能据此断言未写入。
- 默认锁等待 5 秒、启用外键和 FULL 同步；本阶段沿用 SQLite 默认回滚日志，不启用 WAL 或连接池。
- 只在事务块内使用连接，关闭游标，用 `?` 绑定参数；不要手动 commit/rollback/executescript。
- 事务中不等待网络调用，不把同一事务拆给其他任务；同任务嵌套事务会立即报错。
- 退出数据库上下文时等待当前事务结束并关闭连接；同任务在事务内关闭数据库会报错。
- 示例配置 `state_path` 改为 `state/runner.sqlite3`；相对路径按进程工作目录解析。
  原有 JSON 不会自动转换，数据库及日志文件已被 Git 忽略。

原生 `SQLiteDatabase` 保持不建业务表；Attempt 业务存储使用下面的 ORM 层，不要用两种封装并行管理同一数据库文件。
底层参考：[aiosqlite 文档](https://aiosqlite.omnilib.dev/en/stable/)。

## Attempt ORM 数据库层

表结构与恢复约定见 [表设计](../docs/client/表设计.md)。

```python
from evalforge_client.database import ClientDatabase
from evalforge_client.attempts import AttemptRepository

async with ClientDatabase("state/runner.sqlite3") as database:
    attempts = AttemptRepository(database)
    row = await attempts.accept(
        job_id="job-01", attempt_id="attempt-01", agent_target_id="order-agent",
        example_id="example-01", input_json={"message": "查询订单"}, timeout_seconds=60,
    )
    pending = await attempts.list_pending_results()
```

- 首次打开自动创建 `runner_attempts` 表及版本 1；重开保留记录，不自动运行状态恢复。不支持的版本或无版本的同名表需显式迁移，不能直接覆盖。
- `ClientDatabase.session()` 提供 SQLAlchemy `AsyncSession`，成功提交、异常回滚；每个块独立会话，事务串行，使用 `BEGIN IMMEDIATE`。不要嵌套会话、手动提交或在事务中等待网络。
- `RunnerAttempt.input_json/result_json` 在 Python 中为 JSON 值，在磁盘中为 TEXT；非字符串对象键和非有限数会被拒绝。直接修改已加载 JSON 时需整体重新赋值。
- `accept()` 幂等保存派发，相同 ID 参数不一致抛出 `AttemptConflict`；对象键顺序不影响比较。返回值是已提交、脱离会话的 ORM 对象，修改它不会自动写回。
- `claim()` 在 `accepted` 且无取消意图时条件更新为 `running`；只有返回 True 才取得执行权，之后才能调用 Agent。不同 Attempt 之间的单任务容量由后续 JobManager 管理。
- `request_cancel()` 仅保存意图；`finish()` 原子保存终态与待确认结果，最终结果不可被仓储重写；`acknowledge()` 仅在收到云端确认后调用。
- `recover_interrupted()` 显式将遗留 `running` 转为 `unknown`；app 只在启动时调用一次，不能用于普通重连。进程锁暂记 TODO，当前依赖单进程使用约定；HTTP 执行尚未接入。
- `list_unresolved()` 和 `list_pending_results()` 为后续同步提供数据，不自动重传或重执行。

## 会话与同步规则

完整链路与代码阅读顺序见 [会话层核心链路](../docs/client/会话层核心链路.md)。

- 每次连接创建新 Peer 和 RunnerSession，注册业务入口后依次 hello、读取原子数据库快照、sync。hello 响应必须含非空 session_id 和 status=ready；此时还不开放接单。
- `sync_snapshot()` 在同一个事务内读取 active_jobs 和 pending_results，网络调用不占数据库事务。pending_results 发送保存的完整最终业务结果，不含 JSON-RPC id。
- 云端须对快照中的每个 Attempt 返回一个指令，不得遗漏、重复或引用未知 ID。支持 continue、cancel、retry_upload；discard 暂不支持。unknown 不接受 continue，不会自动重执行。
- 快照中的历史待确认结果仅在收到 retry_upload 后提交，云端返回严格的 acknowledged=true 后才落盘确认。失败、超时或断线保留结果并关闭当前会话，再重连同步。
- 快照生成后新增的结果在 ready 后按正常流程上报。上传器 ready 后立即检查，随后每 0.5 秒检查 SQLite，避免只依赖内存通知而遗漏；无需再次 sync。
- 应用同步指令前重新读本地状态；已完成任务不会被旧的 continue/cancel 指令改回执行中。
- SessionJobs 的 resume/cancel 必须快速交给独立 JobManager，不能把 Agent 执行挂在连接处理协程中。数据库和 jobs 对象跨连接保留。
- 没有接入执行层时仍可建立 ready 会话并同步存量记录，但拒绝派发；接入后仍检查未解决 Attempt，容量竞争最终由 JobManager 管理。

后续事项见 [客户端 TODO](../docs/client/TODO.md)。

本地 ORM 示例（不联网，不模拟云端确认；重复运行保留同一个 Attempt）：

```powershell
.\.venv\Scripts\python.exe -X utf8 examples/attempt_database_demo.py
```

## 阅读顺序与本次验证

先运行 `examples/transport_rpc_demo.py`，再阅读 `transport.py` 的 `open/connections`，
随后看 `rpc.py` 的 `call → _receive → _answer` 与 `register` 中的库适配；
最后对照测试中的断线、通知和反向调用场景。

2026-10-30 初次实现验证记录（RPC 已在下述重写中替换）：

- CPython 3.12.14，独立环境仅安装客户端包与 `websockets==17.3`；依赖检查通过，
  editable 安装的包构建成功。`requirements.lock` 从已安装依赖自动生成。
- `-W error` 下 17 项测试全部通过，本地双向调用示例输出一致的中文 JSON 并正常退出。
- 临时反转“通知不回包”的判断后，通知测试按预期失败；随后恢复源码并通过完整测试。
- 独立复查发现处理函数主动关闭 Peer 时的循环等待，已修复并加入回归测试。
- Windows 沙箱中的回环通信测试未完成；获准在沙箱外运行后通过。
- 网络集成验证使用 loopback WS；尚未连接真实 Cloud 或部署 WSS 测试端点。
- 未修改数据库、现有评测 Runner 或协议草案；未实现会话和 Job 的业务可靠性机制。

2026-10-30 指定库重写验证：

- 安装 `jsonrpcserver==5.0.9`、`jsonrpcclient==4.0.3`，更新独立包依赖和完整锁文件；
  可编辑包构建成功，`pip check` 通过。
- 20 项测试在 `-W error` 下全部通过，双向通信示例正常运行。
- 新增负数 RPC 错误数据保留、嵌套批请求拒绝、方法注册隔离、null 结果与无效响应验证。
- 临时反转响应互斥字段检查后，相关测试按预期失败；恢复源码后通过全部测试。
- 独立复查指出错误数据过度清理问题，修正后保留用户主动提供的 RpcError.data。
- `transport.py` 和业务占位层未改动；尚未连接真实 Cloud。

2026-10-30 存储基础层验证：

- 新增 `aiosqlite==0.22.1`，更新独立包依赖和锁文件。
- 新增 8 项真实 SQLite 文件测试：关闭重开、参数化写入、异常/取消回滚、提交点取消、并发隔离、
  连接设置、非法生命周期操作和已有损坏文件保护；全部 28 项客户端测试在 `-W error` 下通过。
- 临时把异常回滚改为提交后，回滚测试按预期失败；恢复源码后通过完整测试。
- 不修改平台 PostgreSQL 基础设施，不创建客户端业务表。
