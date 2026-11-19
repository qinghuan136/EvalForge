# 评测助手 SSE 与卡片协议

普通消息、审批恢复共用 `POST /api/v1/assistant/stream`。请求使用 `conversation_id`、`expected_revision`，以及 `type=message` 或 `type=decision`。登录、CSRF、会话版本与执行权冲突在发送 HTTP 响应头前校验。接入参数见 [评测助手](assistant.md)。

每帧使用 `event` 和 JSON `data`，以空行结束。空闲 15 秒发送 `: keep-alive` 注释。工具事件复用同一连接，旧前端可忽略尚未识别的事件。

| 事件 | 用途 | 发送时机 |
| --- | --- | --- |
| `text_delta` | 追加临时文字 | 模型生成文字时 |
| `tool_call` | 展示工具调用 | PydanticAI 发出 FunctionToolCallEvent 时 |
| `tool_result` | 展示工具结果 | PydanticAI 发出 FunctionToolResultEvent 时 |
| `tool_deferred` | 标记调用等待交互 | PydanticAI 发出 DeferredToolRequestsEvent 时 |
| `approval_required` | 展示一批批准卡片 | Conversation 和待审批调用提交成功后 |
| `display` | 展示工具产生的信息卡片 | 工具执行过程中，数据已经产生时 |
| `done` | 确认本轮已保存并结束 | Conversation 提交成功后 |
| `error` | 报告本轮失败并结束 | 流开始后的执行异常 |

## text_delta

```json
{"text":"正在检查实验进度。"}
```

前端追加到本轮临时预览。正式消息通过会话详情获取，`done` 后刷新。历史读取成功并出现新 Revision 后，预览由历史替换；读取失败保留预览并提供重读按钮。流执行失败时已有文字标记为“本轮未确认保存”，不当作已持久化消息；切换会话或开始下一轮时清理本轮预览。

## 实时工具事件

Agent 运行入口统一消费 `event_stream_handler`，经过展示过滤后送入现有有界 asyncio.Queue，再输出 SSE；工具函数不承担 SSE 转发。没有新增连接、持久化队列或逐事件数据库写入。

`tool_call` 示例：

```json
{
  "conversation_id":"会话 ID", "run_id":"本轮运行 ID", "args_valid":true,
  "part":{"part_kind":"tool-call", "tool_name":"RUNNER_list_runners", "tool_call_id":"call_1", "args":{"page":1}}
}
```

`tool_result` 示例：

```json
{
  "conversation_id":"会话 ID", "run_id":"本轮运行 ID",
  "part":{"part_kind":"tool-return", "tool_name":"RUNNER_list_runners", "tool_call_id":"call_1",
          "content":{"items":[]}, "outcome":"success", "timestamp":"2026-11-19T08:01:00Z"}
}
```

`tool_deferred` 示例：

```json
{
  "conversation_id":"会话 ID", "run_id":"本轮运行 ID",
  "requests":{"calls":[], "approvals":[
    {"part_kind":"tool-call", "tool_name":"RUNNER_create_runner", "tool_call_id":"call_2", "args":{"name":"本地开发"}}
  ]}
}
```

- `part` 保留原生 Part 结构；历史 Conversation 的同一 Part 使用相同过滤规则。args 可能是 JSON 对象、JSON 字符串或 null。ToolReturn 的 outcome 可为 success / failed / denied / interrupted / retried；工具返回的业务错误也可能位于 content.error，不能只看 outcome。
- `args_valid=true` 表示本次参数校验通过，false 表示失败，null 表示本步没有执行参数校验；调用事件不保证工具函数已经执行。Deferred 调用同样有调用事件，收到 `tool_deferred` 后标记等待交互。被拒绝的调用可收到 outcome=denied 的结果，不能显示成已执行成功。
- 并发工具按 `tool_call_id` 配对，不按到达顺序配对。run_id 与本次 Agent Run 保存到消息里的 ID 相同；审批恢复开启新 Run，同一 tool_call_id 可再次出现，前端更新原调用的状态，不重复计数。
- 事件是临时预览，不代表数据库已提交。`tool_deferred` 不携带可提交的 Revision，不代替保存后发出的 `approval_required`。`done` 后重新读取详情中的 Conversation，替换本轮预览，避免重复展示。
- 仅选择展示字段，不导出 provider/工具内部 metadata，不检查或替换 args/content 内的业务数据。完整 Runner 凭据仍由业务逻辑通过现有 `display` 卡片交付，不进入模型历史。
- 本期不提供工具内部百分比进度、SSE 重放或断线续传；中断后读取已保存的会话状态，不自动重放写操作。

