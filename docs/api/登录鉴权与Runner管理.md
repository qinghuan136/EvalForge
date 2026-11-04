# 登录鉴权与 Runner 管理接口

状态：浏览器鉴权、Runner 管理、WSS 认证和 JSON-RPC 基础设施已实现，业务会话及调度后续接入。启动见 [鉴权服务](../server-auth.md)、[Runner 管理](../server-runners.md)、[WSS 接入](../server-wss.md)。分页采用 page + pagesize；Runner 与凭证一对一，创建时签发，删除后重建。

## 通用约定

- REST 前缀：`/api/v1`；请求与响应使用 JSON；时间使用 UTC ISO 8601。
- 浏览器使用有状态登录 Token，由 HttpOnly Cookie 携带；会话保存在 Redis，默认绝对有效期 24 小时，暂不自动续期。
- 生产 Cookie：`__Host-evalforge_session`，设置 `Secure; HttpOnly; SameSite=Lax; Path=/`。
- 浏览器的 POST / PATCH / DELETE 请求均携带 `X-CSRF-Token`，包含注册与登录；云端限制允许的来源。前端自动获取并携带，用户无需配置。
- Runner 使用独立的 Bearer Token，仅用于客户端连接；不能用于浏览器管理接口。
- 成功直接返回资源；204 无响应体。登录和含完整凭证的响应设置 `Cache-Control: no-store`。
- 用户信息仅返回 id、username、display_name，不返回密码摘要。

## 登录鉴权

| 方法 | 路径 | 请求体 | 成功响应 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/auth/csrf` | 无 | 200：`{csrf_token}` | 获取 CSRF Token；必要时建立匿名会话，不能据此视为已登录 |
| POST | `/auth/register` | `{username, password, display_name?}` | 201：`{user}` | 注册成功后另行登录 |
| POST | `/auth/login` | `{username, password}` | 200：`{user, session_expires_at, csrf_token}` | 写入登录 Cookie，并更换会话标识与 CSRF Token |
| GET | `/auth/me` | 无 | 200：`{user, session_expires_at}` | 获取当前登录信息；页面重载后可另取 CSRF Token |
| POST | `/auth/logout` | 无 | 204 | 注销当前会话，清除 Cookie；无有效会话时也可清除 Cookie并返回 204 |
| POST | `/auth/logout-all` | 无 | 204 | 注销全部浏览器会话，含当前会话，并清除 Cookie |
| POST | `/auth/password` | `{current_password, new_password}` | 204 | 修改密码、递增 auth_version、注销全部浏览器会话并清除 Cookie |

`?` 表示字段可选。register / login 不要求已登录；me / logout-all / password 要求有效登录。会话操作仍需按通用约定校验 CSRF。

前端流程：`GET csrf → register（可选）→ login → 使用新 csrf_token 访问管理接口`。
CSRF 用于防止其他网站借浏览器自动携带的 Cookie 发起伪造操作，不是另一份登录凭证。

登录失败统一提示“用户名或密码错误”。修改密码、普通退出和会话过期不撤销 Runner 凭证；账号禁用后，浏览器访问与所属 Runner 连接均失效。
用户名写入前去除首尾空白并转小写，长度 3–64，只允许字母、数字、下划线、点和连字符，首字符须为字母或数字。新密码 15–128 字符，不截断或去除空白；display_name 可选，非空白且最多 100 字符。
登录按 IP 和标准化用户名分别限制每分钟 10 次；注册每 IP 每 5 分钟 5 次；改密码每用户每 5 分钟 5 次；CSRF 获取每 IP 每分钟 60 次。所有写请求要求受信任 Origin 或同源 Referer；无有效会话的重复退出仍须提供合法格式的 CSRF 请求头。

## Runner 管理

以下接口均要求浏览器登录，只能管理自己的 Runner。owner_user_id 由云端取得，客户端不能指定。

| 方法 | 路径 | 请求 | 成功响应 |
| --- | --- | --- | --- |
| POST | `/runners` | `{name}` | 201：`{runner, credential, connection}` |
| GET | `/runners` | 查询参数 `page=1&pagesize=20` | 200：`{items, page, pagesize, total}` |
| GET | `/runners/{runner_id}` | 无 | 200：Runner 详情及连接状态 |
| PATCH | `/runners/{runner_id}` | `{name}` | 200：更新后的 Runner；仅允许改名 |
| DELETE | `/runners/{runner_id}` | 无 | 204：逻辑删除、凭证撤销、当前会话失效 |

分页：page 范围 1–2147483647，pagesize 默认 20、范围 1–100；列表排除已删除记录，按 created_at 降序、runner_id 降序稳定排序。不冻结跨请求分页快照。

创建响应示例：

```json
{
  "runner": {
    "runner_id": "runner-uuid",
    "name": "我的开发机",
    "enabled": true,
    "created_at": "2026-11-03T08:00:00Z"
  },
  "credential": {
    "id": 12,
    "token_id": "public-lookup-id",
    "token": "efr.public-lookup-id.random-secret",
    "expires_at": "2036-11-03T08:00:00Z"
  },
  "connection": {
    "websocket_url": "wss://evalforge.example.com/api/v1/runner/ws"
  }
}
```

- 创建 Runner 与初始凭证在同一数据库事务内完成，默认有效期十个日历年。
- name 去除首尾空白后须为 1–100 字符；连接地址由 EVALFORGE_RUNNER_WS_URL 指定，不从请求 Host 推断。
- 完整 Token 仅创建时返回，数据库只存摘要。查询及改名响应不含 Token 或摘要；丢失、泄露或到期时删除旧 Runner，再创建新的 Runner。
- 不提供凭证列表、凭证更新、重置、单独撤销接口；PATCH 不接受 enabled、归属或凭证字段。
- Runner 列表项与详情使用相同结构：runner_id、name、enabled、created_at、updated_at、last_seen_at、connection。
- connection 包含 online 和 phase，来自 Redis 注册表；phase 为 connected / syncing / ready，缺失时为 disconnected。会话存储不可用或记录损坏时返回 503，不能误报离线。在线不代表有空闲容量；WSS 写入方尚未接入，目前正常无注册记录时显示离线。
- 删除采用 deleted_at 标记，保留凭证记录及 Attempt 历史，不提供恢复；重建后 runner_id 和 Token 都不同。自己的已删除 Runner 重复删除返回 204，详情返回 404。
- 删除使连接及后续操作失效，但不保证本地 Agent 停止；在途 Attempt 按取消 / 状态恢复规则处理，不能仅凭断连判定成功或失败。
- 删除已实现持久化禁用 / 撤销及 Redis 注册清理，WSS 授权复查关闭连接；多连接接管与旧会话业务写入隔离后续接入。Redis 清理失败返回 503，数据库撤销仍有效；重复 DELETE 可重试清理。

## Runner 连接

入口：`wss://<cloud>/api/v1/runner/ws`，握手时携带：

