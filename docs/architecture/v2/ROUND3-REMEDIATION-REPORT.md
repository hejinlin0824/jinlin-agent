# 第三轮整改报告（ROUND3 REMEDIATION REPORT）

> 对应《第三轮整改任务书（WP-C1~C4）与外部基准评测方案》。结论先行：**WP-C1~C4 全部完成——
> 验证指标假阳性已修复并以评审给出的必答题样例回归锁定；真实 usage 与本地估算完全拆分且六条
> 历史事件流已由可重复脚本重算（评审点名的 analyze × planner 假阳性更正为 not_run）；
> Ledger/benchmark 回归补齐（含此前缺失的非空交付物集成正向路径与串行执行证明）；干净
> checkout 全量复验、密钥扫描、双平台 CI 通过。WP-B1 外部基准评测按任务书顺序暂未启动。**

## 1. 基线、分支与 Commit 列表

| 项 | 值 |
|---|---|
| 评审 HEAD（任务书下发） | `581add3` |
| 整改分支 | `feature/v2-benchmark-integrity-fixes`（自 `581add3` 派生，未重置/覆盖原分支历史） |
| main | `bcb0e39`（**未修改、未合并**，见 §9 声明） |
| 本轮提交 | `667a11e`（WP-C1+WP-C2 代码与测试）→ `77af42c`（WP-C3 测试与文档更正）→ 本提交（WP-C4 报告、日志、密钥扫描与证据刷新） |

> 说明：C1 与 C2 共用一个代码提交——两者重写的是同一个 `_session_metrics` 接缝且测试互相
> 依赖（C1 的记录字段测试断言 C2 的 usage 语义），拆开提交无法保持每个提交独立可绿。

## 2. WP-C1：验证统计假阳性修复

**缺陷确认**：评审正确。`_session_metrics()` 以 `any()` 判定失败，空 `verification_runs`
得 `False`→`passed=True`；`run_supervised()` 以 `outcome["status"]=="completed"` 初始化验证
结论。六条真实事件流重算证实：`analyze × planner` 会话无任何 `verification.completed`
记录，旧代码仍报 True。

**实现**（复用既有验证模型，未造第二套）：
- `src/jinlin_agent/verify.py`：抽出 `required_verification_levels()`（等级适用性收窄的唯一
  来源，`check_task_verification` 同源复用），新增 `verification_evidence(runs, profile,
  has_test_command, changed)`——**只**消费 VerificationRun 记录；空记录一律 `not_run`
  （不被 no-changes 收窄洗成 not_applicable）；profile 显式声明/收窄产生的空必需集为
  `not_applicable` 且**必须带原因、永不等于通过**；必需等级失败→`failed`；缺失/跳过→
  `not_run`；同级多次运行取最后一次（重试语义）。
- `scripts/benchmark_matrix.py`：`_session_metrics` 接入 evidence；`_aggregate_verification`
  分层聚合（一个 worker 的通过不能升级失败/缺失的兄弟组件）；`_integration_evidence` 只认
  `integration.verified` 记录（required=任一子任务 `integration=pending`）；缺失会话日志的
  worker 贡献 `not_run`。outcome status 不再进入验证路径。

**回归测试**（任务书 §1.3 清单逐项，全部在库）：
`test_empty_verification_runs_never_pass`（评审必答题样例：`verification_runs=[] →
verification_passed is False, verification_status == "not_run"`）、
`test_missing_required_verification_level_fails_closed`、
`test_failed_required_verification_fails`、
`test_all_required_levels_pass_independently_of_task_status`（函数签名不含 status 参数）、
`test_skipped_optional_level_does_not_fail`、
`test_completed_status_does_not_imply_verification_passed`、
`test_single_strategy_aggregates_session_verification_events`（写文件会话真实产生阶梯记录）、
`test_supervised_strategy_does_not_infer_verification_from_outcome_status`、
`test_root_verification_requires_all_required_worker_and_integration_evidence`，
另有 `test_level_retry_last_run_decides`、`test_profile_declared_not_applicable_is_not_a_pass`、
`test_narrowing_with_existing_records_is_not_applicable_not_pass`。
位置：`tests/test_task_verification.py`、`tests/test_benchmark_matrix.py`。

**验收**：六条真实事件流重算后不再有无证据的 True（见 §3 重算表）；既有 L0/L1/L3/L5 门与
Completion Gate 无回归（全量 396 passed + eval 22/22，§5）。

## 3. WP-C2：真实 usage 与估算 Token 拆分

**缺陷确认**：评审正确。`SessionState.tokens_in` 同时累加 `context_built.tokens_estimate` 与
`model.attempt.usage`，benchmark 将其当作真实用量。自洽性验证：旧混合值 88,173（analyze×multi）
= 重算实际 71,082 + 估算 17,091，严格吻合——证明旧数字只是混合、事件流本身无恙。

