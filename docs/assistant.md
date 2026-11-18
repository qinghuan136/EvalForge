# EvalForge 评测助手

在登录后的工作台顶部打开「评测助手」，可以在同一会话中跨页面追问。实验详情默认提供当前实验；Job、数据集和 Runner 明细内的「询问评测助手」提供该对象作为上下文。助手使用 PydanticAI，已注册 [17 个基础工具](assistant-tools.md)。查询工具直接读取数据，五个写工具通过原生 Deferred Tools 等待用户批准后，由后端调用业务 Service，真实结果交给模型继续回答。

## Conversation 与审批

已实现 [Deferred Tools 审批与持久化方案](AgentDeferredTools_审批与持久化方案.md)：前端收齐全部批准/拒绝决定后一次提交，待审批期间不能发送新普通消息。同批写工具逐个执行；拒绝的工具不执行。卡片参数由服务端保存的 Conversation 提供，前端不提交执行参数。

Conversation 先通过异步 fake 函数存入进程内字典，支持同一进程中跨 HTTP 请求恢复；仅支持单个后端进程，重启丢失现场。暂不修改助手数据库表，也不新增 Action 表。获批工具仍操作真实业务数据库，已有消息记录不等于可恢复的 Conversation。

网页使用统一 `POST /api/v1/assistant/stream`，通过 `type=message/decision` 分流。数据库继续保存可展示消息与原有执行租约，fake 记录完整执行现场并以会话级锁协调同一进程内的运行。重启后已有历史可看，但现场缺失的旧会话需新建后再聊天；不会根据旧历史重放审批。

后续 [ConversationStore 数据库存储](ConversationStore方案.md) 的两张表及 ORM、增量迁移已定义；尚未接入 Backend，旧助手表仍保留。执行迁移不代表审批现场已经改为数据库保存。

## 启用

1. 已有核心表（含 `users` 和 `runner_attempts`）且由仓库 Alembic 管理的数据库：执行 `python -m alembic upgrade head`，应用尚未执行的增量迁移。Compose 环境可用 `docker compose --env-file .env.compose exec cloud-a python -m alembic upgrade head`。若实例已有自行管理的迁移历史，把新增迁移接到现有链上，避免覆盖版本记录。
2. 在 `.env`（Compose 为 `.env.compose`）设置 `EVALFORGE_ASSISTANT_MODELS` 与 `EVALFORGE_ASSISTANT_API_KEY`。模型名以英文逗号分隔，第一项是默认模型。同一会话可从清单里逐轮选模型。可选的 `EVALFORGE_ASSISTANT_BASE_URL` 指向支持 Chat Completions 和工具调用的 OpenAI 兼容入口。省略时使用默认 OpenAI 入口。
3. 启动现有后端和前端。默认一轮最多等待 90 秒，租约 45 秒、每 10 秒续租；环境变量见 `.env.example`。Compose 网关等待聊天响应 120 秒；若调高模型等待时间，需同步检查网关超时。

全新数据库需先创建 EvalForge 核心表。开发环境可从项目根目录运行以下命令创建**完整当前结构**，再将迁移版本标记到当前 `head`：

```powershell
@'
import asyncio
from evalforge.db import Base, Database, DatabaseSettings

async def setup():
    async with Database(DatabaseSettings.from_env()) as db:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

asyncio.run(setup())
'@ | python -
python -m alembic stamp head
```

## HTTP 接口

以下路径均以 `/api/v1/assistant` 开头，使用现有登录 Cookie；POST 需要 CSRF。

