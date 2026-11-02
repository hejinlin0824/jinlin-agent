from __future__ import annotations

import pytest

from jinlin_agent.task_contract import Capability, TaskContract, TaskKind, VerificationProfile, plan_verification


def test_legacy_contract_preserves_existing_modes():
    readonly = TaskContract.legacy(read_only=True)
    writable = TaskContract.legacy(read_only=False)
    assert readonly.read_only and readonly.allows(Capability.READ)
    assert readonly.allows(Capability.USER_INPUT)
    assert not readonly.allows(Capability.WRITE)
    assert not readonly.allows(Capability.EXECUTE)
    assert not writable.read_only and writable.allows(Capability.WRITE)
    assert writable.allows(Capability.EXECUTE)


def test_contract_rejects_invalid_capability_combinations():
    with pytest.raises(ValueError, match="delete capability requires write"):
        TaskContract(TaskKind.IMPLEMENTATION, frozenset({Capability.READ, Capability.DELETE}), VerificationProfile.CODE)
    with pytest.raises(ValueError, match="network capability requires execute"):
        TaskContract(TaskKind.IMPLEMENTATION, frozenset({Capability.READ, Capability.NETWORK}), VerificationProfile.CODE)


def test_contract_round_trip():
    original = TaskContract.legacy(read_only=False)
    assert TaskContract.from_dict(original.to_dict()) == original


def test_documentation_diff_skips_unrelated_tests():
    plan = plan_verification(["README.md", "docs/guide.rst"], has_test_command=True, has_static_command=True)
    assert plan.profile is VerificationProfile.DOCUMENTATION
    assert plan.required_levels == ("L0", "L1")
    assert plan.optional_levels == ()


def test_code_diff_requires_project_tests():
    plan = plan_verification(["src/module.py"], has_test_command=True, has_static_command=True)
    assert plan.profile is VerificationProfile.CODE
    assert plan.required_levels == ("L0", "L1", "L3")
    assert plan.optional_levels == ("L2", "L5")


def test_test_only_diff_runs_tests_without_static_analysis():
    plan = plan_verification(["tests/test_module.py"], has_test_command=True, has_static_command=True)
    assert plan.profile is VerificationProfile.TESTS
    assert plan.required_levels == ("L0", "L1", "L3")
    assert plan.optional_levels == ("L5",)


def test_configuration_diff_selects_applicable_optional_checks():
    plan = plan_verification(["pyproject.toml"], has_test_command=True, has_static_command=True)
    assert plan.profile is VerificationProfile.CONFIGURATION
    assert plan.required_levels == ("L0", "L1")
    assert plan.optional_levels == ("L2", "L3")


def test_test_claim_without_diff_still_requires_test_evidence():
    plan = plan_verification([], has_test_command=True, has_static_command=False, mentions_tests=True)
    assert plan.profile is VerificationProfile.TESTS
    assert plan.required_levels == ("L3",)


def test_test_claim_promotes_l3_to_required_for_docs():
    plan = plan_verification(["docs/verification.md"], has_test_command=True, has_static_command=False, mentions_tests=True)
    assert "L3" in plan.required_levels


def test_policy_uses_contract_capabilities_not_prompt_mode(tmp_path):
    from types import SimpleNamespace
    from jinlin_agent.domain import SideEffect
    from jinlin_agent.policy import PolicyEngine

    config = SimpleNamespace(
        workspace={"follow_symlinks": False, "allow_write": True, "allow_delete": False},
        approval={},
    )
    contract = TaskContract.legacy(read_only=True)
    policy = PolicyEngine(config, tmp_path, task_contract=contract)

    denied = policy.evaluate("run_command", SideEffect.EXECUTE.value, {"command": ["pytest", "-q"]})
    assert not denied.allowed
    assert denied.error.code == "COMMAND_DENIED"
    assert policy.evaluate("read_file", SideEffect.READ.value, {"path": "README.md"}).allowed