**实现**：
- `src/jinlin_agent/state.py`：新增 `tokens_in_actual/out_actual`（仅 provider usage）、
  `tokens_estimated_context`（仅本地估算）、`tokens_usage_missing_calls`（无 usage 的
  attempt 数）、`model_attempts`。旧混合计数器保留并注明仅供预算门（`loop.py`）使用，
  报告一律用新字段。计数口径：`model_calls` = model.requested（逻辑调用）；
  `model_attempts` = model.attempt（**每次重试各记一条**，见 `test_retry_usage_is_counted_once_per_recorded_attempt`）。
- `src/jinlin_agent/model/mock.py`：MockProvider **不再伪造 usage**（原默认 100/20 已删，
  仅脚本显式给出 token 数才填）。此修改暴露并修复了两个真实健壮性缺陷：`loop.py` 与
  `ModelResponse.to_dict()` 在 `usage=None` 时崩溃（即任何不返回 usage 的真实 provider
  都会崩）——已改为 None 透传，attempt 记为 usage-missing。
- `scripts/benchmark_matrix.py`：记录字段改为 `tokens_in_actual/out_actual/total_actual`
  （无 usage 时为 **null** + `usage_complete=false` + 缺失调用数，绝不以 0 冒充）、
  `tokens_estimated_context`、`tokens_usage_missing_calls`；`conflicts` 改为 `null`
  （not measured——任务书 §10.3：不可测量必须报 null 而非硬编码 0）。

**重算脚本与结果**：`scripts/recompute_benchmark.py`（`--reuse-manifest` 保证可重复执行；
manifest 冻结于 `benchmark/recomputed/manifest.json`；原始事件流零修改）。重算由
`root_record_fields()`/`_session_metrics()` 完成——与实时运行同一条代码路径。
机器生成的新旧对照表：`benchmark/recomputed/CHANGES.md`。重算后逐 run、records、summary、
本报告与 FINAL-ACCEPTANCE §4b 五处数字同源一致。

**回归测试**（§2.4 清单逐项，`tests/test_token_accounting.py`）：
`test_actual_usage_excludes_context_estimates`、
`test_estimated_context_tokens_are_reported_separately`（合成样例：估算 100 + usage 80/20 →
actual 80/20/100，**不是 180**）、`test_missing_provider_usage_is_not_reported_as_zero_actual`、
`test_mock_provider_never_claims_actual_usage`、`test_retry_usage_is_counted_once_per_recorded_attempt`、
`test_benchmark_summary_sums_actual_usage_from_runs`、`test_event_recomputed_usage_matches_run_record`，
另有 `test_partial_usage_reports_missing_calls`、`test_legacy_mixed_counters_still_feed_the_budget_gate`。

## 4. WP-C3：Ledger 与 Benchmark 回归补齐

**Ledger**（`tests/test_ledger.py`，5 个新测试）：`test_record_result_sets_pending_for_changed_files`、
`test_record_result_sets_not_needed_for_empty_deliverable`（实时写入路径）、
`test_rebuild_restores_pending_integration_state` / `test_rebuild_restores_not_needed_integration_state`
（**删除 SQLite 投影后** `rebuild()` 从事件流恢复相同状态）、
`test_integration_state_is_event_sourced_not_projection_only`（断言 `task.result_recorded`
事件携带 result payload、live 行与 replay 行逐字段一致）。schema v3 未动，"先事件后提交"未动。

**Benchmark**（6 → 12 个，`tests/test_benchmark_matrix.py`）：
- **非空交付物正向集成**（此前缺失，评审 §3.2 点名）：`test_multi_strategy_integrates_non_empty_deliverable_via_worktree`
  ——真实临时 git 仓库 + 写文件 worker：worktree 创建 → 差异提交 → IntegrationCoordinator
  合并 → `integration.verified passed=true` → 提交绑定进账本 → 集成树含交付物 → 根验证
  `passed`（带显式 L0/L1 证据）。
- `test_sequential_mode_runs_subtasks_serially`：从事件流证明串行（B 的 worker.registered
  事件 seq 在 A 完成之后），而非只查配置字段。
- 空 delivery 路径由既有 `test_multi_strategy_integrates_via_worktree_on_a_git_repo` 继续
  覆盖（not_needed / 不空合并 / 不无意义重验证）。
- 真实临时 git 仓库、真实 IntegrationCoordinator；Mock 的只有 Provider。

**文档更正**：初版报告将收尾轮新增的 benchmark 测试写作 7 个、实际 6 个（评审 §3.2 指认）
——已在 FINAL-ACCEPTANCE §2 与 REMEDIATION-REPORT §7 更正（并注明时点与本轮数量变化）。

## 5. WP-C4：全量复验、密钥扫描与证据重建

**干净 checkout 复验**（`git clone` 全新检出 `77af42c`，0 本地改动，Python 3.13.15，
Windows 10.0.26200；完整日志与 JUnit 在 `docs/architecture/v2/round3/logs/`）：

