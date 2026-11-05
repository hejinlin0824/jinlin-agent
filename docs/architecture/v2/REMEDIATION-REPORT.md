# v2.0 整改报告（REMEDIATION REPORT）

> 对应《评审任务书》(2026-11-03/04)。结论先行：**P0（R1–R4）与 P1（R5–R9）全部完成并有测试与 CI 证据；P2（R10）交付了评测骨架与示例任务集，真实运行需 API key，未执行——不虚构任何数字。** 建议合并前由所有者完成一次真实任务集试运行。
>
> **收尾轮（最终验收任务书 WP-F1~F5）已在其后执行**：三策略代码层真实区分、MockProvider
> 真实管线 dry-run（证据入库 `benchmark/dryrun/`）、14 项指标独立统计、文档与 CLI 行为核对。
> 最终验收结论以 `FINAL-ACCEPTANCE-REPORT.md` 为准；本报告的 §6/§7/§8 已按收尾轮更新。

## 1. 整改前后的 Git 基线

| 项 | 整改前 | 整改后 |
|---|---|---|
| 基线分支 | v2 分支基于旧线（落后 main 169 提交） | `feature/v2-integration-remediation` 基于 **`origin/main@bcb0e39`**（PR #1 合入 v1 加固线后） |
| v2 实现 | 散在 `feature/v2-wp0-inventory` / `feature/v2-wp1-task-ledger` | 全量合并入整改分支（merge `8009cfe`，历史保留） |
| main | `249bbab` | `bcb0e39`（未改动——main 受保护） |

## 2. 工作包完成情况

| 工作包 | 优先级 | 状态 | Commit |
|---|---|---|---|
| WP-R1 主线整合 | P0 | ✅ | `8009cfe` + `75042e3`（AGENT.md 冲突标记更正）+ `1c9938e`（就绪竞态修复 + 本报告前身） |
| WP-R2 集成验证 Sandbox 化 | P0 | ✅ | `b4c2896` |
| WP-R3 Workbench 安全 | P0 | ✅ | `f033fd6` |
| WP-R4 能力三层交集 | P0 | ✅ | `d719224` |
| WP-R5/6/7 恢复语义 | P1 | ✅ | `e8db903` |
| WP-R8 CLI 任务入口 | P1 | ✅ | `fc1cfd0` |
| WP-R9 Workbench 完善 | P1 | ✅（部分项如实未做，见 §10） | `fc1cfd0` |
| WP-R10 真实任务评测 | P2 | 🟡 骨架完成，真实运行未执行 | 本提交（scripts/benchmark_matrix.py） |

## 3. 核心架构变化
无推翻性变化：v2 全部为增量模块（taskmodel/ledger/planner/supervisor/protocol/worker_runtime/workspace_manager/context_provider/workbench），单一执行内核（`loop.Harness`）保持唯一；任务账本为 sqlite 投影 + 任务事件流（JSONL），"先事件后提交、可重建"不变；main 侧 v1 加固（rescue/trash/artifacts/locks/patch）原样保留并全绿。

## 4. 安全整改说明（WP-R2/R3/R4）
- **集成验证不再绕过 Sandbox**（WP-R2）：`IntegrationCoordinator._verify` 经 `Sandbox.run` 执行项目测试；sandbox 构建失败/拒绝执行一律 fail-closed（verification_failed + 原因落证据）；无任何 subprocess 回退（测试用守卫夹具强制证明，git 管道按评审任务书原文属另一信任边界）；sandbox level/engine 记入验证证据与 `integration.verified` 事件。
- **Workbench**（WP-R3）：页面重写为 textContent-only DOM 构造（零 innerHTML，测试植入恶意标题验证）；每次启动随机 Bearer token（`secrets.token_urlsafe(32)`，内存保存）覆盖全部 `/api/*`；Host 必须 127.0.0.1/localhost（防 DNS rebinding）；POST Origin 校验（防 CSRF）；保持 loopback-only；HTTP 审批仍不暴露。
- **权限交集**（WP-R4）：`effective = task ∩ role ∩ system`；None=不约束、空集=硬拒绝（不得退化为无限权限）；system 层 = 既有 config 硬门（allow_write/allow_delete/approval 默认 deny），其他层不可覆盖；检查发生在审批与 tool.intent 之前；测试覆盖"任务授 WRITE 而角色不授 → 拒绝"与反向，以及仓库自我授权文本（AGENTS.md 声称 may WRITE）被无视。

## 5. 恢复机制说明（WP-R5/R6/R7）
- **Worker 恢复**：`recover_interrupted()` 以会话事件日志为真相——终态会话回填 worker 行（completed 会话补记交付物）；会话缺失/非终态 → worker `failed(RECOVERY_UNCERTAIN)` + 任务 `blocked`（人工裁决，绝不自动重试成功）；queued 保留可重排；cancelled 永不复活。
- **Worktree 恢复**：`WorkspaceManager.recover()` 从 `git worktree list --porcelain` 重新领养非终止 worker 与"已完成未集成"的 worktree（base_commit 取自持久化记录）；未识别目录仅报告为孤儿，**绝不自动删除**。
- **Supervisor 恢复**：`execute_root` 天然可续跑（状态全部来自账本）；`_integrate_all` 对"有改动但无集成提交且 worktree 不可恢复"的完成任务 → 根任务 `blocked`（恢复失败需裁决），绝不静默跳过后标成功。
- **审批**（WP-R7）：`waiting_approval` 与 `waiting_user` 严格分离；审批恢复必须显式 `approval_decision`（denied 可续；headless 通道的 approved_once 被拒绝并指向交互式 CLI 授权——如实记录：持久化审批注册表与 HTTP 审批队列未实现）；审批请求以 `task.approval_required` 协议消息上浮。
- **Schema v3**：`tasks.integration` + `workers.last_commit`（v2→v3 ALTER 迁移，user_version fail-closed），根任务完成要求所有子任务 integration ∈ {integrated, not_needed}。