## approval_required

```json
{
  "revision":"暂停后保存的 Revision",
  "cards":[{
    "tool_call_id":"call_123",
    "tool":"RUNNER_create_runner",
    "title":"批准请求",
    "description":"为本地测试创建 Runner。",
    "arguments":{"name":"本地开发"}
  }]
}
```

- 同批卡片共用外层 Revision。每张卡片含调用 ID、工具名、固定标题、描述和实际业务参数。
- 五个写工具可附 `approval_description`，最多 500 字符，由模型解释操作目的。后端从展示参数中移出该字段，将它作为描述。省略时按工具名和目标生成默认描述。
- 描述仅供阅读。执行使用保存的原生 Deferred Tools 调用，前端只提交调用 ID 对应的布尔决定。
- 前端按已支持的工具类型渲染卡片，逐项选择批准或拒绝，全部选择后一次提交。当前流仍在执行时禁用提交。
- 该事件只在存储提交后发送，可以直接用于展示；`done` 负责结束本轮。会话详情的 `actions` 返回同样的卡片结构，刷新后从数据库重建。

审批恢复请求

```json
{
  "type":"decision",
  "conversation_id":"当前会话 ID",
  "expected_revision":"卡片所属 Revision",
  "decisions":{"call_123":true}
}
```

## display

```json
{
  "id":"runner-credential:call_123",
  "tool_call_id":"call_123",
  "title":"Runner 已创建",
  "description":"请保存连接凭证，用于配置本地 Runner。刷新页面后无法再次查看。",
  "fields":[
    {"label":"Runner ID","value":"runner_001","sensitive":false},
    {"label":"连接凭证","value":"efr.example","sensitive":true},
    {"label":"连接地址","value":"wss://example.com/api/v1/runner/ws","sensitive":false}
  ]
}
```

- `id` 为同一会话中的卡片标识，前端据此替换重复卡片。`tool_call_id` 关联来源工具调用。
- `title`、`description` 是显示文本。`fields` 为至少一项的字段列表，每项包含 `label`、字符串 `value`、布尔 `sensitive`，后者默认 `false`。
- 前端逐项展示、支持复制。敏感字段默认遮罩，可显示和重新隐藏。文本按普通文字渲染。
- 创建 Runner 的工具把完整凭证交给后端注入的 `display` 回调，回调放入当前 SSE 队列。模型只收到 Runner 摘要。
- 展示卡片保存在当前页面的 React 内存中，按会话隔离。凭证不写入 Conversation、查询缓存或浏览器存储。此链路直接通过当前 SSE 交付，已移除凭证 Redis 暂存、`credential_call_ids` 和独立领取接口。
- `display` 表示这份业务结果已经产生。随后模型失败、保存失败或流中断时，前端保留已经收到的卡片，并提示本轮失败。它不保证最终 Conversation 已提交。
- 刷新页面会丢失卡片。若凭证生成后连接中断且浏览器没有收到，应先查询 Runner 状态，必要时删除并重建，避免直接重放审批。

## done

```json
{"revision":"本轮保存后的 Revision","status":"completed"}
```

`status` 为 `completed` 或 `awaiting_approval`。事件仅携带这两个字段。前端结束本轮并刷新会话详情，正式消息从详情读取。待审批数据由 `approval_required` 和会话详情提供。

普通 JSON 聊天入口继续返回 `AssistantChatResponse`，包含本轮消息和待审批卡片；SSE `done` 使用独立的精简模型。

## error

```json
{"code":"assistant_timeout","message":"助手回答超时，请重试"}
```

发送后关闭流。前端保留本轮已收到的 `display` 卡片，刷新会话状态，再按错误状态继续处理。`error` 后不发送 `done`，断线也不自动重放审批。

## 顺序与验证

```text
普通回答        text_delta* → done(completed)
读取工具        tool_call → tool_result → text_delta* → done(completed)
等待审批        tool_call → tool_deferred → approval_required → done(awaiting_approval)
批准后创建      tool_call → display → tool_result → text_delta* → done(completed)
工具成功后失败  tool_call → display → tool_result → error
```

`text_delta*` 表示零到多条文字片段。一次 Run 可以产生多张展示卡片，也可以再次产生待审批调用。审批等待期间 Run 和 SSE 已结束，用户决定到达后开启新的 Run。

协议和业务回归见 `tests/test_assistant_api.py`、`tests/test_assistant_deferred.py`、`tests/test_assistant_presentation.py`、`tests/test_assistant_restart.py`。Job 输出预览和本地 Agent SSE 沿用各自协议。
