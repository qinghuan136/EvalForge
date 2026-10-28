# Phoenix Client 来源记录

上游：Arize AI Phoenix，https://github.com/Arize-ai/phoenix

固定参考 commit：`14c193cfad309198168e3c2db522db0ec770a557`。

EvalForge V0 的函数参数绑定、评分返回值规范化、两阶段运行与分离结果记录，
参考并精简改写自该快照的 `packages/phoenix-client/src/phoenix/client/resources/experiments/`。
未将完整上游源文件引入本项目，未把 Phoenix 加为运行依赖。

- `LICENSE`：上游 `packages/phoenix-client/LICENSE` 原文。
- `IP_NOTICE`：上游 `packages/phoenix-client/IP_NOTICE` 原文。
- `LICENSE.upstream-root`：上游仓库根 `LICENSE` 原文。

这些文件保留上游各自的原始声明，不对声明间的差异进行修改。
实现差异与调用链说明见 `docs/phoenix-experiment-chain.md`。
