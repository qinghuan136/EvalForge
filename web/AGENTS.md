# EvalForge Web 开发指南

本目录是浏览器端 React + TypeScript + Vite 应用。FastAPI 后端在 `../evalforge/`，本地 Runner 在 `../client/`。

- `src/app/` 放入口、路由和工作台外壳；`src/features/` 按业务组织页面、弹窗、`api.ts` 和类型。`src/components/ui/` 只放通用控件，`src/lib/` 放公共请求等工具。
- 路由用 React Router，后端查询和缓存用 TanStack Query，做到对应页面时再引入。筛选和分页放 URL；弹窗、表单状态留在组件内。
- 沿用[页面原型](../docs/web/evalforge-prototype.html)的「雾灰青」主题，公共颜色从 `src/styles/theme.css` 取。新建实验、数据集详情、Runner 详情用居中弹窗，保留背后的列表；实验详情用独立路由。
- 接口以[后端接口文档目录](../docs/api/)和实际路由为准。浏览器走 `/api/v1/` REST，不接 Runner WSS。集中处理 Cookie、`X-CSRF-Token` 和 `error.code`；登录后更新 CSRF Token，不把凭据存入浏览器存储。
- 先跑通登录和 Runner 列表，再接数据集与实验。缺接口时明确标记；演示数据与真实请求分开。

在 `web/` 下用 npm：`npm ci` 安装，`npm run dev` 启动，`npm run build` 检查类型并构建。Vite 开发代理默认转发到 `http://127.0.0.1:8000`，可按 `.env.example` 覆盖；联调时在后端允许前端 Origin。改动交互时检查实际页面，包括相关的加载、空和错误状态。
