# EvalForge Cloud ↔ Runner 通信协议（V1 草案）

## 1. 约定

- **传输**：Runner 主动连接 Cloud 的 **WSS**；连接建立后，双方通过 **JSON-RPC 2.0** 双向调用。
- **认证**：WSS 连接时校验 Runner 凭据；`runner_id` 必须与认证身份一致。`runner.hello` 不替代鉴权。
- **执行边界**：Cloud 负责任务调度、状态和评测；Runner 仅调用本地预配置的目标 HTTP API，并回传原始响应。**Dataset 的参考输出（expected output）留在云端**。
- **V1 限制**：一个 Runner 同时执行一个 Job；一个 Job 对应一条 Dataset Example。云端不得下发任意本地 URL 或 Shell 命令。
- **标识**：`runner_id`（设备）、`session_id`（连接会话）、`job_id`（任务）、`attempt_id`（执行尝试）、`agent_target_id`（本地 Agent 目标）、`example_id`（样本）。JSON-RPC `id` 仅关联一次请求/响应，**不是 Job ID**。

所有需要确认的操作采用 JSON-RPC **Request/Response**（含 `id`）；允许丢失的进度事件可采用 **Notification**（无 `id`）。

## 2. 核心方法（第一阶段）

| 方法 | 方向 | 语义 | 响应 |
| --- | --- | --- | --- |
| `runner.hello` | Runner → Cloud | 认证后声明版本、能力和执行容量 | `session_id`、`status` |
| `jobs.dispatch` | Cloud → Runner | 派发一次执行尝试 | `accepted`、可选 `reason` |
| `jobs.started` | Runner → Cloud | 确认开始执行 | `acknowledged` |
| `jobs.output_chunk` | Runner → Cloud | 上报一批流式预览片段 | 无 |
| `jobs.cancel` | Cloud → Runner | 请求取消执行 | `accepted`（仅确认收到取消请求） |
| `jobs.complete` | Runner → Cloud | 提交最终执行结果 | `acknowledged`（须在云端持久化成功后返回） |
| `runner.sync` | Runner → Cloud | 重连后对齐未完成任务和待确认结果 | 各任务的后续操作 |
| `runner.ready` | Runner → Cloud | 明确确认同步完成 | `session_id`、`status=ready` |

### 2.1 `runner.hello`

```json
{"jsonrpc":"2.0","method":"runner.hello","params":{"runner_id":"runner-01","version":"0.1.0","capabilities":["http_json"],"max_concurrent_jobs":1},"id":1}
```

```json
{"jsonrpc":"2.0","result":{"session_id":"session-01","status":"ready"},"id":1}
```

- 云端已实现 hello 注册、租约和新连接接管，真实会话阶段仍为 syncing。响应中的 status=ready 仅兼容握手约定，不允许提前派发任务。
- 云端根据当前会话的真实 ready 阶段和 Runner 的执行容量决定何时派发。参见 [Attempt 业务与同步](server-attempts.md)。

### 2.2 `jobs.dispatch`

```json
{"jsonrpc":"2.0","method":"jobs.dispatch","params":{"job_id":"job-01","attempt_id":"a-01","agent_target_id":"order-agent","example_id":"ex-01","input":{"message":"查询订单1024"},"timeout_seconds":60},"id":2}
```

```json
{"jsonrpc":"2.0","result":{"accepted":true},"id":2}
```

- `accepted=true` 仅表示 Runner 已接收该尝试，**不代表执行成功**。
- Runner 先持久化再确认接单。同一 job_id + attempt_id 的相同参数重复派发正常确认且不重复执行；参数不一致返回 accepted=false、reason=attempt_conflict。先去重，再判断容量。
- `accepted=false` 且原因为 `busy` 或 `not_ready` 时，Cloud 将 Job 放回队列并延迟再尝试。目标不存在或执行层不可用时，Runner 分别返回 `target_not_found` 或 `executor_unavailable`，Cloud 将 Job 标记为 failed。回执超时、连接断开或回执格式无效时，仍处于 assigned 的 Job 和 Attempt 转为 `unknown`，释放容量并触发重连对账。
- `input` **原样作为目标 HTTP JSON 请求体**；目标 URL、鉴权等由 Runner 的本地配置决定。
- 可选布尔字段 `stream_output` 默认 `false`，参与重复派发校验。仅对声明 `http_sse` 的 Runner 派发 `stream_output: true`；普通派发可省略该字段。

### 2.3 `jobs.started`

```json
{"jsonrpc":"2.0","method":"jobs.started","params":{"job_id":"job-01","attempt_id":"a-01"},"id":3}
```

```json
{"jsonrpc":"2.0","result":{"acknowledged":true},"id":3}
```

### 2.4 `jobs.cancel`

```json
{"jsonrpc":"2.0","method":"jobs.cancel","params":{"job_id":"job-01","attempt_id":"a-01","reason":"user_requested"},"id":4}
```

```json
{"jsonrpc":"2.0","result":{"accepted":true},"id":4}
```

- `accepted=true` 仅表示取消请求已接收并持久化。取消生效后通过 `jobs.complete(status="cancelled")` 报告；若已有执行结果则保留原结果，unknown 仅保留取消意图。
- 用户取消实验时，Cloud 先持久化意图，再由持有会话的 Scheduler 发送指令。失败回执按调度退避重试，断线时通过 `runner.sync` 接续。Cloud 收到取消接收回执后继续等待最终结果。
- 取消是**尽力而为**：可停止后续执行、尝试中止当前 HTTP 等待，但不能保证目标 Agent 已停止或撤销副作用。
- 当前实现只停止客户端 HTTP 等待，不调用 Agent 取消接口。重复取消幂等；已获得响应后继续完成结果落盘，迟到取消不覆盖原结果。