```http
Authorization: Bearer efr.<token_id>.<secret>
```

- 云端从凭证确定 Runner 和用户；检查摘要、到期与撤销时间、Runner 未删除且启用、用户启用。
- runner.hello 中的 runner_id 必须与认证身份一致；通过 hello 和状态同步后，ready 才能接单。
- 同一 Runner 只允许一个当前有效会话；新连接接管后，旧会话失效，不得继续业务写入。
- 凭证到期、撤销或身份禁用 / 删除时，现有连接也要失效。Token 不放在 URL 中。
- WSS 消息沿用 [Runner JSON-RPC 协议](../EvalForge_Runner_JSON-RPC_Protocol_v1.md)，不另设客户端登录 REST 接口。

## 错误约定

```json
{"error": {"code": "runner_not_found", "message": "Runner 不存在"}}
```

| HTTP 状态 | 场景 |
| --- | --- |
| 401 | 登录失败、未登录、会话失效、Runner 凭证无效 |
| 403 | CSRF 失败、身份被禁用而禁止操作 |
| 404 | 资源不存在、已删除，或不属于当前用户 |
| 409 | 用户名已存在 |
| 422 | 字段或分页参数不合法，或提交不允许修改的字段 |
| 429 | 注册 / 登录等请求超过频率限制 |
| 503 | 必要的数据库或 Redis 服务不可用 |

不向调用方暴露密码、Token、摘要或底层异常。WSS 建立前按 HTTP 拒绝握手，建立后的协议错误沿用 JSON-RPC 约定。

## 模型衔接

现有三表见 [鉴权数据设计](../auth-schema.md)。ORM 已添加 runner_credentials.runner_id 唯一约束及 runners.deleted_at；已有数据库仍需生成并执行迁移，create_all 不会升级旧表。
本阶段不包含邮箱验证、找回密码、角色权限、凭证轮换和 Runner 恢复接口。
