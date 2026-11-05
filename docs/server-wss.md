# Runner WSS 与 JSON-RPC 基础设施

已实现 `/api/v1/runner/ws` 的握手认证、文本传输及双向 JSON-RPC。应用入口沿用 `evalforge.server.app:create_app`。

已接入 [Runner 会话管理基础](server-sessions.md)：hello、Redis 租约、连接接管与清理，阶段由状态机统一判断。hello 成功后真实阶段为 syncing；仅建立底层连接、尚未注册时显示离线，hello 注册提交期间可短暂显示 connected。sync / ready 和任务业务后续接入。

## 文件与链路

```text
routes/runner_ws.py
  → RunnerAuth.authenticate：升级前检查 Bearer 凭证
  → WebSocketConnection：ASGI 文本通道、消息边界和发送互斥
  → JsonRpcPeer：双向调用、响应关联、错误、超时与关闭
```

| 文件 | 职责 |
| --- | --- |
| evalforge/server/runner_auth.py | 解析凭证、PG 查询、常量时间摘要比较、绑定 RunnerIdentity、连接期间复查 |
| evalforge/server/ws_transport.py | 单读取者、串行发送、UTF-8 文本与 1 MiB 限制、关闭与认证保护 |
| evalforge/server/rpc.py | 从现有客户端移植，使用 jsonrpcserver / jsonrpcclient；云端无需安装客户端包 |
| evalforge/server/routes/runner_ws.py | 握手拒绝、接入、方法注册入口、授权监控及清理 |
| evalforge/server/app.py | 组装 RunnerAuth 与路由，提供 configure_runner_rpc 注册回调 |

## 认证与授权失效

- 客户端只通过 `Authorization: Bearer efr.<token_id>.<secret>` 提供凭证；不接受 Cookie、URL 参数或 hello 替代认证。
- 根据 token_id 查询 Credential → Runner → User，比较 secret 的 SHA-256，检查凭证未过期 / 撤销、Runner 未删除且启用、所属用户启用。
- 连接绑定 runner_id、owner_user_id、credential_id、到期时间和摘要；后续不能通过 RPC 参数更换认证身份。握手成功校验时更新 last_used_at / last_seen_at。
- 每次接收消息和发送消息前重校验；空闲时默认每 5 秒查询一次，接近原到期时间时提前检查。修改 `EVALFORGE_RUNNER_AUTH_CHECK_SECONDS` 可调整间隔，实际失效检测还受数据库查询耗时影响。
- 凭证撤销、Runner 删除 / 禁用、账号禁用或到期关闭码为 1008；数据库不可用时关闭 1011。浏览器改密和退出全部登录不撤销 Runner 凭证。
- hello 注册后，每次消息收发和空闲监控还会检查 Redis 当前会话身份。消息进入 RPC 前的检查不能替代后续业务事务内的归属、会话版本与 Attempt 校验。

## 传输和 RPC 边界

- 公开入口要求 WSS。显式设置 EVALFORGE_COOKIE_SECURE=false 后，仅 loopback 对端和 loopback Host 可使用本机 WS。
- 原生 Python 客户端不发送 Origin 时可以连接；若发送，则必须精确匹配 EVALFORGE_ALLOWED_ORIGINS。重复 Authorization、非法或重复 Origin 均拒绝。
- 升级前凭证错误返回 401，身份禁用返回 403，认证存储不可用返回 503；拒绝响应脱敏并禁止缓存。ASGI 服务不支持 denial 扩展时，降级为升级前 HTTP 403 拒绝。
- 仅接收文本，二进制关闭 1003；非法文本关闭 1007；超过 1 MiB 关闭 1009。发送也检查大小；部署时应同时配置 ASGI 后端的帧 / 消息限制。
- Ping/Pong 由 WebSocket 实现处理，不增加 JSON 业务心跳。客户端已有 Ping/Pong，服务启动可配置下述 Uvicorn 保活参数。
- 支持 request / response、notification、batch、反向调用；接收循环先匹配响应，处理函数在受限任务中执行，避免反向调用死锁。
- 默认最多 128 个待响应调用、32 个并行入站处理任务、32 条 batch 元素。请求 ID 只关联 RPC，不是 job_id / attempt_id。
- call 默认超时 30 秒；超时 / 取消释放本地等待，迟到响应忽略，不意味着远端操作被撤销。断线使等待失败并回收处理任务。
- 库负责 JSON-RPC 标准解析与分发，适配层补严格 JSON 2.0、非有限数、容量控制和异常脱敏；普通异常返回 -32603，不回显异常文本。
- 关闭时停止授权监控；已开始的短认证事务完成清理后才传播取消，避免遗留数据库关闭任务。

会话层默认注册 runner.hello。工厂参数 `configure_runner_rpc(peer, identity)` 可注册其他 async 方法；回调本身同步执行，每条连接独立调用，不应覆盖 hello。生产默认无测试 echo 或任务占位方法。

## 启动与验证

在 [鉴权服务](server-auth.md) 的 PostgreSQL / Redis 配置基础上，直连 TLS 示例：

```powershell
.\.venv\Scripts\python.exe -m uvicorn evalforge.server.app:create_app --factory --host 127.0.0.1 --port 8000 --ws websockets-sansio --ws-max-size 1048576 --ws-ping-interval 20 --ws-ping-timeout 20 --ssl-certfile '<证书路径>' --ssl-keyfile '<私钥路径>' --no-proxy-headers
```

也可以由反向代理终止 TLS，但必须正确设置代理头并严格限定 Uvicorn 信任的代理 IP，确保应用收到 wss scheme；不要直接信任来自公网调用方的转发头。

测试使用临时自签名证书，客户端加载该证书作为信任根，未关闭 TLS 或主机名验证。真实 Uvicorn WSS 与现有 client 的独立子进程完成双向 RPC；数据库使用文件 SQLite，Redis 使用 fakeredis，不替代真实 PostgreSQL / Redis 联调。

2026-11-04：新增 19 项测试，最终云端全量 133 项中 131 项通过、2 项因没有真实 PostgreSQL / Redis 实例跳过。临时移除摘要比较后，错误秘密的握手测试按预期失败；已恢复。确定性阻塞测试验证取消会等待认证清理，关闭后不遗留处理任务。首轮全量曾因本机内存不足失败，改用较低开销的 shell 后完整复测通过。

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
.\.venv\Scripts\python.exe -X utf8 -W error -m unittest discover -s tests -v
```

本次安装的 Uvicorn 0.54.0 sansio 后端在已正确返回 HTTP 拒绝响应后，会额外记录“ASGI callable returned without completing handshake”。已核对实际 HTTP 状态和响应体；未修改第三方包。该日志不表示认证被放行。

参考：[Starlette WebSocket / denial 扩展](https://starlette.dev/websockets/)、[JSON-RPC 2.0](https://www.jsonrpc.org/specification)。
