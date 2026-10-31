"""Layered configuration.

Precedence: builtin defaults < user config (~/.jinlin-agent/config.toml)
< project config (<workspace>/.jinlin/config.toml) < environment overrides.

The project layer may only *tighten* security-relevant settings; attempts to
relax them are recorded as violations and ignored. Repository content is
untrusted input, so this is a security boundary, not a convenience.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .errors import CONFIG_INVALID, AgentError


# --------------------------------------------------------------------------- defaults

DEFAULTS: dict[str, Any] = {
    "model": {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4.1-flash",
        "api_key_env": "DEEPSEEK_API_KEY",
        "timeout_seconds": 180,
        "max_output_tokens": 8192,
        "context_window": 131072,
        "temperature": 0.0,
        "max_retries": 3,
    },
    "runtime": {
        "max_steps_per_turn": 40,
        "max_steps_per_session": 200,
        "max_model_calls": 120,
        "max_compactions": 10,
        "max_consecutive_tool_failures": 5,
        "max_tool_signature_repeats": 3,
        "model_request_timeout_seconds": 180,
        "default_tool_timeout_seconds": 120,
        "max_shell_timeout_seconds": 1800,
        "wall_clock_seconds": 14400,
        "max_total_tokens": 4_000_000,
        "checkpoint_every_steps": 5,
    },
    "workspace": {
        "allow_write": True,
        "allow_delete": False,
        "follow_symlinks": False,
        "max_file_bytes": 1_048_576,
        "max_patch_bytes": 2_000_000,
    },
    "approval": {
        "shell_medium": "ask",
        "shell_high": "ask",
        "shell_destructive": "deny",
        "network": "ask",
        "file_delete": "ask",
        "file_outside_workspace": "deny",
        "write_outside_workspace": "deny",
        "install_dependencies": "ask",
        "git_commit": "ask",
        "git_push": "deny",
    },
    "verification": {
        "require_for_code_changes": True,
        "run_baseline": True,
        "baseline_timeout_seconds": 300,
        "test_command": [],
        "levels": ["L0", "L1", "L2", "L3", "L5"],
        "full_suite_on_final": True,
    },
    "context": {
        "soft_threshold": 0.70,
        "hard_threshold": 0.85,
        "output_reserve_tokens": 8192,
        "max_recent_steps": 8,
        "max_file_tokens": 12000,
        "max_retrieval_tokens": 40000,
        "max_duplicate_search_results": 3,
    },
    "sandbox": {
        "mode": "auto",  # auto | off | require
        "level": "L1",
        "network": "deny_by_policy",
    },
    "storage": {
        "home": "~/.jinlin-agent",
    },
    "project": {
        "instruction_files": [
            "AGENTS.md",
            "CLAUDE.md",
            "CONTEXT.md",
            "README.md",
            "CONTRIBUTING.md",
        ],
        "ignore_dirs": [
            ".git",
            ".hg",
            ".svn",
            "node_modules",
            ".venv",
            "venv",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "dist",
            "build",
            ".idea",
            ".vscode",
        ],
    },
}

# security knobs: how a lower-precedence layer may be altered by a project
_RULES: dict[str, str] = {
    "model.provider": "lock",
    "model.base_url": "lock",
    "model.model": "lock",
    "model.api_key_env": "lock",
    "storage.home": "lock",
    "sandbox.mode": "lock",
    "sandbox.level": "lock",
    "workspace.allow_write": "tighten_false",
    "workspace.allow_delete": "tighten_false",
    "workspace.follow_symlinks": "tighten_false",
    "verification.require_for_code_changes": "tighten_true",
    "verification.run_baseline": "tighten_true",
    "approval.shell_medium": "stricter_rank",
    "approval.shell_high": "stricter_rank",
    "approval.shell_destructive": "stricter_rank",
    "approval.network": "stricter_rank",
    "approval.file_delete": "stricter_rank",
    "approval.file_outside_workspace": "stricter_rank",
    "approval.write_outside_workspace": "stricter_rank",
    "approval.install_dependencies": "stricter_rank",
    "approval.git_commit": "stricter_rank",
    "approval.git_push": "stricter_rank",
    "runtime.max_steps_per_turn": "decrease",
    "runtime.max_steps_per_session": "decrease",
    "runtime.max_model_calls": "decrease",
    "runtime.max_consecutive_tool_failures": "decrease",
    "runtime.wall_clock_seconds": "decrease",
    "runtime.max_total_tokens": "decrease",
}

_APPROVAL_RANK = {"auto": 0, "ask": 1, "deny": 2}


class ConfigViolation(dict):
    pass


@dataclass
class ModelConfig:
    provider: str
    base_url: str
    model: str
    api_key_env: str
    timeout_seconds: int
    max_output_tokens: int
    context_window: int
    temperature: float
    max_retries: int


@dataclass
class Config:
    model: ModelConfig
    runtime: dict[str, Any]
    workspace: dict[str, Any]
    approval: dict[str, Any]
    verification: dict[str, Any]
    context: dict[str, Any]
    sandbox: dict[str, Any]
    storage: dict[str, Any]
    project: dict[str, Any]
    sources: list[str] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ paths

    @property
    def home(self) -> Path:
        return Path(os.path.expanduser(self.storage["home"]))

    @property
    def sessions_dir(self) -> Path:
        return self.home / "sessions"

    def api_key(self) -> str | None:
        return os.environ.get(self.model.api_key_env)

    def effective_hash(self) -> str:
        payload = self.to_dict()
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": asdict(self.model),
            "runtime": self.runtime,
            "workspace": self.workspace,
            "approval": self.approval,
            "verification": self.verification,
            "context": self.context,
            "sandbox": self.sandbox,
            "storage": self.storage,
            "project": self.project,
        }

    # ------------------------------------------------------------------ update

    def tighten(self, override: dict[str, Any], source: str) -> None:
        """Apply one layer; security keys may only tighten."""
        for section, values in override.items():
            if section not in DEFAULTS or not isinstance(values, dict):
                continue
            if section == "model":
                self._tighten_model(values, source)
                continue
            target = getattr(self, section, None)
            if target is None:
                continue
            for key, value in values.items():
                if key not in DEFAULTS[section]:
                    continue  # unknown keys tolerated for forward compatibility
                dotted = f"{section}.{key}"
                rule = _RULES.get(dotted)
                if rule is not None:
                    ok, reason = _check_rule(rule, target[key], value)
                    if not ok:
                        self._record_violation(dotted, source, target[key], value, reason)
                        continue
                target[key] = value

    def _tighten_model(self, values: dict[str, Any], source: str) -> None:
        data = asdict(self.model)
        for key, value in values.items():
            if key not in data:
                continue
            dotted = f"model.{key}"
            rule = _RULES.get(dotted)
            if rule is not None:
                ok, reason = _check_rule(rule, data[key], value)
                if not ok:
                    self._record_violation(dotted, source, data[key], value, reason)
                    continue
            data[key] = value
        self.model = ModelConfig(**data)

    def _record_violation(self, key: str, source: str, current: Any, attempted: Any, reason: str) -> None:
        self.violations.append(
            {"key": key, "source": source, "current": current, "attempted": attempted, "reason": reason}
        )


def _check_rule(rule: str, current: Any, value: Any) -> tuple[bool, str]:
    if rule == "lock":
        return False, "key is locked at the user level"
    if rule == "tighten_false":
        return value is False, "may only be disabled (false)"
    if rule == "tighten_true":
        return value is True, "may only be enabled (true)"
    if rule == "decrease":
        try:
            return float(value) <= float(current), "may only decrease"
        except (TypeError, ValueError):
            return False, "must be numeric"
    if rule == "stricter_rank":
        cur = _APPROVAL_RANK.get(str(current), 1)
        new = _APPROVAL_RANK.get(str(value), 1)
        return new >= cur, "may only become stricter (auto < ask < deny)"
    return True, ""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentError(CONFIG_INVALID, f"cannot read config {path}: {exc}", source="config") from exc


def load_config(workspace: Path | None = None, home: Path | None = None) -> Config:
    home_dir = Path(home) if home else Path(os.path.expanduser(DEFAULTS["storage"]["home"]))

    cfg = Config(
        model=ModelConfig(**DEFAULTS["model"]),
        runtime=dict(DEFAULTS["runtime"]),
        workspace=dict(DEFAULTS["workspace"]),
        approval=dict(DEFAULTS["approval"]),
        verification=dict(DEFAULTS["verification"]),
        context=dict(DEFAULTS["context"]),
        sandbox=dict(DEFAULTS["sandbox"]),
        storage={"home": str(home_dir)},
        project=dict(DEFAULTS["project"]),
    )
    cfg.sources.append("builtin")

    user_path = home_dir / "config.toml"
    if user_path.exists():
        cfg.tighten(_load_toml(user_path), "user")
        cfg.sources.append(str(user_path))

    if workspace is not None:
        proj_path = Path(workspace) / ".jinlin" / "config.toml"
        if proj_path.exists():
            cfg.tighten(_load_toml(proj_path), "project")
            cfg.sources.append(str(proj_path))

    # environment overrides (user level, highest precedence for the endpoint)
    if os.environ.get("JINLIN_MODEL"):
        cfg.model.model = os.environ["JINLIN_MODEL"]
    if os.environ.get("JINLIN_BASE_URL"):
        cfg.model.base_url = os.environ["JINLIN_BASE_URL"]
    if os.environ.get("JINLIN_API_KEY_ENV"):
        cfg.model.api_key_env = os.environ["JINLIN_API_KEY_ENV"]
    if os.environ.get("JINLIN_HOME"):
        cfg.storage["home"] = os.environ["JINLIN_HOME"]

    if not cfg.model.model:
        raise AgentError(CONFIG_INVALID, "model.model must not be empty", source="config")
    return cfg
