# WP-R1 整合报告 — 主线基线重建（评审任务书 §二）

## 1. 基线核实（评审要求"以 GitHub 真实状态为准"）
- `origin/main`：`249bbab` → **`bcb0e39`**（PR #1 已将 v1 加固线 review/followup-fixes 全量合入 main）
- `feature/v2-wp0-inventory`：领先 2 / **落后 169**；`feature/v2-wp1-task-ledger`：领先 15 / 落后 169（落后部分 = b0f3ade 之后的 v1 加固：rescue 生命周期/审计、trash 归属、artifact index 韧性、locks、patch、test-runner 元数据及约 4500 行新测试）
- 另发现新远端分支 `fix/wp1-cli-error-reporting`（未并入本次整合，待所有者处置）

## 2. 整合方式
新分支 `feature/v2-integration-remediation` 基于 `origin/main@bcb0e39` 建立，`git merge --no-ff feature/v2-wp1-task-ledger` 保留完整 v2 提交历史。

## 3. 逐文件决策（两侧改动全集对照）
**src 自动合并（无重叠）**：main 改 artifacts/cli/locks/patch/trash；v2 新增 11 个模块并改 context/domain/errors/evalkit/loop/policy/prompt/state/verify/tools-registry。唯一双方同改文件：
- `src/jinlin_agent/cli.py`：自动合并成功——保留 main 的 160 行新增（rescue/CLI 增强），同时保留 v2 的 `ledger`/`serve` 命令与 `eval --suite v2`。
**tests 自动合并（无重叠）**：main 新增 rescue/artifacts/trash 元数据测试 + test_security 扩展；v2 新增 9 个测试文件 + test_eval 参数化。
**docs 冲突（人工解决）**：
- `AGENT.md`：frontmatter 保留 main 的 v1 验收状态两行，新增 `v2_status` 键（合并后实测 324 passed / eval 22/22）；§16 v2 状态段保留。修复过程瑕疵：合并提交 `8009cfe` 曾带入未解决的 frontmatter 冲突标记，已在 `75042e3` 更正（如实记录）。
- `README.md`：保留 main 的 v1 收尾章节（Rescue 审计/隔离区恢复/test-runner 元数据/一键验收），其后保留 v2 §14 章节。
**配置自动合并**：`.github/workflows/ci.yml`、`docs/PLAN.md` 自动合并成功。

## 4. 合并后缺陷与修复（全量回归抓出，非预存在）
`WorkbenchServer.start()` 不等待监听就绪——324 测试重载下 accept 线程未调度即被连接 → 两次全量各随机挂一个 workbench 测试（连接拒绝/超时）。修复：start() 阻塞至端口可连接（10s 内），测试轮询时限放宽至 90s（负载容错，断言不弱化）。

## 5. 验收对照（评审任务书 WP-R1）
- ✅ 新整改分支基于最新 main（`bcb0e39`）
- ✅ 全部有效 v2 模块保留（11 新模块 + 9 测试文件 + cli/loop/policy/verify 等增强）
- ✅ 未丢失 main 侧安全能力（rescue/trash/artifacts/locks/patch 及其测试原样保留并全绿）
- ✅ 全量测试通过：**324 passed / 0 failed（3 skipped=平台守卫）/ eval 22/22**
- ✅ 逐文件决策记录：即本文件 §3
- 评审任务书提到的"原有 Workbench、TaskContract、VerificationPlan、API 身份认证"经再次逐提交核实：**main 的 169 个新提交中不存在这些模块**（该表述与 v2 设计文档 §2.2 同源的误设，见 gap-analysis §0）；main 侧真实新增 = rescue/trash/artifact 加固与测试。已按事实整合，未虚构保留对象。
