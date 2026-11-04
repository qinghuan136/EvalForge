# Runner 管理

五个管理接口已实现：POST / GET `/api/v1/runners`，GET / PATCH / DELETE `/api/v1/runners/{runner_id}`。
浏览器登录与 CSRF 复用鉴权模块；请求和响应见 [接口文档](api/登录鉴权与Runner管理.md)。

## 代码链路

```text
routes/runners.py → 当前用户 / CSRF
                 → services/runners.py
                     ├─ PostgreSQL：Runner、唯一凭证、逻辑删除
                     └─ runner_connections.py：Redis 连接快照读取 / 注册清理
```

| 文件 | 内容 |
| --- | --- |
| evalforge/server/schemas/runners.py | 名称校验、创建 / 详情 / 分页响应白名单 |
| evalforge/server/routes/runners.py | 五个路由、分页参数、登录与 CSRF 依赖 |
| evalforge/server/services/runners.py | 归属校验、事务、随机凭证签发、改名与删除 |
| evalforge/server/runner_connections.py | 批量读取连接状态及清理注册信息 |
| evalforge/db/orm/auth.py | Runner.deleted_at、唯一凭证关系与约束 |

## 行为

- 创建时在同一事务插入 Runner 和凭证。凭证为 `efr.<token_id>.<secret>`，secret 使用 32 字节密码学随机数；数据库只保存 secret 的 32 字节 SHA-256 摘要。
- 完整 Token 仅在事务提交成功后返回一次，默认有效期十个日历年。查询 / 改名不返回凭证；管理响应均禁止缓存。不提供凭证更换和 Runner 恢复。
- name 去除首尾空白后为 1–100 字符；PATCH 只接受 name。用户归属来自登录会话，其他用户访问同一 ID 统一返回 404。
- 列表使用 page / pagesize，默认 1 / 20，pagesize 最大 100；按 created_at、runner_id 降序排列，排除已删除及其他用户的 Runner。分页不冻结跨请求快照。
- 写事务内重新校验用户启用状态和 auth_version，用户行锁与改密码 / 退出全部会话协调；数据库和 Redis 基础设施保持共享，每次数据库操作独立 Session。
- 删除事务同时写 deleted_at、enabled=false、revoked_at，不改变 Attempt 结果或删除历史。已删除记录详情 / 改名为 404，重复删除为 204。
- 数据库提交后才清理 Redis 注册；清理失败返回 503，撤销不回滚，重复 DELETE 会再次清理。改名后连接读取失败也返回 503，但名称已保存，可重复 PATCH 重试。

## 连接快照与当前边界

Redis key：`evalforge:runner:session:<runner_id>`。未来 WSS 写入方需要设置 TTL，最少包含：

```json
{"session_id":"current-session-id","phase":"ready"}
```

phase 支持 connected / syncing / ready；记录缺失显示 online=false、phase=disconnected。列表用一次 MGET 批量查询；Redis 失败或记录损坏返回 503。

WSS 认证与传输已接入，但业务会话注册方尚未实现，正常无记录时显示离线。删除后，WSS 授权复查会关闭现有连接；多连接接管、旧会话写入隔离和任务同步后续实现。断线不等同远端 Agent 已停止，不能只依赖 Redis 注册存在来授权。

创建响应中的连接地址由 `EVALFORGE_RUNNER_WS_URL` 指定，不信任请求 Host。默认 `wss://localhost:8000/api/v1/runner/ws`；接入能力见 [WSS / RPC](server-wss.md)，当前尚不能完成业务 hello / sync。
本机 HTTP 调试可同时设置 EVALFORGE_COOKIE_SECURE=false、允许的 loopback Origin，以及 `ws://127.0.0.1:8000/api/v1/runner/ws`。公开部署使用 WSS。

## 数据库与验证

ORM 现为 21 张表、183 个字段。新增 deleted_at 可空字段、deleted_at 非空时必须禁用的检查约束、活动 Runner 的归属 / 时间部分索引，以及 runner_credentials.runner_id 唯一索引。关系由 Runner.credentials 列表改为 Runner.credential 单个对象。

当前仍未生成正式初始迁移或执行真实 PostgreSQL 建表；已有数据库需先检查同一 Runner 的多份历史凭证，再清理数据、添加唯一索引和新字段。不可通过 create_all 升级旧表。启动与环境准备见 [鉴权服务](server-auth.md)。

测试使用 HTTPX ASGI、异步 SQLite 和 fakeredis，覆盖 Token 摘要、十年到期、分页、跨用户访问、CSRF、逻辑删除、Attempt 历史保留、提交失败及 Redis 失败后的重试。
2026-11-04：新增 14 项 Runner 测试，云端全量 114 项中 112 项通过、2 项因没有真实实例跳过。临时移除归属条件后，跨用户访问测试按预期失败；已恢复源码。依赖检查、编译和文档链接检查通过。
PostgreSQL 行锁与真实 Redis 部署仍需实例联调。
