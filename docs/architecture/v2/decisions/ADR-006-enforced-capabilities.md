# ADR-006：能力集由 PolicyEngine + Sandbox 强制执行，不依赖 Prompt

- 状态：Proposed（待项目负责人确认）
- 日期：2026-11-03
- 关联：v2.0 §9.4/§9.6、§18-6；AGENTS.md 硬规则 4/7

## 背景

权限强制已是本仓库的传统强项：路径边界（paths.py 三层解析）、命令风险分级（policy.py:125-178）、fail-closed 审批（policy.py:202-247）、配置只紧不松（config.py:126-155）、`<untrusted>` 注入隔离（prompt.py:116-130）、"提示词无法提权"（README §5）。当前的能力表达只有两个维度：工具 `side_effect` 枚举 + 会话级 `read_only` 开关。v2 要求任务级最小权限（TaskSpec.capabilities + workspace_scope）。

## 决策

1. 能力集（READ/WRITE/EXECUTE/NETWORK/DELETE/USER_INPUT/APPROVAL/WORKFLOW）成为 **TaskSpec 字段**；`PolicyEngine.evaluate` 增加"任务能力集"判定维度（现状只有 `read_only` 一个开关，policy.py:281-288）。
2. `workspace_scope.read/write` 路径范围在现有 `PathPolicy` 之上实现：resolve 后追加"是否在任务写范围内"检查，语义与错误码复用 `PATH_OUTSIDE_WORKSPACE`/`PROTECTED_FILE_ACCESS` 家族（新增 `SCOPE_VIOLATED`，errors.py 家族扩展）。
3. 修补 v2 §2.3-3 证实的缺口：目前**没有任何工具声明** NETWORK/DESTRUCTIVE/EXTERNAL side_effect（grep 0 命中）——为未来具备这些副作用的工具补声明 + 策略路径；网络域名/端口级允许列表进入 config 只紧不松规则表。
4. 审批对象具体化并绑定参数（现状 ApprovalRequest 已含 arguments/impact，policy.py:184-198），按 v2 §9.6 补有效期；session 级授权（`session_grants`）在任务层收窄为 task 级授权。
5. 高风险默认拒绝或审批，随现有 `approval.*` 配置键走；新键全部加入 `_RULES`（项目级只能更严）。

## 备选方案

- 在 Prompt 里写"你不许访问 X"：否决——这正是 AGENTS.md 硬规则 4 与 T4 的存在意义；仓库 `<untrusted>` 机制已保证仓库内容无法自我提权。
- 为 Worker 单独再造一套策略引擎：否决——在现有 `PolicyEngine.evaluate` 加维度即可，评审面最小。

## 后果

- WP-6 的安全测试矩阵（v2 §16.4：路径穿越/符号链接/prompt 注入/shell 注入/越权删除/伪造完成消息）大半已有现成回归（test_security.py 1405 行），新增的是"跨 Worker 越权"用例。
- 顺序约束（v2 §15）：本 ADR 的 1/2 两项必须先于 Worker 获得写能力落地。