### 2.5 `jobs.complete`

```json
{"jsonrpc":"2.0","method":"jobs.complete","params":{"job_id":"job-01","attempt_id":"a-01","status":"succeeded","output":{"reply":"订单尚未发货"},"duration_ms":850,"http_status":200,"error":null},"id":5}
```

```json
{"jsonrpc":"2.0","result":{"acknowledged":true},"id":5}
```

- `status`：`succeeded` / `failed` / `cancelled`；失败时 `error` 为 `{ "code": "timeout", "message": "..." }` 等结构化信息。
- `output` 是 Agent **本次实际输出**（流式模式取终止 SSE `result` 事件中的 JSON），不是 Dataset 的预期输出。
- Cloud 按 `job_id + attempt_id` **幂等保存**结果；Runner 在收到确认前应保留结果并可重试上报。
- **业务执行失败**放在 `status=failed`；**RPC 请求无效**才使用 JSON-RPC 的 `error`。

### 2.6 `runner.sync`

```json
{"jsonrpc":"2.0","method":"runner.sync","params":{"runner_id":"runner-01","active_jobs":[{"job_id":"job-01","attempt_id":"a-01","status":"running"}],"pending_results":[]},"id":6}
```

```json
{"jsonrpc":"2.0","result":{"jobs":[{"job_id":"job-01","attempt_id":"a-01","action":"continue"}]},"id":6}
```

- 当前 `action`：`continue`（允许继续）、`cancel`（请求停止）、`retry_upload`（重传本地待确认结果）；`discard` 暂不实现。
- `active_jobs` 上报本地 `accepted / running`。客户端进程启动将遗留 `running` 转为 `unknown` 并保留在本地历史。云端兼容旧客户端上报的单条 `unknown`。已被云端跳过的 Attempt 收到活跃快照时返回 `cancel`，最终结果仍可补传。
- `pending_results` 为保存的完整 `jobs.complete` 业务参数列表，不含 JSON-RPC 封装。Cloud 须对快照中每个 Attempt 返回一条指令；历史待确认结果须明确返回 retry_upload，云端即使已存结果，也可幂等接收后重新确认。
- 客户端完整校验指令后应用；遗漏、重复、未知 ID、不支持动作或状态冲突使本轮同步失败。快照后新增的最终结果等 ready 后按正常流程上报，不需要再次 sync。
- hello 的 status=ready 仅表示握手通过；客户端完成 sync、补传历史结果后请求 runner.ready。同步完成与执行容量是否空闲分开判断；默认入口声明 `http_json` 和 `http_sse`，未注入执行层的独立会话 capabilities 为空并拒绝派发。
- 重连后先 runner.hello，再 runner.sync，再 runner.ready。每个连接只成功处理一次 sync；数据库保存失败返回 RPC 错误，不自动重试。
- 同一 Runner 的新会话接管旧会话；旧连接不可继续派发或上报不属于它的尝试。

### 2.7 `runner.ready`

```json
{"jsonrpc":"2.0","method":"runner.ready","params":{"session_id":"session-01"},"id":7}
```

```json
{"jsonrpc":"2.0","result":{"session_id":"session-01","status":"ready"},"id":7}
```

- 云端校验当前有效会话、sync 已处理及历史待补传结果全部入库后，条件更新 Redis 会话阶段并确认。重复 ready 幂等。
- active Job 不需要执行结束，但仍占用容量。客户端先打开本地接单门禁再发 ready；ready 失败则结束会话。

### 2.8 `jobs.output_chunk`

```json
{"jsonrpc":"2.0","method":"jobs.output_chunk","params":{"job_id":"job-01","attempt_id":"a-01","chunks":[{"seq":1,"text":"订单"},{"seq":2,"text":"尚未发货"}]}}
```

- 每个 Attempt 的 `seq` 从 1 递增。通知没有 `id` 和 ACK，首批可以早于 `jobs.started`；断线或缓冲满时可能缺片，预览不补传。
- 每批 1–16 片，序号严格递增，单片文本最多 2,048 字符。
- Cloud 只接收当前连接在派发时登记的流式 Attempt。通知只更新 Redis 预览并向实验频道发布，执行状态和最终输出仍由 `jobs.started` 和 `jobs.complete` 更新。

## 3. 状态与可靠性

```text
queued → assigned → running → succeeded / failed / cancelled
                    ↘ cancel_requested → cancelled / succeeded / failed
```

- `cancel_requested` 是取消意图，不是终态；可能在取消生效前已执行完成。
- Runner 断线时，Cloud 将 Job 标记为待确认/失联状态，**不能直接认定失败或立即重试**。
- `job_id + attempt_id` 区分不同执行尝试；超时或失联恢复由 Cloud 裁定。存在外部副作用的 Agent 调用不应默认自动重试。
- 连接存活由 WebSocket Ping/Pong 检测，**不能替代 Job 状态确认**。
- 最小实现应保证：派发前记录任务归属、完成后持久化再确认、重连同步以及重复上报幂等。

## 4. 可选扩展（第二阶段）

| 方法 | 方向 | 用途 |
| --- | --- | --- |
| `jobs.heartbeat` | Runner → Cloud | 上报运行中尝试的存活状态、续约租约（有 `id`） |
| `jobs.progress` | Runner → Cloud | 上传已完成/总数等进度；可使用 Notification |

## 5. 错误处理

遵循 JSON-RPC 2.0 错误格式，例如：

```json
{"jsonrpc":"2.0","error":{"code":-32602,"message":"Invalid params"},"id":2}
```

错误码遵循 JSON-RPC 标准；业务拒绝（例如 Runner 繁忙）优先使用正常响应中的 `accepted=false`。协议版本升级、消息大小限制、超时和日志脱敏可在实施阶段进一步约定。
