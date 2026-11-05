# 第四轮收尾修复报告（ROUND4 FINAL FIX REPORT）

> 对应《第三轮收尾修复包（WP-R4-01~06）》。结论先行：**四项修复全部落地并有回归测试锁定；
> dry-run 与六条真实记录已按修复后语义重建（原始事件流 SHA-256 前后一致，243 个文件）；
> gitleaks 8.28.0 全工作树 + 全历史扫描完成（111 处命中全部为事件流标识符字段的误报，
> 零真实凭证，逐条分类留档）；Windows junction 解码警告已消除；最终 HEAD 干净复验
> 407 passed + 3 skipped / eval 22/22、双平台 CI 绿（run 回填于 §7）。WP-B1 未启动。**

## 1. 基线与提交链

| 项 | 值 |
|---|---|
| 修复包基线 HEAD | `6aceada`（任务书指定，开始前核对一致） |
| `main` | `bcb0e39`（未修改、未合并、未 force-push） |
| 分支 | `feature/v2-benchmark-integrity-fixes`（沿用，未重置历史） |
| 本轮提交 | `2268f6e`（WP-R4-01）→ `6a23187`（WP-R4-02）→ `c90882c`（WP-R4-03）→ `cb7ea92`（WP-R4-04）→ `ad11a51`（WP-R4-05）→ 本提交（WP-R4-06 报告与终验） |

开始前检查已执行并记录：`git fetch origin --prune`、分支切换、HEAD 与 `origin/main` 比对、
工作区干净（0 个未提交改动）。

## 2. WP-R4-01：监督式任务 Token 聚合修复（`2268f6e`）

**缺陷确认（与任务书 §2.1 一致）**：①聚合器以 `sm[key] or 0` 累加，把无 usage 会话的 `None`
折算成 0；②根任务不计算 `tokens_total_actual`；③`usage_complete` 不受缺失会话日志影响。
入库反例核验属实：`dryrun/analyze-demo_multi.json` 曾为 in/out=0 + missing=8 + complete=false，
且 summary 误计 `runs_with_usage=1`。

**修复语义**（`scripts/benchmark_matrix.py::root_record_fields` / `aggregate`，
`scripts/recompute_benchmark.py` 同步）：
- 实际值只从**观测到**的 usage 累加（显式 `is not None` 判断）；整根无任何观测时三个
  `tokens_*_actual` 全部为 `null`；
- `tokens_total_actual` 在根级显式计算 = `tokens_in_actual + tokens_total_actual` 的对应和；
  有观测即数值，无观测即 null；
- `usage_complete` = 全部应参与 Worker 会话均已读取 **且** 每条已记录 attempt 均带 usage；
  缺失会话计数进新字段 `usage_missing_sessions` / `usage_expected_sessions`（缺失会话的
  attempt 数不可知，**不**虚构进 `tokens_usage_missing_calls`）；
- `aggregate()` 区分 null 与合法数值 0：`runs_with_usage` = 至少一条 usage 观测的 run 数
  （数值 0 仍算有 usage）；新增 `runs_with_partial_usage` / `runs_without_usage`；
  `tokens_total_actual_total` 仅在存在观测时为数值；
- 单会话与监督式语义一致；预算门 legacy mixed counters 未动（任务书 §2.3.12）。

**回归测试**（`tests/test_token_accounting.py` + `tests/test_benchmark_matrix.py`，
任务书 §2.4 清单逐项）：`test_supervised_mock_provider_reports_null_actual_usage`（planner 与
multi 各自经 `run_one(..., dry_run=True)` 真实监督管线）、
`test_supervised_root_total_actual_equals_input_plus_output`、
`test_supervised_root_sums_provider_usage_without_context_estimates`（双 Worker 不同 usage，
估算不混入）、`test_supervised_root_partial_usage_keeps_partial_sum_but_marks_incomplete`、
`test_missing_worker_session_log_marks_usage_incomplete`（直接单元：真账本 + 真会话目录，
缺失日志可追溯）、`test_supervised_root_all_missing_usage_is_null_not_zero`、
`test_aggregate_does_not_count_null_actual_as_run_with_usage`，另有 error 传播测试（§3）。

## 3. WP-R4-02：未知 verification profile fail-closed（`6a23187`）

**缺陷确认**：`verification_evidence()` 此前丢弃了 `required_verification_levels()` 的
`known` 标志——未知 profile 会回退到 code 等级并可能在 benchmark 记为 `passed`，而
Completion Gate 同场景会拒绝。

**修复**：`verification_evidence()` 对未知 profile 直接返回 `status="error"`、`passed=false`
、notes 含 `unknown verification profile 'mystery'`（先于 not_run/not_applicable 判定，
故空记录的未知 profile 也是 error）；`check_task_verification()` 拒绝行为未变（未放宽
Completion Gate）；`_aggregate_verification()` 将 `error` 作为独立 fail-closed 状态传播
（仅 `failed` 排序更高），兄弟 Worker 的 `passed` 不能覆盖。

**回归测试**：`test_unknown_profile_never_benchmark_passes`、
`test_unknown_profile_empty_runs_is_not_not_applicable`、
`test_unknown_profile_fails_task_gate_and_benchmark_consistently`（同一未知 profile 在任务门
与 benchmark 证据路径都不通过）、`test_aggregate_verification_propagates_error_over_sibling_pass`。

