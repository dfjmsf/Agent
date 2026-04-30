# ASTrea System Architecture Map

> **版本**: v1.7.0 Unlimited Architecture
> **最后更新**: 2026-04-29
> **维护者**: 架构审计自动化

本目录是 ASTrea 系统的**持久化架构索引**，作为所有开发/迁移/审计工作的唯一拓扑参考源。

## 文档索引

| 文件 | 内容 | 状态 |
|------|------|------|
| [topology.md](./topology.md) | 全局模块拓扑 + 数据流 + 依赖图 | ✅ 已建立 |
| [agent_registry.md](./agent_registry.md) | 10 个 Agent 的职责/接口/Token 预算 | ✅ 已建立 |
| [known_defects.md](./known_defects.md) | 已知缺陷台账 + 修复优先级 | ✅ 已建立 |

## 架构契约

1. **Blackboard 是唯一真理源** — Agent 读写 Blackboard，Engine 扫描 Blackboard 驱动状态机
2. **Engine 是唯一 VFS 写入者** — Agent 禁止直接操作磁盘文件
3. **Facade 隔离** — 外部入口 (`server.py`) 只通过 `AstreaEngine` Facade 交互
4. **TDD 闭环** — 每个 Task 必须经过 Coder→Patcher→Reviewer 完整验证链