| 步骤 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| pytest | `python -m pytest -q -p no:cacheprovider --junitxml=junit.xml` | 0 | **396 passed / 0 failed / 0 errors / 3 skipped**（junit.xml 同入库） |
| eval | `python -m jinlin_agent eval --suite all` | 0 | **22/22 probes passed** |
| dry-run | `python scripts/benchmark_matrix.py --tasks docs/architecture/v2/benchmark/dryrun/tasks.json --out <fresh> --strategies single,planner,multi --dry-run` | 0 | 3 策略全部 completed；**新语义如实呈现**：verification_status=not_applicable（analysis 档案显式无必需等级）、tokens_actual=null + usage_complete=false（mock 无真实 usage） |

dry-run 证据（`benchmark/dryrun/`）已用该次干净复验的输出刷新，与 C1/C2 语义一致。

**CI**：本轮全部提交触发 GitHub Actions；最终整改 HEAD `c31b61f` 的双平台 run
**`41995235743`**：`ubuntu-latest / py3.13` ✅、`windows-latest / py3.13` ✅（Job 名、HEAD
SHA 如实记录；JUnit 的 tests/failures/errors/skipped 四数直接引用，未做混算）。

**密钥扫描**（gitleaks 未安装——如实记录；以等效规则手工扫描，工具与命令如下）：
- `git grep -n -I -E 'ark-[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{16,}' HEAD -- docs src scripts tests fixtures`
  → 1 处命中：`feature/v2-benchmark-integrity-fixes` **分支名**中的 "ark-" 字符串巧合
  （`benchmark-integrity`），非凭证，判定误报；
- 本轮新增行（`git diff 581add3..HEAD`）同规则 → 同一误报，无新增真实命中；
- `sk-` 命中仅 `docs/REPORT-AND-HANDBOOK.md` 的占位符 `sk-...`（文档示例，非真实 key）；
- `DEEPSEEK_API_KEY` 仅以变量名/占位符出现（允许）；真实 key 只存在于本轮 shell 环境变量，
  **全库与 benchmark 事件流/工件 0 命中**；
- 无疑似真实凭证，无需触发泄漏处置流程。

**证据重建**：`benchmark/real/` 六个逐 run JSON + records + summary 已被重算值刷新；
原始事件流（`benchmark/real/home/`）未动；重算审计（manifest + CHANGES.md 新旧对照）
在 `benchmark/recomputed/`。FINAL-ACCEPTANCE §4b 已替换为重算表并附更正声明；
REMEDIATION-REPORT §7/§9/§10 与 AGENT.md 状态同步更新。

## 6. 仍未完成（如实清单）

1. **WP-B1 外部基准评测未启动**（任务书：四项整改通过后才启动）：SWE-bench Verified Mini /
   Lite / Pro V2 / SWE-rebench V2 / Terminal-Bench 的链路 smoke、小规模配对评测与编排专项
   任务集（§7.3 的 10~15 个）全部未做。
2. **真实规划路径未被真实任务触发**：现有 2 个简单任务均被判 direct；plan/multi 的真实规划
   全链路仍需复杂任务集。
3. `conflicts` 现在如实报 null，事件级计数仍未实现；审批持久化注册表/HTTP 审批队列未实现；
   集成验证 affected-only 定向未做；CLI `serve` 长驻服务形态未做。
4. 真实评测的样本量（n=2）不支持任何策略优劣结论——multi 在其上更慢的事实已记录。

## 7. Benchmark 阶段 A/B

未执行（见 §6.1）。启动前置条件：本报告的 WP-C1~C4 通过评审。

## 8. Windows 本地复验步骤与预期

```bash
git clone --branch feature/v2-benchmark-integrity-fixes https://github.com/hejinlin0824/jinlin-agent.git
cd jinlin-agent
python -m pytest -q            # 预期 396 passed + 3 skipped / 0 failed
python -m jinlin_agent eval --suite all   # 预期 22/22 probes passed
python scripts/benchmark_matrix.py --tasks docs/architecture/v2/benchmark/dryrun/tasks.json ^
  --out %TEMP%\r3-dryrun --strategies single,planner,multi --dry-run
# 预期：3 策略 completed；verification_status=not_applicable；tokens_actual=null + usage_complete=False
python scripts/recompute_benchmark.py --home docs/architecture/v2/benchmark/real/home ^
  --records docs/architecture/v2/benchmark/real --tasks docs/architecture/v2/benchmark/real/tasks.json ^
  --out %TEMP%\r3-recomputed --reuse-manifest
# 预期：6 条重算记录，与入库的 benchmark/recomputed/ 逐字段一致（除时间戳类字段）
python -m pytest tests/test_token_accounting.py tests/test_task_verification.py tests/test_ledger.py -v
```

## 9. 声明

本轮：未修改 `main`、未向 `main` 合并、未 force-push；仅在
`feature/v2-benchmark-integrity-fixes` 分支新增提交；未删除/放宽/跳过任何既有安全测试或
验收门（全量测试从 368 增至 396，全部为新增）；MockProvider 结果未进入任何真实 usage 字段
（mock 的 attempt usage 为 null 且 usage_complete=false）；API key 仅经环境变量注入。
