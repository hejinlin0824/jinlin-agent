# 独立评审响应与整改记录（v1.2）

| 项 | 值 |
|---|---|
| 评审来源 | `JINLIN_AGENT_REVIEW.md`（2026-11-01，静态代码审查）+ `JINLIN_AGENT_REVIEW_FOLLOWUP.md`（整改复核） |
| 评审基线 | `f89384c`（首轮）、`85854f1`（复核轮） |
| 评审评分 | 实验型工程 Harness **7.0/10**；生产级通用 Agent **4.5/10** |
| 本响应版本 | v1.2：首轮 P0/P1 整改 + follow-up 三项残留修复 |
| 结论 | **接受两轮评审**。首轮 P0×2、P1×6 已修；follow-up 的 P1×2、P2×1 已修；其余明确延后 |

## 0. Follow-up 复核整改（v1.2，基于 `85854f1` 复核）

| 复核项 | 处理 | 回归测试 |
|---|---|---|
| **P1 多文件回滚误删/覆盖并发写入** | 重写事务写入：只登记**本事务成功产生**的变更，并保存写后哈希；回滚前核验当前哈希仍等于本事务写后状态，不一致则保留外部内容并返回 `RECOVERY_UNCERTAIN` + `rollback_incomplete`；Add 冲突绝不删除并发创建者文件；Delete 改为"原子重命名到备份 → 校验 → 事务成功才删除备份"，校验失败时恢复原文件 | `test_apply_patch_add_conflict_does_not_delete_external_file`、`test_apply_patch_update_conflict_does_not_restore_over_external_edit`、`test_apply_patch_rollback_only_reverts_own_mutations`、`test_apply_patch_delete_conflict_preserves_external_file` |
| **P1 L5 `exit_code=None` 可能放行** | 未知进程状态 fail-closed：`timed_out` / `exit_code is None` / 未知非零退出 / exit 0 但输出含失败 ID，均判 failed | `test_l5_none_exit_code_fails_closed` |
| **P2 事件尾部判定过宽** | 改为 bytes 读取并保留"是否以换行结束"：只有**无换行结尾**的最后片段允许作为 torn tail；换行终止的坏 JSON、中间空行、schema/session 不一致、seq 缺口一律 `SESSION_CORRUPTED` | `test_torn_tail_without_newline_is_tolerated`、`test_newline_terminated_corrupt_tail_is_rejected`、`test_blank_line_between_events_is_rejected`、`test_schema_and_session_mismatch_is_rejected` |
| **P2 Claim-Evidence 范围表述** | 明确限定：**测试类 Claim 已建立 L3 证据约束；通用 Claim-Evidence 语义匹配仍未实现**（后续用 `claim_type`/`evidence_type` 设计） | 文档修正 |
| 复核建议：CI 记录 | 新增 `.github/workflows/ci.yml`：Windows + Ubuntu、Python 3.13，跑 pytest（junitxml）+ 离线 eval，上传报告 artifact | CI |

说明：本轮按要求先推送到评审分支 `review/followup-fixes`，不直接改 `main`；由评审确认 diff 与 CI 结果后再合并。

## 1. 首轮已修复（含回归测试）

