"""Verification ladder, baseline and the completion gate.

The gate is the single most important control mechanism (T4): the model can
*propose* completion, never declare it. Every check below is ordinary code over
the real workspace, the event log and the verification records.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .domain import CompletionResult, VerificationLevel, VerificationRun, new_id, utcnow_iso
from .errors import AgentError
from .sandbox.base import process_env


# --------------------------------------------------------------------------- detection


@dataclass
class DetectedProject:
    types: list[str] = field(default_factory=list)
    test_commands: list[list[str]] = field(default_factory=list)
    lint_commands: list[list[str]] = field(default_factory=list)
    build_commands: list[list[str]] = field(default_factory=list)
    instruction_files: list[str] = field(default_factory=list)


def _which(name: str) -> str | None:
    return shutil.which(name)


def venv_python(workspace: Path) -> Path | None:
    for candidate in (
        workspace / ".venv" / "Scripts" / "python.exe",
        workspace / "venv" / "Scripts" / "python.exe",
        workspace / ".venv" / "bin" / "python",
        workspace / "venv" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return None


def _python_module_cmd(workspace: Path, module: str, *args: str) -> list[str] | None:
    py = venv_python(workspace)
    if py is not None:
        return [str(py), "-m", module, *args]
    if _which(module) or _which("pytest"):
        return [module, *args]
    if module == "pytest":
        return [sys.executable, "-m", module, *args]
    return None


def detect_project(workspace: Path, config: Any) -> DetectedProject:
    workspace = Path(workspace)
    proj = DetectedProject()
    tests: list[list[str]] = []
    lint: list[list[str]] = []
    build: list[list[str]] = []

    pyproject = workspace / "pyproject.toml"
    pyproject_data: dict[str, Any] = {}
    if pyproject.exists():
        proj.types.append("python")
        try:
            pyproject_data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pyproject_data = {}
    if (workspace / "setup.py").exists() or (workspace / "requirements.txt").exists():
        if "python" not in proj.types:
            proj.types.append("python")

    has_tests = any(
        (workspace / d).is_dir() for d in ("tests", "test", "src/tests")
    ) or any(workspace.glob("test_*.py"))
    if "python" in proj.types and (has_tests or "pytest" in json.dumps(pyproject_data)):
        cmd = _python_module_cmd(workspace, "pytest", "-q")
        if cmd:
            tests.append(cmd)
        if pyproject_data.get("tool", {}).get("ruff") is not None or _which("ruff"):
            r = _which("ruff")
            if r and not r.lower().endswith(".exe"):
                lint.append([r, "check", "."])
            elif r:
                lint.append([r, "check", "."])
        if pyproject_data.get("tool", {}).get("mypy") is not None or _which("mypy"):
            cmd_m = _python_module_cmd(workspace, "mypy", ".")
            if cmd_m:
                lint.append(cmd_m)

    if (workspace / "package.json").exists():
        proj.types.append("node")
        pkg = _safe_json(workspace / "package.json")
        scripts = (pkg or {}).get("scripts") or {}
        npm = _npm_command()
        if npm and "test" in scripts:
            tests.append([*npm, "test", "--silent"])
        if npm and "lint" in scripts:
            lint.append([*npm, "run", "lint"])
        if npm and "build" in scripts:
            build.append([*npm, "run", "build"])

    if (workspace / "Cargo.toml").exists():
        proj.types.append("rust")
        if _which("cargo"):
            tests.append(["cargo", "test", "-q"])
    if (workspace / "go.mod").exists():
        proj.types.append("go")
        if _which("go"):
            tests.append(["go", "test", "./..."])
    if (workspace / "Makefile").exists() and _which("make"):
        proj.types.append("make")
        if "test:" in (workspace / "Makefile").read_text(encoding="utf-8", errors="replace").lower():
            tests.append(["make", "test"])

    configured = config.verification.get("test_command") or []
    if configured:
        tests = [list(configured)] + tests

    proj.test_commands = _dedupe(tests)
    proj.lint_commands = _dedupe(lint)
    proj.build_commands = _dedupe(build)
    proj.instruction_files = [f for f in config.project.get("instruction_files", []) if (workspace / f).exists()]
    return proj


def _safe_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _npm_command() -> list[str] | None:
    npm = _which("npm")
    if not npm:
        return None
    if npm.lower().endswith((".cmd", ".bat")) and sys.platform == "win32":
        node = _which("node")
        if node:
            for base in (Path(node).parent.parent / "node_modules" / "npm" / "bin" / "npm-cli.js",):
                if base.exists():
                    return [node, str(base)]
        return None
    return [npm]


def _dedupe(items: list[list[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    out: list[list[str]] = []
    for item in items:
        key = tuple(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


# --------------------------------------------------------------------------- parsing

_PASSED = re.compile(r"(\d+) passed")
_FAILED = re.compile(r"(\d+) failed")
_ERRORS = re.compile(r"(\d+) error")
_FAILED_ID = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_ERROR_ID = re.compile(r"^ERROR\s+(\S+)", re.MULTILINE)


def parse_test_output(text: str, exit_code: int | None, timed_out: bool = False) -> dict[str, Any]:
    text = text or ""
    out: dict[str, Any] = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "failed_ids": sorted(set(_FAILED_ID.findall(text))),
        "error_ids": sorted(set(_ERROR_ID.findall(text))),
    }
    for regex, key in ((_PASSED, "passed"), (_FAILED, "failed"), (_ERRORS, "errors")):
        matches = regex.findall(text)
        if matches:
            out[key] = max(int(x) for x in matches)
    if out["failed_ids"] and out["failed"] == 0:
        out["failed"] = len(out["failed_ids"])
    if out["error_ids"] and out["errors"] == 0:
        out["errors"] = len(out["error_ids"])
    return out


# --------------------------------------------------------------------------- runner


@dataclass
class VerificationOutcome:
    run: VerificationRun
    output: str = ""


class Verifier:
    def __init__(
        self,
        workspace: Path,
        config: Any,
        sandbox: Any,
        artifacts: Any,
        emit: Callable[[str, dict[str, Any]], dict[str, Any]],
        state: Any,
    ) -> None:
        self.workspace = Path(workspace)
        self.config = config
        self.sandbox = sandbox
        self.artifacts = artifacts
        self.emit = emit
        self.state = state
        self.project = detect_project(self.workspace, config)
        self.baseline: dict[str, Any] | None = None

    # ------------------------------------------------------------------ baseline

    def ensure_baseline(self) -> dict[str, Any] | None:
        if self.baseline is not None:
            return self.baseline
        if getattr(self.state, "baseline", None):
            if self.state.baseline.get("status") in ("skipped", "error"):
                return None
            self.baseline = self.state.baseline
            return self.baseline
        if not self.config.verification.get("run_baseline", True) or not self.project.test_commands:
            return None
        cmd = self.project.test_commands[0]
        result = self.sandbox.run(
            cmd,
            str(self.workspace),
            timeout=int(self.config.verification.get("baseline_timeout_seconds", 300)),
            env=process_env(str(self.workspace)),
        )
        parsed = parse_test_output(
            (result.stdout or "") + (result.stderr or ""), result.exit_code, result.timed_out
        )
        status = "passed" if (result.exit_code == 0 and not result.timed_out) else "failed"
        self.baseline = {"command": cmd, "status": status, **parsed}
        self.emit("verification.baseline", {"baseline": self.baseline})
        return self.baseline

    # ------------------------------------------------------------------ levels

    def run_ladder(self, levels: list[str]) -> list[VerificationRun]:
        runs: list[VerificationRun] = []
        for level in levels:
            run = self.run_level(level)
            runs.append(run)
        return runs

    def run_level(self, level: str) -> VerificationRun:
        if level == VerificationLevel.L0_DIFF.value:
            return self._level0()
        if level == VerificationLevel.L1_STRUCTURE.value:
            return self._level1()
        if level == VerificationLevel.L2_STATIC.value:
            return self._level2()
        if level == VerificationLevel.L3_TESTS.value:
            return self._level3()
        if level == VerificationLevel.L5_REGRESSION.value:
            return self._level5()
        run = VerificationRun(
            verification_id=new_id("ver"),
            level=level,
            type="unknown",
            status="skipped",
            reason_skipped=f"no implementation for level {level}",
            started_at=utcnow_iso(),
            completed_at=utcnow_iso(),
        )
        self.emit("verification.completed", {"verification": run.to_dict()})
        return run

    # ------------------------------------------------------------------ impl

    def _changed_files(self) -> list[str]:
        files = set(getattr(self.state, "written_files", {})) | set(getattr(self.state, "deleted_files", []))
        if files:
            return sorted(files)
        # fall back to git status (e.g. resumed session)
        res = self.sandbox.run(["git", "status", "--porcelain=v1"], str(self.workspace), timeout=30)
        out = []
        for line in (res.stdout or "").splitlines():
            if len(line) > 3:
                out.append(line[3:].strip().strip('"'))
        return out

    def _level0(self) -> VerificationRun:
        run = VerificationRun(
            verification_id=new_id("ver"),
            level="L0",
            type="diff-review",
            started_at=utcnow_iso(),
        )
        self.emit("verification.started", {"verification_id": run.verification_id, "level": "L0", "type": "diff-review"})
        changed = self._changed_files()
        findings: list[str] = []
        for rel in changed:
            path = self.workspace / rel
            if not path.exists():
                findings.append(f"deleted: {rel}")
                continue
            size = path.stat().st_size
            if size > int(self.config.workspace.get("max_file_bytes", 1_048_576)):
                findings.append(f"large file changed: {rel} ({size} bytes)")
            if _looks_secret(path):
                findings.append(f"suspicious sensitive content: {rel}")
        res = self.sandbox.run(["git", "diff", "--stat", "--no-color"], str(self.workspace), timeout=30)
        diff_stat = (res.stdout or "").strip()
        ok = not any(f.startswith("suspicious") for f in findings)
        run.status = "passed" if ok else "failed"
        run.findings = findings
        run.summary = {"changed_files": changed, "diff_stat": diff_stat[:4000]}
        run.completed_at = utcnow_iso()
        self.emit("verification.completed", {"verification": run.to_dict()})
        return run

    def _level1(self) -> VerificationRun:
        run = VerificationRun(
            verification_id=new_id("ver"),
            level="L1",
            type="structure",
            started_at=utcnow_iso(),
        )
        self.emit("verification.started", {"verification_id": run.verification_id, "level": "L1", "type": "structure"})
        issues: list[str] = []
        checked = 0
        for rel in self._changed_files():
            path = self.workspace / rel
            if not path.exists():
                continue
            checked += 1
            suffix = path.suffix.lower()
            try:
                if suffix == ".py":
                    source = path.read_text(encoding="utf-8", errors="replace")
                    compile(source, str(path), "exec")
                elif suffix == ".json":
                    json.loads(path.read_text(encoding="utf-8", errors="replace"))
                elif suffix == ".toml":
                    tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as exc:
                issues.append(f"{rel}: syntax error line {exc.lineno}: {exc.msg}")
            except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
                issues.append(f"{rel}: parse error: {exc}")
            except Exception as exc:
                issues.append(f"{rel}: {type(exc).__name__}: {exc}")
        run.status = "passed" if not issues else "failed"
        run.findings = issues
        run.summary = {"checked_files": checked}
        run.completed_at = utcnow_iso()
        self.emit("verification.completed", {"verification": run.to_dict()})
        return run

    def _level2(self) -> VerificationRun:
        run = VerificationRun(
            verification_id=new_id("ver"),
            level="L2",
            type="static",
            started_at=utcnow_iso(),
        )
        self.emit("verification.started", {"verification_id": run.verification_id, "level": "L2", "type": "static"})
        if not self.project.lint_commands:
            run.status = "skipped"
            run.reason_skipped = "no lint/type-check command detected in the project"
        else:
            outputs = []
            failed = False
            for cmd in self.project.lint_commands[:2]:
                res = self.sandbox.run(cmd, str(self.workspace), timeout=300, env=process_env(str(self.workspace)))
                outputs.append(f"$ {' '.join(cmd)}\nexit={res.exit_code}\n{(res.stdout or '')[-4000:]}")
                if res.exit_code != 0:
                    failed = True
            run.status = "failed" if failed else "passed"
            run.output_artifact_id = self.artifacts.write("verification_log", "\n\n".join(outputs), {"level": "L2"}).artifact_id
            run.findings = ["lint/type check failed"] if failed else []
        run.completed_at = utcnow_iso()
        self.emit("verification.completed", {"verification": run.to_dict()})
        return run

    def _level3(self) -> VerificationRun:
        run = VerificationRun(
            verification_id=new_id("ver"),
            level="L3",
            type="tests",
            started_at=utcnow_iso(),
        )
        self.emit("verification.started", {"verification_id": run.verification_id, "level": "L3", "type": "tests"})
        if not self.project.test_commands:
            run.status = "skipped"
            run.reason_skipped = "no test command detected (declare one in .jinlin/config.toml [verification].test_command)"
        else:
            cmd = self.project.test_commands[0]
            timeout = int(self.config.runtime.get("max_shell_timeout_seconds", 1800))
            targets = self._changed_test_targets()
            if targets and cmd and "-m" in cmd and "pytest" in cmd:
                cmd = [*cmd, *targets]
            res = self.sandbox.run(cmd, str(self.workspace), timeout=timeout, env=process_env(str(self.workspace)))
            combined = (res.stdout or "") + "\n" + (res.stderr or "")
            parsed = parse_test_output(combined, res.exit_code, res.timed_out)
            run.command = cmd
            run.exit_code = res.exit_code
            run.summary = parsed
            run.output_artifact_id = self.artifacts.write(
                "verification_log", combined, {"level": "L3", "command": cmd}
            ).artifact_id
            passed = (
                res.exit_code == 0
                and not res.timed_out
                and parsed["failed"] == 0
                and parsed["errors"] == 0
                and not parsed["failed_ids"]
                and not parsed["error_ids"]
            )
            if res.timed_out:
                run.findings = [f"test command timed out after {timeout}s"]
            elif not passed:
                run.findings = [
                    f"tests failed (exit {res.exit_code}, {parsed['failed']} failed, "
                    f"{parsed['errors']} errors, failed_ids={parsed['failed_ids'][:5]})"
                ]
            run.status = "passed" if passed else "failed"
        run.completed_at = utcnow_iso()
        self.emit("verification.completed", {"verification": run.to_dict()})
        return run

    def _level5(self) -> VerificationRun:
        run = VerificationRun(
            verification_id=new_id("ver"),
            level="L5",
            type="regression",
            started_at=utcnow_iso(),
        )
        self.emit("verification.started", {"verification_id": run.verification_id, "level": "L5", "type": "regression"})
        baseline = self.ensure_baseline()
        if baseline is None:
            run.status = "skipped"
            run.reason_skipped = "no baseline (no test command or baseline disabled)"
        else:
            cmd = baseline["command"]
            res = self.sandbox.run(
                cmd,
                str(self.workspace),
                timeout=int(self.config.runtime.get("max_shell_timeout_seconds", 1800)),
                env=process_env(str(self.workspace)),
            )
            combined = (res.stdout or "") + "\n" + (res.stderr or "")
            now = parse_test_output(combined, res.exit_code, res.timed_out)
            run.summary = {"baseline": baseline, "current": now}
            new_failed = set(now["failed_ids"]) - set(baseline.get("failed_ids", []))
            new_errors = set(now["error_ids"]) - set(baseline.get("error_ids", []))
            findings: list[str] = []
            # unknown process state must fail closed
            passed = not res.timed_out and res.exit_code is not None and not new_failed and not new_errors
            if res.timed_out:
                findings.append("regression test command timed out")
            if res.exit_code is None:
                detail = f" ({res.error})" if getattr(res, "error", None) else ""
                findings.append(f"regression command returned no exit code{detail}")
            if new_failed:
                findings.append("new failing tests: " + ", ".join(sorted(new_failed)[:5]))
            if new_errors:
                findings.append("new collection/test errors: " + ", ".join(sorted(new_errors)[:5]))
            if passed and res.exit_code not in (0, None):
                # non-zero exit is only acceptable when the failure set is exactly the known one
                known_failed = bool(now["failed_ids"]) and set(now["failed_ids"]) <= set(
                    baseline.get("failed_ids", [])
                )
                known_errors = bool(now["error_ids"]) and set(now["error_ids"]) <= set(
                    baseline.get("error_ids", [])
                )
                if not (known_failed or known_errors):
                    findings.append(f"test command exited {res.exit_code} without a matching known failure set")
                    passed = False
            if passed and res.exit_code == 0 and (now["failed_ids"] or now["error_ids"]):
                findings.append("test command exited 0 but its output still contains failures/errors")
                passed = False
            run.status = "passed" if passed else "failed"
            if findings:
                run.findings = findings
        run.completed_at = utcnow_iso()
        self.emit("verification.completed", {"verification": run.to_dict()})
        return run

    def _changed_test_targets(self) -> list[str]:
        targets = []
        for rel in self._changed_files():
            name = Path(rel).name
            if name.startswith("test_") and name.endswith(".py") or name.endswith("_test.py"):
                targets.append(rel)
        return targets[:5]


def _command_stem(exe: str) -> str:
    """'C:/Python313/python.exe' -> 'python', so prose like 'python -m pytest -q' matches."""
    name = Path(str(exe)).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _looks_secret(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return False
    patterns = (
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY-----",
        r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*['\"][^'\"]{12,}['\"]",
    )
    return any(re.search(p, text) for p in patterns)


# --------------------------------------------------------------------------- gate


@dataclass
class GateResult:
    passed: bool
    result: CompletionResult
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


class CompletionGate:
    def __init__(self, workspace: Path, config: Any, state: Any, sandbox: Any, verifier: Verifier | None = None) -> None:
        self.workspace = Path(workspace)
        self.config = config
        self.state = state
        self.sandbox = sandbox
        self.verifier = verifier

    def check(self, claims: list[Any], summary: str = "") -> GateResult:
        s = self.state
        reasons: list[str] = []
        warnings: list[str] = []
        evidence: dict[str, Any] = {}

        # 1. task exists
        if not (getattr(s, "request", "") or "").strip():
            reasons.append("no task objective recorded")

        # 2. plan complete
        plan = getattr(s, "plan", None)
        if plan is None or not plan.is_effective():
            reasons.append("no effective plan (objective + steps) was recorded")
        else:
            for step in plan.open_steps():
                reasons.append(f"plan step still open: {step.step_id} {step.description}")
            for step in plan.steps:
                if step.status == "completed" and not step.evidence:
                    reasons.append(f"plan step {step.step_id} completed without evidence")

        # 3. last observation not a failure
        obs = getattr(s, "recent_observations", [])
        if obs and not obs[-1].get("ok", True):
            err = obs[-1].get("error") or {}
            reasons.append(f"last tool call failed and was not resolved: {err.get('code', 'unknown')}")

        # 4. no pending approval
        if getattr(s, "pending_approval", None):
            reasons.append("an approval is still pending")

        # 5. no unresolved conflict
        last_error = getattr(s, "last_error", None) or {}
        if last_error.get("code") in ("FILE_CHANGED_EXTERNALLY", "PATCH_CONFLICT", "WORKSPACE_DIRTY_CONFLICT"):
            reasons.append(f"unresolved edit conflict: {last_error.get('code')}")

        # 6. diff state / changed files
        changed = sorted(set(s.written_files) | set(s.deleted_files))
        evidence["changed_files"] = changed
        preexisting = set((s.workspace_snapshot or {}).get("dirty_files", []))
        git_now = self._git_changed()
        unexpected = sorted(set(git_now) - set(changed) - preexisting)
        if unexpected:
            reasons.append("files changed outside the agent's own edits: " + ", ".join(unexpected[:10]))
        if changed:
            pass  # code change: verification is mandatory below
        else:
            # read-only task: allow completion if claims are analysis-type
            if not any("no changes" in c.lower() or "analysis" in c.lower() for c in [summary]):
                warnings.append("no workspace changes were produced (read-only task)")

        # 7/8. verification
        runs = list(getattr(s, "verification_runs", []))
        by_level: dict[str, VerificationRun] = {}
        for run in runs:
            by_level[run.level] = run  # later runs win
        evidence["verification"] = {lvl: {"status": r.status, "id": r.verification_id} for lvl, r in by_level.items()}
        if changed:
            if "L0" not in by_level:
                reasons.append("code changed but diff review (L0) never ran")
            elif by_level["L0"].status != "passed":
                findings = "; ".join(by_level["L0"].findings[:3]) or "see verification record"
                reasons.append(f"diff review (L0) failed: {findings}")
            if "L1" not in by_level:
                reasons.append("code changed but structural verification (L1) never ran")
            elif by_level["L1"].status != "passed":
                reasons.append("structural verification (L1) did not pass")
            has_tests = bool(self.verifier and self.verifier.project.test_commands)
            if has_tests:
                if "L3" not in by_level:
                    reasons.append("project has tests but they were never run")
                elif by_level["L3"].status != "passed":
                    reasons.append(f"tests (L3) did not pass (status={by_level['L3'].status})")
            if "L5" in by_level and by_level["L5"].status == "failed":
                reasons.append("regression check (L5) found new failures vs baseline")
            if "L5" not in by_level and has_tests:
                warnings.append("regression baseline (L5) was not run")
            baseline_info = getattr(s, "baseline", None) or {}
            if has_tests and baseline_info.get("status") in ("error", "skipped"):
                detail = baseline_info.get("error") or baseline_info.get("reason") or "unavailable"
                warnings.append(f"baseline {baseline_info.get('status')}: {detail}")

        # 9. claims evidence
        mentions_tests = any(
            ("test" in (getattr(c, "statement", "") or "").lower() or "pass" in (getattr(c, "statement", "") or "").lower())
            for c in claims
        )
        if mentions_tests and self.verifier is not None and self.verifier.project.test_commands:
            if "L3" not in by_level:
                reasons.append("claim says tests pass but no test run exists")
            elif by_level["L3"].status != "passed":
                reasons.append("claim says tests pass but the latest test run did not pass")
        resolved: list[dict[str, Any]] = []
        for claim in claims:
            refs = list(getattr(claim, "evidence_refs", []) or [])
            missing = [r for r in refs if not self._ref_exists(r)]
            if not refs:
                reasons.append(f"claim has no evidence: {getattr(claim, 'statement', '')[:80]}")
            elif missing:
                reasons.append(
                    f"claim references unknown evidence: {', '.join(missing[:3])} "
                    "(cite a file path, a ver_* id, an artifact_* id, or the verification command "
                    "instead of a tool name or free prose)"
                )
            text_low = (getattr(claim, "statement", "") or "").lower()
            if ("test" in text_low or "pass" in text_low) and self.verifier and self.verifier.project.test_commands:
                if not any(self._passing_test_ref(ref, by_level.get("L3")) for ref in refs):
                    reasons.append(
                        "claim about tests must cite the passing test verification (ver_* id, "
                        "'L3 tests', or the test command), not just files"
                    )
            resolved.append({"statement": getattr(claim, "statement", ""), "evidence_refs": refs})
        evidence["claims"] = resolved

        # 10. no false success
        for claim in claims:
            text = (getattr(claim, "statement", "") or "").lower()
            if ("test" in text or "pass" in text) and changed:
                if "L3" in by_level and by_level["L3"].status != "passed":
                    reasons.append("claim says tests pass but the latest test run did not pass")
                if "L3" not in by_level and self.verifier and self.verifier.project.test_commands:
                    reasons.append("claim says tests pass but no test run exists")

        if reasons:
            result = CompletionResult.BLOCKED if len(reasons) >= 3 else CompletionResult.PARTIAL
            return GateResult(False, result, reasons, warnings, evidence)
        result = CompletionResult.COMPLETED_WITH_WARNINGS if warnings else CompletionResult.COMPLETED
        return GateResult(True, result, [], warnings, evidence)

    def _git_changed(self) -> list[str]:
        res = self.sandbox.run(["git", "status", "--porcelain=v1"], str(self.workspace), timeout=30)
        if res.exit_code != 0:
            return []
        out = []
        for line in (res.stdout or "").splitlines():
            if len(line) > 3:
                path = line[3:].strip().strip('"')
                if self._is_scratch(path):
                    continue
                out.append(path)
        return out

    def _is_scratch(self, path: str) -> bool:
        """Generated files that are not agent edits: caches, bytecode, harness temp."""
        norm = path.replace("\\", "/").strip("/")
        if norm.endswith((".pyc", ".pyo")):
            return True
        if Path(norm).name.startswith("pytest-cache-files-"):
            return True
        ignored = set(self.config.project.get("ignore_dirs", []))
        if any(segment in ignored for segment in norm.split("/")):
            return True
        return False

    def _ref_exists(self, ref: str) -> bool:
        """Resolve a claim's evidence reference.

        Accepts a file path, artifact id, verification id, verification level+type
        or a verification command — and also prose that *contains* one of those, so
        a model can write "pytest -q exit 0" without the gate becoming decorative:
        the anchor still has to exist in the real event/verification state.
        """
        s = self.state
        raw = str(ref or "").strip()
        if not raw:
            return False
        low = raw.replace("\\", "/").lower()
        state_paths = (
            set(getattr(s, "read_hashes", {}) or {})
            | set(getattr(s, "written_files", {}) or {})
            | set(getattr(s, "deleted_files", []) or [])
        )
        for path in state_paths:
            normalized = str(path).replace("\\", "/").lower()
            if raw == path or (normalized and normalized in low):
                return True
        try:
            if (self.workspace / raw).exists():
                return True
        except OSError:
            pass
        for run in getattr(s, "verification_runs", []):
            if raw in (run.verification_id, run.output_artifact_id):
                return True
            if run.verification_id and run.verification_id.lower() in low:
                return True
            if run.output_artifact_id and run.output_artifact_id.lower() in low:
                return True
            if run.level.lower() in low and run.type.lower() in low:
                return True
            if run.command and any(alias in low for alias in _command_aliases(run.command)):
                return True
        for obs in getattr(s, "recent_observations", []):
            artifact = obs.get("artifact_id")
            if raw == artifact or (artifact and str(artifact).lower() in low):
                return True
        return False

    def _passing_test_ref(self, ref: str, l3: VerificationRun | None) -> bool:
        """A claim about tests must point at a *passing* L3 verification."""
        if l3 is None or l3.status != "passed":
            return False
        raw = str(ref or "").strip().lower()
        if not raw:
            return False
        if l3.verification_id and l3.verification_id.lower() in raw:
            return True
        if l3.output_artifact_id and l3.output_artifact_id.lower() in raw:
            return True
        if l3.level.lower() in raw and l3.type.lower() in raw:
            return True
        if l3.command and any(alias in raw for alias in _command_aliases(l3.command)):
            return True
        return False


def _command_aliases(command: list[str]) -> set[str]:
    """Aliases of a verification command that a claim may quote."""
    if not command:
        return set()
    stem = " ".join([_command_stem(command[0])] + [str(x) for x in command[1:]]).lower()
    tokens = stem.split()
    aliases: set[str] = set()
    for i in range(len(tokens)):
        for j in range(i + 2, min(len(tokens), i + 6) + 1):
            alias = " ".join(tokens[i:j])
            if len(alias.split()) >= 2:
                aliases.add(alias)
    aliases.add(" ".join(t for t in tokens[1:] if t != "-m"))
    return aliases
