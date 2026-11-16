# EvalForge 云端部署

在一台装有 Docker Engine 和 Docker Compose V2 的主机上运行。Nginx 提供 HTTPS、托管浏览器构建产物，并转发 REST 和 Runner WSS。`cloud-a` 和 `cloud-b` 各运行一个 Uvicorn worker，共享 PostgreSQL 和 Redis。使用业务接口前需单独准备数据库表。

## 启动

1. 在项目根目录把 `deploy/compose.env.example` 复制为 `.env.compose`。分别生成 `POSTGRES_PASSWORD` 和 `EVALFORGE_INTERNAL_ROUTE_TOKEN`，例如各运行一次 `openssl rand -hex 32`。密码直接用于 PostgreSQL 连接 URL，令牌会渲染进 Nginx 配置，填写生成的十六进制字符串即可。
2. 将域名的证书链 `fullchain.pem` 和私钥 `privkey.pem` 放入 `EVALFORGE_TLS_DIR` 指定的目录。示例使用 `deploy/tls/`。将 `EVALFORGE_ALLOWED_ORIGINS` 和 `EVALFORGE_RUNNER_WS_URL` 改为浏览器与 Runner 实际访问的 HTTPS 和 WSS 地址。如果使用非默认端口，两个地址都要带端口。本机联调可把 `EVALFORGE_HTTPS_BIND` 设为 `127.0.0.1`，并让浏览器和 Runner 信任测试证书。
3. 在项目根目录构建并启动。

   ```sh
   docker compose --env-file .env.compose build cloud-a nginx
   docker compose --env-file .env.compose up -d --no-build --wait
   docker compose --env-file .env.compose exec nginx nginx -t
   docker compose --env-file .env.compose ps
   ```

浏览器访问配置的 HTTPS 地址。Runner 继续在本地启动，使用管理页面发放的凭证连接 `EVALFORGE_RUNNER_WS_URL`。公网入口只有 Nginx 的 HTTPS 端口。PostgreSQL 的数据保存在 Compose 命名卷里，常规 `docker compose --env-file .env.compose down` 保留该卷。

## 验证请求路由

1. 注册账号，导入一个数据集，配置本地 Runner 和 Agent。Runner 连接后，等管理页面显示「已就绪」。通过浏览器创建实验，确认 Job 被派发并返回结果。
2. 用 `docker compose --env-file .env.compose logs nginx` 查看代理日志。创建请求有一条 `uri=/internal/runner-route` 记录，显示哪台后端执行归属查询。接着主请求的 `route=cloud-a` 或 `route=cloud-b` 显示选中的会话进程。两条记录的 `upstream` 可以不同。取消实验、查询列表或详情时只有业务请求记录。
3. 断开 Runner，等页面显示离线，再发起定向创建请求。Nginx 返回 `403 runner_not_routable`，已存在的实验仍可通过取消接口处理。

Nginx 通过 Docker DNS 重新解析服务地址，`cloud-a` 和 `cloud-b` 仍须分别对应一个 worker。定向创建和 Job SSE 只接受配置中列出的归属标识，归属查询或目标连接不可用时返回 `503`。Job SSE 已关闭代理响应缓冲。