## 6. CLI 与 Workbench 实际使用说明
```bash
# 任务级入口（新；支持规划/多 Worker/集成/恢复）
jinlin-agent task run --workspace ./project -t "实现用户认证模块"
jinlin-agent task status <task_id>
jinlin-agent task resume <task_id> --answer "..."
jinlin-agent task cancel <task_id> --reason "..."
jinlin-agent task trace <task_id> --tail 50

# 旧单 Agent 路径（未变）
jinlin-agent run --workspace ./project -t "..."

# 可观测
jinlin-agent serve --port 7800        # 127.0.0.1 Workbench（纯只读：见下方说明）
jinlin-agent ledger [--rebuild] [--metrics]
```
**`serve` 的只读边界（与实现一致，WP-F4 核对）**：CLI `serve` 以 ledger-only 模式启动
（`WorkbenchServer(cfg, runtime=None, supervisor=None)`），只提供 GET 观测端点
（任务/Worker 列表与详情、决策时间线）；所有变更端点（POST 创建任务、cancel、answer）
在无附着 Runtime/Supervisor 时一律返回 405。创建/取消/回答**只能**通过库内嵌使用：
`WorkbenchServer(cfg, runtime=<WorkerRuntime>, supervisor=<Supervisor>)`（需要可用的
provider）。HTTP 审批（approval）有意不暴露。

## 7. 完整测试报告
- `python -m pytest -q` → 收尾轮时点 **368 passed + 3 skipped**；第三轮整改（WP-C1~C3，分支
  `feature/v2-benchmark-integrity-fixes`）后为 **396 passed + 3 skipped**（新增：验证证据语义
  8 + token 记账 10 + Ledger integration 语义 5 + benchmark 矩阵 6→12）/ 0 failed
- `python -m jinlin_agent eval --suite all` → **22/22**（18 v1 + 4 v2：P7 并行重叠 / P8 依赖顺序 / P9 能力越界拒绝 / P10 预算不重试）
- 新增专项测试：WP-R1 就绪竞态、WP-R2 六个（经 sandbox/拒绝 fail-closed/构造失败/超时回滚/cwd 正确/Windows 恶意外逃）、WP-R3 四个（XSS/Token/Origin/Host）、WP-R4 七个（交集语义/两向扩张/system 覆盖/空集硬拒绝/集成）、WP-R5/6/7 十一个（领养/孤儿/中断 fail-closed/会话回填/queued 存活/重启集成/blocked 根/审批三态）、WP-R8 五个 CLI。
- CI：推送后自动触发（`feature/**` 已在触发列表）；结果见分支 Actions 页。

## 8. Windows 和 Ubuntu CI 结果
整改轮 CI 曾双平台绿（run 41628012675）。**最终 HEAD 的 CI 以最终验收报告为准**：
`docs/architecture/v2/FINAL-ACCEPTANCE-REPORT.md` 记录最终 commit 的双平台 run ID。
注：WP-R2 期间 CI 曾抓出 ubuntu 独有的"PATH 缺失导致验证命令无法执行"缺陷，已修复并留档（wp-v2.0-5-report §6.1）。

## 9. 仍然存在的问题
1. **真实任务集评测：初步真实评测已执行**（所有者提供 Ark deepseek-v4-flash 凭证，2 简单任务 ×
   3 策略 = 6/6 完成并通过独立验证，证据与发现见 `FINAL-ACCEPTANCE-REPORT.md` §4b）。
   **仍未做**：含真实复杂度、能让评估器选择 plan/multi 的任务集（本轮两任务均被判 direct，
   真实规划路径未被触发）；多 Worker 是否转默认策略仍**不得**据此下定论（任务书 §一.8）。
2. 协作式取消为步边界（进行中调用先完成）；进程级硬杀仅存在于沙箱命令层。
3. HTTP 审批队列（持久化审批注册表）未实现；headless worker 审批 fail-closed，`approved_once` 仅交互 CLI 可授。
4. Windows 更强沙箱后端（Job Objects 之外的 Hyper-V/WSL2/ACL 矩阵）与资源配额未评估/实现。
5. L4 行为验证仍未实现（v1 遗留，如实保留）。
6. 事件 hash chain、跨文件崩溃事务、递归脱敏（v1 遗留延后项）继续延后。

## 10. 未完成事项（明确声明，不用文档代替实现）
- R10 的扩大真实运行（三种策略 × 30–50 任务、含能触发规划/多 Worker 的复杂任务）：**未执行**。
  已完成的是初步真实评测（2 简单任务 × 3 策略，6/6 通过，见 FINAL-ACCEPTANCE-REPORT §4b）；
  在复杂任务集评测完成前不得宣称多 Agent 优于单 Agent。
- R9 中"查看代码变更 Diff / 查看审批请求详情"的 Workbench 视图未做（数据已在 API 中：diff 在任务事件、审批在 approval_required 消息）。

## 11. 是否建议合并
**建议有条件合并**：`feature/v2-integration-remediation` 可合入 main——前提是所有者抽检本报告 §4–§8 的任 2–3 项证据并跑通 §7 的两条命令。理由：P0 安全项全部落实且有回归锁定；恢复语义 fail-closed；v1 能力零退化；发布门槛（真实任务集）按任务书本属 P2 且明确"无证据不宣称"，不阻塞本次安全与整合整改的合并。多 Worker 默认策略**维持关闭**，直至 R10 真实评测给出数据。
