# ADR-010：向后兼容、分阶段增量迁移；不推倒现有单 Agent 路径

- 状态：Accepted（2026-11-03 负责人确认）
- 日期：2026-11-03
- 关联：v2.0 §20.2/§20.4、§18-10；AGENT.md §6.4/§8；AGENTS.md

## 背景

该仓库有严格的评审文化：10 轮整改、`main` 受保护、所有修复走 `review/**` 分支、TDD（先失败测试后实现）、每步全量回归（105 测试 + 18 探针 + CI 双平台）。v2 §0 原则"先盘点、再设计、分阶段迁移；不直接推倒重写"与仓库既有流程天然一致。

## 决策

1. **全部增量**：v2 的新能力以新模块落地（WP-1 起：`task.py`/`ledger.py`/`orchestrator.py` 等新文件），不重写 `loop.py`/`runtime.py`/`events.py`/`verify.py` 的既有语义；对现有模块的改动限于参数化（如 Harness 构造、PolicyEngine 增维度）。
2. **兼容窗口**：`jinlin-agent run/resume/...` 的 CLI 行为、退出码、事件 schema（schema_version 内演进，新事件只增不改义）、`~/.jinlin-agent/sessions` 目录格式在迁移期保持不变；现有 105 测试 + 18 探针**一个不许删、一个不许弱化**（AGENT.md §7-11），每个 WP 合并前全量回归 + CI 双平台绿。
3. **分支与交付纪律**（沿 AGENT.md §6.4/§8 + v2 §20.3）：`main` 只读；每个 WP 从 `review/followup-fixes`（或其后的基线）切独立分支；WP 报告按 v2 §20.3 模板（基线/目标/改动/测试/安全兼容/未完成/本地验收）；不自行合 main。
4. **迁移顺序**按 ADR-002/003 的结论：WP-1（Task Model + Ledger）先行，其次 WP-2（Runtime），编排（WP-3/4）之后；每阶段以 v2 §14 的验收条款 + Definition of Done（v2 §20.4）关闭。
5. **安全先行**：涉及 Worker 写能力的工作包，其 ADR-006 对应项先合入。

## 备选方案

- 大爆炸式重构成"Agent 平台"：否决——v2 §21 明确反对；且会摧毁 10 轮评审建立的可信基线。
- 先做 Web UI 再补内核：否决——UI 无 Task Ledger/Runtime API 可挂（gap-analysis §5）。

## 后果

- 迁移期仓库同时存在"单 Agent 直连"与"任务编排"两条路径，由 CLI 子命令区分（如未来 `jinlin-agent task submit ...`），互不污染。
- 每个 WP 完成时更新 AGENT.md 状态（仓库惯例），保证下一位接手者不需要重新盘点。
