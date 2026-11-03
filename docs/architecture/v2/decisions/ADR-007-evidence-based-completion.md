# ADR-007：完成状态只能由 Completion Gate 依据证据给出——扩展到任务级与集成级

- 状态：Proposed（待项目负责人确认）
- 日期：2026-11-03
- 关联：v2.0 §10、§18-7；AGENTS.md 硬规则 3/6/9

## 背景

"模型提议、代码裁决"已是仓库核心语义：模型只能 `propose_completion`（task.py:280），`CompletionGate.check` 用 13 类机器判定否决（verify.py:548-675），假成功在真实模型上实测为 0（README §9）。v2 的增量是：多 Worker 之后，"子任务完成"与"根任务（集成后）完成"是两个新的完成裁决点。

## 决策

1. 现有会话级 gate 语义不变（L0/L1/L3 必需、L5 回归、claim 证据绑定、结果五分级）。
2. 新增两个裁决点，全部复用 `CompletionGate` 结构扩展：
   - **子任务 gate**：Worker 交付的 TaskResult 必须含 changed_files/commit/tests/known_risks（v2 §7.4），其"测试通过"类 claim 同样必须引用通过的 L3（AGENTS.md 硬规则 9 原样适用）；
   - **集成 gate**：全部子任务 closed + merge 后**重跑**受影响验证（L3 不可豁免）+ 集成分支无未解决冲突 + 根任务验收标准逐条有证据。
3. 验证计划按 TaskContract + 实际 Diff + 项目配置自适应（v2 §10.2）——现有 `_run_harness_verification`（loop.py:367-389）已经是该逻辑的会话级版本，扩展为可被 TaskSpec 声明的必需等级覆盖；**必需等级不得因预算静默跳过**，预算不足 → 结果标 `partial`/`blocked` 而非 `completed`（v2 §10.4；gate 现有五分级直接支持）。
4. 验证证据记录补齐 v2 §10.3 字段——`VerificationRun`（domain.py:333-353）已含 command/cwd/exit_code/status/output_artifact_id/时间，补"运行环境（沙箱等级/engine）与对应 commit"两个字段。

## 备选方案

- 让 Supervisor/Reviewer 的模型判断替代部分 gate 检查：否决——T4 的全部价值就在"不依赖模型配合"；reviewer 的结论只能作为 gate 的输入证据，不能越过它。

## 后果

- 探针扩展（WP-10）：伪造子任务交付、集成后测试失败仍报完成 → 必须被拒。
- L4 仍未实现的事实保持显式（current-state.md §9）：任务合同若声明 L4 必需而项目无该能力，gate 必须 reject 而非静默跳过。
