"""Declarative task contracts and diff-aware verification planning.

Contracts are supplied by the caller, never inferred from prompt wording.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


class TaskKind(str, Enum):
    QUESTION = "question"
    ANALYSIS = "analysis"
    DOCUMENTATION = "documentation"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    MIXED = "mixed"


class Capability(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    DELETE = "delete"
    USER_INPUT = "user_input"
    APPROVAL = "approval"
    WORKFLOW = "workflow"


class VerificationProfile(str, Enum):
    NONE = "none"
    READ_ONLY = "read_only"
    DOCUMENTATION = "documentation"
    CONFIGURATION = "configuration"
    CODE = "code"
    TESTS = "tests"


@dataclass(frozen=True)
class TaskContract:
    kind: TaskKind
    capabilities: frozenset[Capability]
    verification_profile: VerificationProfile
    workspace_scope: str = "pinned_workspace"
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError(f"unsupported task contract version: {self.version}")
        if self.workspace_scope != "pinned_workspace":
            raise ValueError("only pinned_workspace scope is currently supported")
        if Capability.DELETE in self.capabilities and Capability.WRITE not in self.capabilities:
            raise ValueError("delete capability requires write capability")
        if Capability.NETWORK in self.capabilities and Capability.EXECUTE not in self.capabilities:
            raise ValueError("network capability requires execute capability")

    @classmethod
    def legacy(cls, *, read_only: bool) -> "TaskContract":
        if read_only:
            return cls(TaskKind.ANALYSIS, frozenset({Capability.READ, Capability.USER_INPUT}), VerificationProfile.READ_ONLY)
        return cls(
            TaskKind.IMPLEMENTATION,
            frozenset({
                Capability.READ, Capability.WRITE, Capability.EXECUTE,
                Capability.USER_INPUT, Capability.APPROVAL, Capability.WORKFLOW,
            }),
            VerificationProfile.CODE,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskContract":
        if not isinstance(value, dict):
            raise ValueError("task contract must be an object")
        allowed = {"kind", "capabilities", "verification_profile", "workspace_scope", "version"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError("unknown task contract fields: " + ", ".join(sorted(unknown)))
        caps = value.get("capabilities")
        if not isinstance(caps, (list, tuple, set, frozenset)):
            raise ValueError("task contract capabilities must be a list")
        return cls(
            kind=TaskKind(value["kind"]),
            capabilities=frozenset(Capability(item) for item in caps),
            verification_profile=VerificationProfile(value["verification_profile"]),
            workspace_scope=str(value.get("workspace_scope", "pinned_workspace")),
            version=int(value.get("version", 1)),
        )

    def allows(self, capability: Capability | str) -> bool:
        return Capability(capability) in self.capabilities

    @property
    def read_only(self) -> bool:
        return not self.allows(Capability.WRITE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "kind": self.kind.value,
            "capabilities": sorted(item.value for item in self.capabilities),
            "verification_profile": self.verification_profile.value,
            "workspace_scope": self.workspace_scope,
        }


@dataclass(frozen=True)
class VerificationPlan:
    profile: VerificationProfile
    changed_files: tuple[str, ...]
    required_levels: tuple[str, ...]
    optional_levels: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.value,
            "changed_files": list(self.changed_files),
            "required_levels": list(self.required_levels),
            "optional_levels": list(self.optional_levels),
            "reason": self.reason,
        }


_CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt",
    ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".scala", ".sh", ".ps1",
}
_DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
_CONFIG_NAMES = {
    "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "package.json",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "cargo.toml", "go.mod",
    "go.sum", "requirements.txt", "poetry.lock", "uv.lock",
}
_TEST_MARKERS = ("test_", "_test.", ".spec.", ".test.")


def _suffix(name: str) -> str:
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def plan_verification(
    changed_files: Iterable[str],
    *,
    has_test_command: bool,
    has_static_command: bool,
    mentions_tests: bool = False,
) -> VerificationPlan:
    """Choose checks from the observed diff and available project tooling."""
    files = tuple(sorted({str(p).replace("\\", "/") for p in changed_files}))
    if not files:
        if mentions_tests and has_test_command:
            return VerificationPlan(VerificationProfile.TESTS, files, ("L3",), (), "An explicit test-pass claim requires a real test run.")
        return VerificationPlan(VerificationProfile.READ_ONLY, files, (), (), "No workspace changes were recorded.")

    names = [p.rsplit("/", 1)[-1].lower() for p in files]
    tests_changed = any(any(marker in n for marker in _TEST_MARKERS) for n in names)
    code = any(_suffix(n) in _CODE_SUFFIXES and not any(marker in n for marker in _TEST_MARKERS) for n in names)
    config = any(n in _CONFIG_NAMES for n in names)
    docs = all(_suffix(n) in _DOC_SUFFIXES or n in {"readme", "license", "changelog"} for n in names)

    if docs:
        profile = VerificationProfile.DOCUMENTATION
        required, optional = ["L0", "L1"], []
        reason = "Documentation-only diff: inspect the diff and structure; unrelated code tests are not required."
    elif tests_changed and not code:
        profile = VerificationProfile.TESTS
        required = ["L0", "L1"] + (["L3"] if has_test_command else [])
        optional = ["L5"] if has_test_command else []
        reason = "Test-only diff: run the project test command; compare with baseline when available."
    elif config and not code:
        profile = VerificationProfile.CONFIGURATION
        required = ["L0", "L1"]
        optional = (["L2"] if has_static_command else []) + (["L3"] if has_test_command else [])
        reason = "Configuration diff: validate structure and run configured static/test checks when applicable."
    else:
        profile = VerificationProfile.CODE
        required = ["L0", "L1"] + (["L3"] if has_test_command else [])
        optional = (["L2"] if has_static_command else []) + (["L5"] if has_test_command else [])
        reason = "Code or mixed diff: require diff/structure checks and project tests when available."

    if mentions_tests and has_test_command and "L3" not in required:
        required.append("L3")
        reason += " An explicit test-pass claim makes L3 mandatory."
    return VerificationPlan(profile, files, tuple(required), tuple(optional), reason)