| 评审项 | 修复 | 位置 | 回归测试 |
|---|---|---|---|
| **P0-1** 父目录 symlink/Junction 越界（目标不存在时绕过） | 新增 `paths.resolve_in_workspace()`：词法边界 → realpath 边界（含 Junction）→ 禁用 follow 时逐级拒绝 link 组件；文件工具、补丁引擎、Shell 共用 | `src/jinlin_agent/paths.py`、`policy.py`、`tools/base.py` | `test_parent_junction_escape_rejected`、`test_dotdot_and_absolute_escape_rejected` |
| **P0-2** Shell `cwd` 未校验边界 | Policy 在命令分级前校验 cwd；Shell handler 经 `ToolContext.resolve` 二次防御 | `policy.py::_check_execute`、`tools/base.py` | `test_shell_cwd_outside_denied` |
| **P1-1** `write_file` 覆盖已存在文件时不检查读后写 | 已存在文件强制 `check_clean`（未读 → `FILE_NOT_READ`，被外部改 → `FILE_CHANGED_EXTERNALLY`）；新文件走 `create=True` | `tools/patch.py::write_file` | `test_write_file_existing_requires_read`、`test_write_file_external_change_rejected` |
| **P1-2** `atomic_write` 替换前不复核哈希 | 增加 `expected_sha256` 写前复核 + 写后校验；`create=True` 拒绝并发创建覆盖；多文件补丁的 add/update/delete 全部走同一守卫 | `tools/patch.py::atomic_write` | `test_atomic_write_hash_guard` |
| **P1-3** 证据"存在"不等于"能支撑 Claim" | 测试类 Claim 必须引用**通过的 L3 验证**（ver_*、artifact_*、"L3 tests"、测试命令），文件路径不再能背书"测试通过" | `verify.py::CompletionGate` | `test_gate_requires_passing_test_evidence_for_test_claims` |
| **P1-4** L5 只比较失败数量 | 解析失败/错误测试 **nodeid 集合**、退出码、timeout；比较集合差异；未知非零退出码不默认通过 | `verify.py::parse_test_output/_level5` | `test_l5_detects_changed_failure_identity`、`test_parse_test_output_extracts_failure_ids` |
| **P1-5** L0 安全 findings 不阻断完成 | L0 未运行或 failed → 完成门硬性拒绝 | `verify.py::CompletionGate` | `test_gate_rejects_failed_l0` |
| **P1-6** 基线异常被静默吞掉 | baseline 事件固定带 `status=passed/failed/skipped/error` + 原因；异常写事件并进入报告警告 | `session.py::_capture_baseline`、`verify.py::ensure_baseline` | 由 E2/E3/L5 测试覆盖 |
| **P2-1** 事件读取忽略任意位置损坏 | 只容忍**文件末尾 torn write**；中间损坏、`seq` 缺口、缺字段 → `SESSION_CORRUPTED` | `events.py::_iter_events_checked` | `test_mid_log_corruption_is_reported`、`test_seq_gap_is_reported`、`test_torn_tail_still_tolerated` |
| **P2-doc** README 与报告的真实模型验证矛盾 | README 删除过时"等待 Key"描述；真实模型验证以可核验记录为准；`<this-repo>` 占位符替换 | `README.md` | 文档评审 |

## 2. 部分修复 / 明确延后

| 评审项 | 状态 | 说明 |
|---|---|---|
| P1-2 多文件补丁崩溃原子性 | **部分** | 已有写前哈希复核、写后校验、异常回滚；**跨文件崩溃事务（transaction manifest）未实现**，进程被强杀仍可能部分替换 |
| P2-2 上下文最终 Token 预算闭环 | **部分** | 已有内容相关预算与压缩；最终 messages 的二次统一估算、required 超预算的显式 `BUDGET_EXCEEDED` 仍待做 |
| P2-3 脱敏覆盖嵌套结构/自由文本 | **部分** | 事件参数按顶层键脱敏；嵌套对象、自由文本、Artifact 统一 redaction 未实现 |
| P2-4 事件 hash chain | **延后** | 当前用"末尾容错 + 中间报错 + seq 连续性"保证完整性；hash chain 作为后续增强 |
| 独立真实任务评测（30–50 个） | **未做** | 需要外部任务集与统一预算，属于下一阶段工作 |
| 覆盖率/CI 记录 | **未做** | 当前只有 pytest 结果，无覆盖率与 CI 趋势 |

## 3. 安全边界现状（修复后）

```
所有文件入口 ──► paths.resolve_in_workspace()
                    │ 1. 词法边界（../、绝对路径）
                    │ 2. realpath 边界（symlink + Junction，含不存在的叶子）
                    │ 3. follow_symlinks=false 时逐级拒绝 link 组件
                    ▼
               受保护路径 / .git 写入拒绝
                    ▼
               编辑：读后写哈希 + 写前复核 + 写后校验 + create-only
                    ▼
Shell：cwd 同一边界校验 → 命令分级 → 审批(fail-closed) → 沙箱
```

## 4. 验证方式

```bash
python -m pytest -q                    # 63 passed（42 基础 + 21 安全/完成门/事务回归）
jinlin-agent eval --suite all          # 18/18 probes passed
```

评审建议的回归用例（SEC-001..008、EDIT-001..004、VERIFY-001..007、EVENT-001..004）
已覆盖大部分；**仍未覆盖**：Junction 在 L1 沙箱内的行为、多文件崩溃注入（EDIT-004，
需要 transaction manifest + 启动恢复）、嵌套/自由文本脱敏（SECRET-001/002）。

## 5. 对评审评分的说明

- 同意"实验型工程 Harness 7.0 / 生产级 4.5"的判断：产品能力面、跨平台隔离、独立真实任务评测仍是短板。
- 本次整改的目标不是提高自评分，而是把"设计上强调可靠"落成"实现上强制可靠"：路径、cwd、编辑一致性、L0/L5/证据、事件完整性、并发回滚现在都有机器判定与回归测试。
- 通用 Claim-Evidence 语义匹配、多文件崩溃事务、最终 Token 预算闭环、递归脱敏仍待办，报告里不宣称已完成。
- 下一阶段优先级（与评审一致）：**独立真实任务评测 > 剩余 P2 > 扩展能力（MCP/Skills/UI/多 Agent）**。
