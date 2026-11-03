# WP-V2.0-7/8 实施报告 — 上下文工程 与 自适应验证（Milestone E 合并交付）

> 分支 `feature/v2-wp1-task-ledger`。实现提交 `9d0988d`；基线 `86b16c8`（WP-6 后）。两个 WP 同属 Milestone E，一次提交交付，报告合并。

## 1. 基线
- Branch：`feature/v2-wp1-task-ledger`；Base：`86b16c8`；Head：`9d0988d`

## 2. 目标
WP-7：统一、按需、有限预算、可追溯的上下文（v2 §14-WP7）——本轮按差距分析收窄为 ContextProvider 接口 + **依赖任务产物注入**（打分/预算/压缩/审计已在 v1 内核）。WP-8：验证与完成基于证据（v2 §14-WP8）——任务级验证计划、必需等级、验收门、证据字段。

## 3. 实际改动
**WP-7**：`context_provider.py`（ContextProvider 钩子；provider 失败记为丢弃原因，绝不吞掉）；`DependencyContextProvider` 把已完成上游的结构化交付物注入 worker 窗口（打分参与预算，超预算只会有理由地丢弃）；`Harness(context_providers=)` 参数化；WorkerRuntime 按 TaskSpec.dependencies 装配。

**WP-8**：`VerificationRun.sandbox_level` 证据字段（每次 run 打点，v2 §10.3）；`PROFILE_LEVELS`（code/config/docs/tests/analysis → 必需等级）+ **验收门**：会话成功 ≠ 任务完成——必需等级未过则任务 `failed`（error 记录原因）；L3 适用性跟随项目真实测试命令（`code` 档无测试命令 → L3 如实"不适用"；`tests` 档 → 明确失败"无法验证"——v2 §10.4 未验证绝不冒充成功）；纯分析交付（无文件改动）不虚构 L0/L1；未 profile 的规划任务按 `code`；**TaskResult 保留每级最新 run**（截断窗口会悄悄丢 L0——worktree e2e 抓出并修复）；planner 映射 verification 字段。

## 4. 测试
`tests/test_context_provider.py` 4 个 + `tests/test_task_verification.py` 8 个（TDD 先红）：provider 块生成/跳过/预算压力有理由丢弃、worker 上下文可见上游交付、profile 映射、门通过/缺失/失败/未知 profile、L3 适用性两向、sandbox_level 证据、Supervisor 端到端（tests 档在无测试项目被门拒绝 → 有界重规划 → analysis 替代完成）。全量 **189 passed**，eval 18/18。

## 5. 安全与兼容性
验收门运行在 worker 线程（确定性代码，非模型判断）；Supervisor 不再重复把关（知道项目测试命令的只有 worker 上下文——设计权衡记录于代码注释）。L0/L1 不可跳过语义不变。

## 6. 未完成事项
压缩仍为确定性摘要（不用 LLM 摘要）；上下文质量评估样例（v2 §14-WP7 交付物"评估样例"）由 22 个探针中的 P3/P3b + 依赖注入测试共同承担，专项评估未建。
