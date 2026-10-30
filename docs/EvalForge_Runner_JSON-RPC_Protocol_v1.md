# EvalForge Cloud ↔ Runner 通信协议（V1 草案）

## 1. 约定

- **传输**：Runner 主动连接 Cloud 的 **WSS**；连接建立后，双方通过 **JSON-RPC 2.0** 双向调用。
- **认证**：WSS 连接时校验 Runner 凭据；`runner_id` 必须与认证身份一致。`runner.hello` 不替代鉴权。
- **执行边界**：Cloud 负责任务调度、状态和评测；Runner 仅调用本地预配置的目标 HTTP API，并回传原始响应。**Dataset 的参考输出（expected output）留在云端**。
- **V1 限制**：一个 Runner 同时执行一个 Job；一个 Job 对应一条 Dataset Example。云端不得下发任意本地 URL 或 Shell 命令。
- **标识**：`runner_id`（设备）、`session_id`（连接会话）、`job_id`（任务）、`attempt_id`（执行尝试）、`target_id`（本地目标）、`example_id`（样本）。JSON-RPC `id` 仅关联一次请求/响应，**不是 Job ID**。

所有需要确认的操作采用 JSON-RPC **Request/Response**（含 `id`）；允许丢失的进度事件可采用 **Notification**（无 `id`）。

## 2. 核心方法（第一阶段）

| 方法 | 方向 | 语义 | 响应 |
| --- | --- | --- | --- |
| `runner.hello` | Runner → Cloud | 认证后声明版本、能力和执行容量 | `session_id`、`status` |
| `jobs.dispatch` | Cloud → Runner | 派发一次执行尝试 | `accepted`、可选 `reason` |
| `jobs.started` | Runner → Cloud | 确认开始执行 | `acknowledged` |
| `jobs.cancel` | Cloud → Runner | 请求取消执行 | `accepted`（仅确认收到取消请求） |
| `jobs.complete` | Runner → Cloud | 提交最终执行结果 | `acknowledged`（须在云端持久化成功后返回） |
| `runner.sync` | Runner → Cloud | 重连后对齐未完成任务和待确认结果 | 各任务的后续操作 |

### 2.1 `runner.hello`

```json
{"jsonrpc":"2.0","method":"runner.hello","params":{"runner_id":"runner-01","version":"0.1.0","capabilities":["http_json"],"max_concurrent_jobs":1},"id":1}
```

```json
{"jsonrpc":"2.0","result":{"session_id":"session-01","status":"ready"},"id":1}
```

### 2.2 `jobs.dispatch`

```json
{"jsonrpc":"2.0","method":"jobs.dispatch","params":{"job_id":"job-01","attempt_id":"a-01","target_id":"order-agent","example_id":"ex-01","input":{"message":"查询订单1024"},"timeout_seconds":60},"id":2}
```

```json
{"jsonrpc":"2.0","result":{"accepted":true},"id":2}
```

- `accepted=true` 仅表示 Runner 已接收该尝试，**不代表执行成功**。
- Runner 若繁忙或未配置 `target_id`，返回 `accepted=false`，`reason` 可为 `busy` / `target_not_found`；Cloud 负责重新调度。
- `input` **原样作为目标 HTTP JSON 请求体**；目标 URL、鉴权等由 Runner 的本地配置决定。

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

- `accepted=true` 仅表示已接收取消指令；最终需通过 `jobs.complete(status="cancelled")` 报告。
- 取消是**尽力而为**：可停止后续执行、尝试中止当前 HTTP 等待，但不能保证目标 Agent 已停止或撤销副作用。

### 2.5 `jobs.complete`

```json
{"jsonrpc":"2.0","method":"jobs.complete","params":{"job_id":"job-01","attempt_id":"a-01","status":"succeeded","output":{"reply":"订单尚未发货"},"duration_ms":850,"http_status":200,"error":null},"id":5}
```

```json
{"jsonrpc":"2.0","result":{"acknowledged":true},"id":5}
```

- `status`：`succeeded` / `failed` / `cancelled`；失败时 `error` 为 `{ "code": "timeout", "message": "..." }` 等结构化信息。
- `output` 是 Agent **本次实际响应**，不是 Dataset 的预期输出。
- Cloud 按 `job_id + attempt_id` **幂等保存**结果；Runner 在收到确认前应保留结果并可重试上报。
- **业务执行失败**放在 `status=failed`；**RPC 请求无效**才使用 JSON-RPC 的 `error`。

### 2.6 `runner.sync`

```json
{"jsonrpc":"2.0","method":"runner.sync","params":{"runner_id":"runner-01","active_jobs":[{"job_id":"job-01","attempt_id":"a-01","status":"running"}],"pending_results":[]},"id":6}
```

```json
{"jsonrpc":"2.0","result":{"jobs":[{"job_id":"job-01","attempt_id":"a-01","action":"continue"}]},"id":6}
```

- `action`：`continue`（允许继续）、`cancel`（停止）、`discard`（本次尝试失效）、`retry_upload`（重传本地待确认结果）。
- 重连后先 `runner.hello` 再 `runner.sync`，完成同步后再接受新 Job。
- 同一 Runner 的新会话接管旧会话；旧连接不可继续派发或上报不属于它的尝试。

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
