# 客户端后续事项

- 按用户约定，暂不实现进程独占锁；当前同一数据库只能由一个 Runner 进程使用。后续有需要再加入 OS 文件锁或等价机制。
- Runner 与 Cloud 的状态库身份绑定检查；当前通过配置约定隔离不同 Runner 和环境。
- 实现独立于连接生命周期的 JobManager，接入 SessionJobs：容量管理、持久化接收、继续、取消及正常结果上报。
- 实现本地 Agent HTTP 调用，派发参数统一为 agent_target_id。
- 明确 unknown 的后续人工/查询处置，继续禁止自动重执行；discard 暂不实现。
- 在真实 WSS Cloud 环境验证认证、会话接管和恢复；现有测试使用本机模拟服务。