## 4. WP-R4-03：Windows junction 解码警告（`c90882c`）

`tests/test_security.py::_make_junction()` 改为字节模式 `capture_output=True`（无
text/encoding 参数）：cmd.exe 输出为 OEM 代码页，文本模式按 UTF-8 解码会在 reader 线程抛
`UnicodeDecodeError`。helper 只消费 returncode 与 link 存在性，无需解码输出；失败时的
pytest.skip 行为与路径穿越断言保持不变。

验证：`python -m pytest tests/test_security.py -q -W error::pytest.PytestUnhandledThreadExceptionWarning`
→ 50 passed；全量套件同参数（§7）0 个该警告。

## 5. WP-R4-04：全范围 Secret Scan（`cb7ea92`）

| 项 | 值 |
|---|---|
| 工具 / 版本 | gitleaks **8.28.0**（官方发行版） |
| 工作树扫描 | `gitleaks dir . --redact --report-format json ... --no-banner`，退出码 1（有 findings） |
| 历史扫描 | `gitleaks git . --redact --report-format json ... --no-banner`，退出码 1（有 findings）；非浅层，372 提交全 refs |
| 机器报告（脱敏，已入库） | `docs/architecture/v2/round4-security/gitleaks-working-tree.json`（41 findings）、`gitleaks-git-history.json`（70 findings） |
| 分类结论 | **111 处全部为误报**：generic-api-key 规则命中审计事件流中三个 harness 生成的 opaque 标识符字段（`session_id` / `payload.tool_call_id` / `payload.idempotency_key`；含历史提交 `a729547e` 中已删除的 v1 时代 `home/sessions/`）。分类脚本化：解析命中行、**只记录字段名**，值不落盘 |
| 真实凭证 | **0**。所有者评测 key 仅存在于执行期环境变量，工作树与全部 372 提交零命中 |
| allowlist | **未添加**（评估后认为 event-stream 路径级 allowlist 会掩盖未来真实泄漏；findings 保持可见，以分类报告为准） |

完整分类表：`docs/architecture/v2/round4-security/SECRET-SCAN-REPORT.md`。

## 6. WP-R4-05：证据重建（`ad11a51`）

- **dry-run**：干净重建后逐项通过任务书 §6.2 检查——无 usage 记录 actual 全 null（非 0）、
  `usage_complete=false`、`runs_with_usage=0`（入库反例消除）、估算独立、verification 按证据
  （single 的 code 档案无记录 → not_run；analysis 子任务 → not_applicable）。
- **六条真实记录**：按任务书 §6.3 流程执行——预览（临时目录）→ manifest 核查（6 条映射全部
  存在且唯一）→ 应用到 `benchmark/real/` + `benchmark/recomputed/` → 复跑预览到全新目录，
  **与已应用派生数据逐字段一致（diff = NONE）**。
- **自检**（§6.4）：所有记录 `tokens_total_actual == tokens_in_actual + tokens_out_actual`
  （有观测时）；analyze × planner 保持 `not_run / false`；全部 `usage_complete=true`、
  `usage_missing_sessions=0`。
- **原始事件流未被修改的证明**：`benchmark/real/home/**` 共 243 个文件，SHA-256 前后清单
  完全一致——`docs/architecture/v2/round4/home-sha256-before.json` / `home-sha256-after.json`。

## 7. WP-R4-06 与最终复验

- 最终 HEAD 全量：`python -m pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning
  --junitxml=...` → **407 passed / 0 failed / 0 errors / 3 skipped**（测试计数从 396 增加 11：
  R4-01 七个 + R4-02 四个）；`eval --suite all` → **22/22**；警告 0。日志：`round4/logs-pytest-final.log`。
- 全新 checkout 终验（§10 四条命令）与双平台 CI：见 §8 回填（Run ID 由随后文档提交写入）。
- 文档同步：`FINAL-ACCEPTANCE-REPORT.md` §4b 附 R4 修正说明；`REMEDIATION-REPORT.md` §7 测试
  数字更新为 407；`AGENT.md` 状态更新；`SECRET-SCAN-REPORT.md` 新增。

### CI 回填区

| Run ID | HEAD | Job 结论 |
|---|---|---|
| （待回填） | 本提交 | windows-latest / py3.13、ubuntu-latest / py3.13 |

## 8. 未完成与残余风险

1. WP-B1 外部基准评测未启动（维持任务书顺序约束，smoke test 亦未执行，不冒充正式 benchmark）。
2. `conflicts` 仍为 null（事件级计数未实现）；审批持久化注册表/HTTP 审批队列未实现；
   affected-only 集成验证未做；CLI `serve` 长驻服务形态未做。
3. 真实规划路径仍未被真实任务触发（评估器对简单任务判 direct）；n=2 不支持策略优劣结论。
4. benchmark 事件流中的标识符字段会持续被 generic-api-key 规则命中（每轮扫描退出码 1），
   以 SECRET-SCAN-REPORT.md 的分类为准；未加 allowlist 是有意决定。
