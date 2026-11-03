# WP-V2.0-0 实施报告

> 按 v2.0 设计文档 §20.3 模板。对应交付物：[current-state.md](current-state.md)、[gap-analysis.md](gap-analysis.md)、[decisions/ADR-001~010](decisions/)、[test-baseline.md](test-baseline.md)、[ARCHITECTURE-v2.0.md](ARCHITECTURE-v2.0.md)（设计文档入库副本）。

## 1. 基线

- Branch：`review/followup-fixes`（本 WP 交付于新分支 `feature/v2-wp0-inventory`，自 `review/followup-fixes` 切出）
- Base commit：`b0f3ade`（文档 HEAD；代码 HEAD `17b2262` = 第 10 轮整改 fix(hardening)）
- Head commit：见 `git log --oneline -1`

## 2. 目标

以真实代码为准完成 v2.0 迁移前的现状盘点与架构差距分析（v2 §14-WP0），产出可评审的差距矩阵与 ADR，**不改任何行为代码**。

## 3. 实际改动

- 仅新增文档：`docs/architecture/v2/`（9 个文件，见上）；无源码、无测试、无配置改动。
- 关键结论（详见 gap-analysis.md）：
  1. v2 §2.2 资产表与仓库不符——ApplicationService / Workbench / TaskContract / Web API / `feature/workbench-*` 分支**均不存在**；真实基线是单 Agent CLI harness（内核强、任务层零）。
  2. v2 蓝图的六块核心组件（事件溯源 / 策略引擎 / 沙箱 / 验证阶梯 / 完成门 / 上下文预算）已有可复用实现；缺的是 TaskSpec·TaskLedger（WP-1）、Runtime/Worker（WP-2）、Planner/DAG（WP-3）、协议（WP-4）、Worktree 集成（WP-5）、任务级权限（WP-6）与 Web（WP-9）。
  3. 技术栈适配：不引 Pydantic/FastAPI（硬规则 1），Ledger 用 stdlib sqlite3，并发保持同步 + 线程池。
- API / Schema 变化：无。

## 4. 测试

- 命令与环境：`python -m pytest -q --junitxml`（Windows 11 / Python 3.13）→ **105 passed / 0 failed / 0 skipped**；`python -m jinlin_agent eval --suite all` → **18/18**。详见 [test-baseline.md](test-baseline.md)。
- CI 链接：本 WP 未推送分支，无新 CI run；最近基线 run 为 41306414718（双平台绿）。
- 本 WP 为纯文档，无行为回归面。

## 5. 安全与兼容性

- 权限变化：无。数据迁移：无。向后兼容：无影响。
- 风险：设计文档 §2.2 与本盘点的出入需要负责人裁决（以盘点为准）；ADR-001~010 均为 Proposed，未经确认前不得据其改代码。

## 6. 未完成事项（明确列出）

- ADR-001~010 待负责人逐条确认（Proposed）；确认前不进入 WP-1。
- v2 §14-WP0 交付物中的"调用链图 / 差距矩阵"已含于 current-state.md §3 与 gap-analysis.md §1；**ADR 数量按 v2 §18 全套 10 篇交付**，但内容深度是提案级，WP-1 设计文档需将其细化为可实施的 schema。
- AGENT.md 尚未加入指向 `docs/architecture/v2/` 的索引（待 ADR 确认后随 WP-1 更新，避免本 WP 混入状态文档改动）。
- 仓库根目录有 3 个往轮遗留的未跟踪脚本（`.fa9_patch.py`、`.fa9_rollback.py`、`.r10_rollback.py`），非本 WP 产物，建议负责人决定去留。
- 已知外观问题：Windows 下 pytest 一次 reader-thread UnicodeDecodeError 警告（test-baseline.md 观察项 1）。

## 7. 本地验收（Windows 操作步骤）

```bash
cd "E:\codebase\玩耍项目\jinlin-agent"
git log --oneline -1                 # 确认在 feature/v2-wp0-inventory
python -m pytest -q                  # 105 passed
python -m jinlin_agent eval --suite all   # 18/18
# 阅读顺序：README.md → current-state.md → gap-analysis.md → decisions/ADR-001~010
```

预期：测试全绿；文档可逐条对照源码行号复核；无任何源码 diff（`git diff --stat review/followup-fixes` 只显示 `docs/architecture/` 新增）。