| 请求 | 响应 |
| --- | --- |
| `GET /sessions` | 最近 50 个会话和允许选择的模型名称 |
| `POST /sessions`，`{"title":"实验对话"}` | 新会话的 ID、标题与初始版本 |
| `GET /sessions/{id}` | 会话版本、最近 100 条可见消息、`status`、待审批 `actions`、`credential_call_ids` 和 `error` |
| `POST /stream` | 统一 SSE 入口，接收普通消息或整批审批决定，结构见[方案](AgentDeferredTools_审批与持久化方案.md#5-请求与响应) |
| `POST /sessions/{id}/chat` | 兼容旧普通消息请求；默认 JSON，Accept 为 SSE 时返回事件流；同样使用原生审批 |
| `POST /sessions/{id}/runner-credentials/{tool_call_id}` | 当前用户一次性领取 Agent 创建的 Runner 的完整创建响应；已领取或不存在返回 `404` |

统一入口请求示例：`{"type":"message","session_id":42,"page_context":{"type":"experiment","id":7},"expected_version":0,"message":"跑到哪里了？","model_name":"配置中的模型名"}`。省略模型时使用默认值；审批恢复沿用暂停时的模型。历史通过完整 Conversation 继续，不再从 SQL 截取最近 12 轮重建。会话忙、版本过期、现场丢失或失去执行权返回 `409`。

凭证只存进程内独立容器，领取需登录 Cookie 和 CSRF，不进入模型、Conversation、普通 SSE 或查询缓存。网络失败后可能无法再次领取；进程重启也会丢失未领取凭证，此时可删除并重建 Runner。

## 页面上下文

`page_context` 可以省略，每轮最多提供一个对象。四种类型使用同一聊天接口（JSON 与 SSE 都适用）：

| `type` | 字段 | 校验 |
| --- | --- | --- |
| `experiment` | `id`（整数） | 实验属于当前用户 |
| `job` | `experiment_id`（整数）、`job_id`（字符串） | Job 属于指定实验，且实验属于当前用户 |
| `dataset` | `id`（整数）、可选 `version_id`（整数） | 数据集属于当前用户；版本如果提供，必须在该数据集下 |
| `runner` | `id`（字符串） | Runner 属于当前用户且未删除 |

字段或类型不合法返回 `422`；资源不存在、越权或父子关系不匹配返回 `404`。兼容旧客户端的顶层 `experiment_id`，在后端将它转换为实验上下文。和 `page_context` 同时提供时，两者必须指向同一个实验。页面上下文用于理解当前指代，工具在有无页面上下文时都按当前登录身份校验资源归属。

成功后，用户消息保存自身的 `page_context`，会话详情和聊天结果也会返回它。模型看到的是当时的上下文引用与用户原文，历史上下文不会因为切页而被替换。新类型需加入 `PageContext` 联合定义与对应的访问校验；`PageContextCatalog.list_types()` 从同一份类型定义生成清单。

## 聊天 SSE

用 `fetch` 发送同一个 POST 请求，带上 `Accept: text/event-stream`、登录 Cookie 和 CSRF 请求头，从响应体逐段读取。流开始前的鉴权、参数与会话冲突仍使用 HTTP 状态码和 JSON 错误；流开始后通过事件通知。

| 事件 | `data` | 客户端处理 |
| --- | --- | --- |
| `text_delta` | `{"text":"实验正在"}` | 追加到临时预览，JSON 内的换行保留 |
| `action_required` | `{"version":1,"actions":[{"tool_call_id":"call_1","tool":"RUNNER_create_runner","arguments":{"name":"本地开发"}}]}` | Conversation 已保存，按工具类型展示卡片 |
| `done` | `{"version":1,"status":"awaiting_approval","messages":[...],"actions":[...]}` | 本次 Run 已保存；`completed` 表示本次完成，`awaiting_approval` 表示需审批后继续 |
| `error` | `{"code":"assistant_timeout","message":"助手回答超时，请重试"}` | 丢弃临时预览，提示失败 |

无事件时每 15 秒发一条 SSE 注释保活。分片本身不持久化；未收到 `done` 时重新读取会话详情，不自动重放批准请求。断开会取消仍在运行的 Run 并释放租约；审批恢复中断则记录 `assistant_execution_uncertain`，阻止继续重放，应先查询真实业务状态再新建会话。已提交的消息仍可从 SQL 历史读取。真实模型调用与审批恢复不具备跨进程、跨重启的持久执行保证。

表结构和事务边界见 [会话与消息设计](Agent会话与消息表设计.md)。
