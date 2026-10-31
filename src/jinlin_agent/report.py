"""Final report: every 'done' claim must point at evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import utcnow_iso


def _fmt_result(result: str) -> str:
    return {
        "completed": "完成",
        "completed_with_warnings": "完成（有警告）",
        "partial": "部分完成",
        "blocked": "阻塞",
        "failed": "失败",
    }.get(result, result)


def build_report(state: Any, gate: Any, config: Any, provider_name: str = "") -> str:
    completion = getattr(state, "completion", None) or {}
    result = (gate.result.value if gate is not None else completion.get("result", "failed"))
    lines: list[str] = []
    lines.append("# 任务结果")
    lines.append("")
    lines.append("## 状态")
    lines.append("")
    lines.append(f"**{_fmt_result(result)}**")
    lines.append("")
    lines.append(f"- session: `{state.session_id}`")
    lines.append(f"- 模型: `{state.model}`" + (f" ({provider_name})" if provider_name else ""))
    lines.append(f"- 工作区: `{state.workspace}`")
    lines.append(f"- 沙箱等级: `{state.sandbox_level}`")
    lines.append(f"- 阶段: `{state.phase}` / steps: {state.step_index} / model calls: {state.model_calls} / tool calls: {state.tool_calls_total}")
    lines.append(f"- tokens: in {state.tokens_in} / out {state.tokens_out}")
    lines.append("")

    lines.append("## 任务")
    lines.append("")
    lines.append((state.request or "").strip() or "(no request recorded)")
    lines.append("")

    lines.append("## 完成内容")
    lines.append("")
    claims = state.claims or []
    if claims:
        for claim in claims:
            refs = ", ".join(f"`{r}`" for r in claim.evidence_refs) or "**无证据**"
            lines.append(f"- {claim.statement} — 证据: {refs}")
    else:
        lines.append("- （未产生完成声明）")
    lines.append("")

    lines.append("## 修改文件")
    lines.append("")
    written = state.written_files or {}
    deleted = state.deleted_files or []
    if not written and not deleted:
        lines.append("（无工作区修改）")
    for path, meta in written.items():
        lines.append(f"- `{path}`")
        if meta.get("hash_after"):
            lines.append(f"  - sha256 after: `{meta['hash_after']}`")
        if meta.get("hash_before"):
            lines.append(f"  - sha256 before: `{meta['hash_before']}`")
    for path in deleted:
        lines.append(f"- `{path}` (deleted)")
    lines.append("")

    lines.append("## 验证结果")
    lines.append("")
    if state.verification_runs:
        for run in state.verification_runs:
            cmd = " ".join(run.command) if run.command else run.type
            detail = ""
            if run.summary:
                detail = " — " + json.dumps(run.summary, ensure_ascii=False)
            extra = f" — {run.status}"
            if run.reason_skipped:
                extra += f"（{run.reason_skipped}）"
            lines.append(f"- `{cmd}` ({run.level}){extra}{detail}")
            if run.findings:
                for finding in run.findings[:5]:
                    lines.append(f"  - {finding}")
            if run.output_artifact_id:
                lines.append(f"  - 输出: artifact `{run.output_artifact_id}`")
    else:
        lines.append("（没有验证记录）")
    lines.append("")

    lines.append("## 未解决问题")
    lines.append("")
    problems = list(getattr(gate, "reasons", []) if gate is not None else [])
    if state.pending_question:
        problems.append("等待用户回答: " + str(state.pending_question.get("question", "")))
    if problems:
        for problem in problems:
            lines.append(f"- {problem}")
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("## 风险与注意事项")
    lines.append("")
    warnings = list(getattr(gate, "warnings", []) if gate is not None else [])
    if state.config_violations:
        warnings.append("项目配置尝试放宽权限，已被忽略: " + ", ".join(v.get("key", "?") for v in state.config_violations))
    if state.sandbox_level in ("L0", "L1-job"):
        warnings.append(
            f"沙箱等级为 {state.sandbox_level}: 进程树受限但文件写入没有 OS 级隔离，"
            "依赖策略与审批；生产使用前请评估。"
        )
    if warnings:
        for warning in warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("## 证据")
    lines.append("")
    lines.append(f"- 事件日志: `{state.session_id}/events.jsonl`")
    if state.report_artifact_id:
        lines.append(f"- 报告 artifact: `{state.report_artifact_id}`")
    for run in state.verification_runs:
        if run.output_artifact_id:
            lines.append(f"- {run.level} 输出: `{run.output_artifact_id}`")
    lines.append(f"- 配置哈希: `{state.config_hash}`")
    lines.append(f"- 生成时间: {utcnow_iso()}")
    return "\n".join(lines) + "\n"


def write_report(session_dir: Path, artifacts: Any, state: Any, gate: Any, config: Any, provider_name: str = "") -> str:
    body = build_report(state, gate, config, provider_name)
    artifact = artifacts.write("final_report", body, meta={"session": state.session_id})
    try:
        (Path(session_dir) / "report.md").write_text(body, encoding="utf-8")
    except OSError:
        pass
    state.report_artifact_id = artifact.artifact_id
    return artifact.artifact_id
