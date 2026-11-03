# ADR-005：Worker 隔离分两层——Git Worktree 管版本，OS Sandbox 管安全

- 状态：Accepted（2026-11-03 负责人确认）
- 日期：2026-11-03
- 关联：v2.0 §9.1–9.3、§18-5；AGENTS.md 硬规则 7

## 背景

仓库已有真实的 OS 层隔离：Windows L1 沙箱（受限令牌 + workspace 写 ACE + Job Object 杀整树，sandbox/windows.py），含诚实降级链 L1 → L1-job → L0 与真实写探针测试（tests/test_sandbox_win.py，405 行）。仓库**没有**任何 worktree/分支管理。v2 §9.1 正确指出 worktree 不是安全边界——它不阻止读外部文件、访问网络或执行任意命令。

## 决策

1. 每个 Worker 的文件变更发生在独立 worktree + 任务分支（`agent/task-<id>`），记录 base_commit；**禁止** Worker 直接写共享主工作区。worktree 的创建/清理/基线过期检测/集成属于 Workspace Manager + Integration Coordinator（WP-5 新建）。
2. OS 层沿用现有 Sandbox 契约（`Sandbox.run/popen/probe_write`，sandbox/base.py:61-90），将 `workspace` 参数指向 Worker 的 worktree 路径即可生效（受限令牌的写 ACE 按 worktree 授予）；Windows 不可用时降级链保持现状，等级如实入 `sandbox.mode` 事件与报告。
3. 明确不宣称的能力（沿 README §5 的诚实版安全模型）：网络无防火墙级隔离；进程内文件工具靠路径策略而非 OS 权限；`C:` 整盘类强隔离需要后续评估 Windows Sandbox/WSL2（v2 §9.5），不在 WP-5 范围内。
4. 基线过期的 Worker（B 完成时集成分支已前进）：保留其 commit，rebase/merge 失败 → `integration.conflict` 暂停自动集成（v2 §9.3 的 9 步流程），**绝不**整目录复制覆盖。

## 备选方案

- 共享工作区 + 文件级写范围（无 worktree）：否决——多 Worker 写同一工作区会让完成门的"未归属改动"检查（verify.py:584-597）与回滚事务失去归属语义。
- 用轻量 clone 替代 worktree：备选保留——若 worktree 在 Windows 上的锁/索引竞争成为问题（`.git` 共享），clone 到隔离目录是兜底，接口不变。

## 后果

- WP-5 验收直接引用 v2 §14-WP5：基线过期能检测、冲突不静默、合并后重跑受影响验证、根任务完成前不动稳定分支。
- 现有编辑引擎（patch.py 的锁/哈希/回滚）在 worktree 内无需改动即生效，因为 worktree 路径就是该 Worker 的 `workspace` 根（paths.resolve_in_workspace 的边界随之自然收窄到 worktree）。
