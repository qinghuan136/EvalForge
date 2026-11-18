# EvalForge 评测助手

助手已使用数据库 `ConversationStore`。完整模型消息保存在 `assistant_conversation_entries`，会话 Revision、用量、待审批请求和执行租约保存在 `assistant_conversation_records`。刷新网页或重建后端实例后，都从数据库恢复同一会话。

## 启用

已有核心表且由仓库 Alembic 管理的数据库，启动新版本前执行 `python -m alembic upgrade head`，至少应用 `20261118_conversation_store`。自管迁移链需先接入对应迁移。应用启动时不自动建表。

全新开发库可运行 `Base.metadata.create_all` 创建当前结构，再执行 `python -m alembic stamp head`。配置 `EVALFORGE_ASSISTANT_MODELS`、`EVALFORGE_ASSISTANT_API_KEY`，可选 `EVALFORGE_ASSISTANT_BASE_URL`。默认模型等待 90 秒，执行租约 180 秒，每 60 秒续租。正常完成或暂停后立即释放租约；实例崩溃时，最多约 180 秒后可识别过期状态。配置见 `.env.example`。

## 接口

路径以 `/api/v1/assistant` 开头，沿用登录 Cookie，POST 校验 CSRF。

| 请求 | 响应 |
| --- | --- |
| `POST /sessions`，`{"title":"实验对话"}` | `conversation_id`、`revision`、标题和创建/更新时间 |
| `GET /sessions` | 最近 50 个会话及允许选择的模型 |
| `GET /sessions/{conversation_id}` | 会话摘要、最近 100 条可见消息、状态、待审批 `actions`、错误 |
| `POST /stream` | 普通消息和整批审批共用 SSE 入口 |
| `POST /sessions/{conversation_id}/chat` | 普通消息的 JSON/SSE 入口，也使用 `expected_revision` |

`conversation_id` 和 `revision` 都是字符串。Revision 是不透明令牌，每次成功保存后更新，客户端原样回传。

普通消息请求示例

```json
{
  "type": "message",
  "conversation_id": "创建接口返回的会话 ID",
  "expected_revision": "最近读取的 Revision",
  "message": "创建一个本地开发 Runner",
  "model_name": "允许的模型名称",
  "page_context": {"type":"experiment","id":7}
}
```

`model_name`、`page_context` 可以省略。页面上下文支持 experiment、job、dataset、runner，后端校验资源归属和父子关系。模型看到页面引用和工具查询结果，用户原始文本、页面上下文和数据来源保存在消息的 `metadata.evalforge`。

审批请求示例

```json
{
  "type": "decision",
  "conversation_id": "同一个会话 ID",
  "expected_revision": "暂停后的 Revision",
  "decisions": {"call_001":true,"call_002":false}
}
```

必须一次提交全部待审批调用的布尔决定。服务端根据保存的 Conversation 恢复模型和工具参数。等待审批期间本轮 Run 已结束，不持有执行租约。

## SSE 与保存

| 事件 | 含义 |
| --- | --- |
| `text_delta` | `{"text":"..."}`，临时文字 |
| `approval_required` | `{revision, cards}`，标题、描述、调用 ID 和业务参数 |
| `display` | `{id, tool_call_id, title, description, fields}`，工具产生的展示卡片 |
| `done` | `{"revision":"...","status":"completed 或 awaiting_approval"}` |
| `error` | `{"code":"...","message":"..."}` |

每次 Run 完成或暂停后，在同一短事务中校验执行权、调用 `store.save()` 追加 Entry/更新 Record、更新状态并释放租约，提交成功后发送批准请求和结束事件。工具执行时可通过 display 回调即时发送展示卡片。普通片段不逐条写库。网页通过 sessionStorage 记住当前用户最后选中的会话 ID，刷新后重新读取已保存的审批卡片，未提交的批准/拒绝选择需要重选。

同一会话同时只能有一个有效运行者。领取使用数据库条件更新，心跳只续租，最终保存同时校验运行 ID、有效租约和 Revision。旧运行者无法覆盖新运行者。普通消息中断可重提；审批执行中断或租约过期记录 `assistant_execution_uncertain`，先核对业务状态后新建会话，避免重复写入。

## Runner 凭证与旧历史

完整 Runner 凭证由工具通过 SSE `display` 直接交付，前端按通用字段卡片展示、遮罩和复制。卡片只保存在当前页面内存，刷新后丢失；后续模型或保存失败时仍保留已经收到的卡片。凭证不进入模型消息、数据库 Conversation 或浏览器存储。详细字段及事件顺序见 [助手 SSE 与卡片协议](assistant-sse.md)。

旧 `assistant_sessions`、`assistant_messages` 保留。读取会话列表时将旧 SQL 历史幂等导入为 `legacy-{id}` 的只读会话，原数字详情路径也可读取。旧 fake 的运行现场无法从历史消息可靠还原，因此旧会话提示新建后继续。新运行只写 Record/Entry，旧表清理另行安排。

17 个工具及其参数见 [基础工具](assistant-tools.md)，Backend 方法和表结构见 [ConversationStore 方案](ConversationStore方案.md)。
