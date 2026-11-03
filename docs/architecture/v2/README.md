# 架构 v2.0（统一 Runtime + 动态角色编排）——WP-V2.0-0 交付物

> 状态：**盘点完成；ADR-001~010 已于 2026-11-03 由负责人全部确认（Accepted）；零第三方依赖约束继续有效；WP-V2.0-1 进行中**。依据：《Jinlin Agent 架构设计 2.0》（本目录 [ARCHITECTURE-v2.0.md](ARCHITECTURE-v2.0.md)）。
> 阅读顺序：先读 [wp-v2.0-0-report.md](wp-v2.0-0-report.md)（3 分钟），再按需深入。

| 文件 | 内容 |
|---|---|
| [ARCHITECTURE-v2.0.md](ARCHITECTURE-v2.0.md) | 设计文档 2.0 原文（入库副本，来源：所有者提供的 `jinlin-agent-architecture-v2.md`） |
| [current-state.md](current-state.md) | **交付物 1**：真实代码现状盘点（模块地图、调用链、33 种事件、20 工具、安全/验证/完成门机制，全部带 `文件:行号`） |
| [gap-analysis.md](gap-analysis.md) | **交付物 2**：差距分析——v2 资产表勘误、组件级差距矩阵（✅/🟡/❌）、v2 §2.3 七项边界核实、新发现、技术栈适配、顺序建议 |
| [decisions/](decisions/) | **交付物 3**：ADR-001 ~ ADR-010（全部 Proposed，待逐条确认） |
| [test-baseline.md](test-baseline.md) | **交付物 4**：测试基线（105 passed / 18/18 / CI 双平台绿 + 观察项） |
| [wp-v2.0-0-report.md](wp-v2.0-0-report.md) | WP 实施报告（v2 §20.3 模板：基线/目标/改动/测试/未完成/本地验收） |

## 一句话摘要

仓库的真实基线是**单 Agent、事件溯源、CLI 形态**的代码工程 harness：执行内核（事件日志/策略/沙箱/验证/完成门/上下文）完整且经 10 轮评审加固；v2.0 设计文档 §2.2 所列的 ApplicationService / Workbench / TaskContract / Web API **不存在**。v2 蓝图依然适用，落地路径 = 在现有内核之上新建**任务层**（TaskSpec / TaskLedger / Runtime / Planner / 协议 / Worktree 集成 / Web），按 WP-1 → WP-2 → WP-3/4 → WP-5/6 主轴增量推进，`main` 受保护，每步全量回归。

## 决策记录（2026-11-03，负责人确认）

1. ✅ ADR-001 ~ ADR-010 全部确认（Accepted）；
2. ✅ gap-analysis.md §0 的资产表勘误确认（以真实代码为准）；
3. ✅ 零第三方依赖约束在 v2 阶段继续有效；
4. ▶ **WP-V2.0-1：统一 Task Model 与 Task Ledger** 已启动（独立分支、TDD、全量回归）。
