# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for scripts/ux_test_session.py.

The script creates a real Cognito user and temporarily widens an app client's
auth flows, so the properties worth pinning are the safety ones: the throwaway
user is unmistakably throwaway, teardown always restores what setup changed, and
the flows file the skill depends on stays parseable and internally consistent.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[4] / "scripts"


def _load_module():
    """Import scripts/ux_test_session.py by path.

    It lives outside any package (scripts/ is not importable as one) and imports
    rbac_common as a sibling, so sys.path has to carry scripts/ itself.
    """
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "ux_test_session", _SCRIPTS / "ux_test_session.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
class TestThrowawayCredentials:
    def test_the_email_can_never_reach_a_real_mailbox(self):
        """RFC 2606 reserves .invalid, so a stray invite cannot be delivered."""
        module = _load_module()
        ctx = {"user_pool": "pool", "region": "us-east-1"}

        with (
            patch.object(module, "resolve_stack", return_value=ctx),
            patch.object(module, "_web_url", return_value="https://example.test/"),
            patch.object(module, "enable_admin_auth"),
            patch.object(module, "create_cognito_user") as create,
        ):
            args = MagicMock(stack="s", group="Admin", region="us-east-1")
            module.cmd_setup(args)

        email = create.call_args[0][1]
        assert email.endswith("@example.invalid")
        assert email.startswith("ux-test-")

    def test_the_password_satisfies_the_pool_policy(self):
        """A rejected password would fail a UX run for a reason unrelated to UX."""
        module = _load_module()
        for _ in range(20):
            password = module._password()
            assert len(password) >= 16
            assert any(c.isupper() for c in password)
            assert any(c.islower() for c in password)
            assert any(c.isdigit() for c in password)
            assert any(not c.isalnum() for c in password)

    def test_passwords_are_not_reused_between_sessions(self):
        module = _load_module()
        assert len({module._password() for _ in range(50)}) == 50


@pytest.mark.unit
class TestTeardownRestores:
    def test_teardown_deletes_the_user_and_restores_auth_flows(self):
        """Setup widens the app client's auth flows; teardown must undo that.

        Leaving ALLOW_ADMIN_USER_PASSWORD_AUTH enabled on a stack is a durable
        weakening of its auth configuration, and it would be invisible.
        """
        module = _load_module()
        ctx = {"user_pool": "pool", "region": "us-east-1"}

        with (
            patch.object(module, "resolve_stack", return_value=ctx),
            patch.object(module, "delete_cognito_user") as delete,
            patch.object(module, "restore_auth_flows") as restore,
        ):
            args = MagicMock(
                stack="s", email="ux-test-1@example.invalid", region="us-east-1"
            )
            rc = module.cmd_teardown(args)

        assert rc == 0
        delete.assert_called_once()
        restore.assert_called_once_with(ctx)

    def test_setup_reports_its_own_teardown_command(self, capsys):
        """The user must not have to reconstruct it — an unrun teardown is the
        failure mode that leaves a known password on the stack."""
        module = _load_module()

        with (
            patch.object(module, "resolve_stack", return_value={}),
            patch.object(module, "_web_url", return_value="https://example.test/"),
            patch.object(module, "enable_admin_auth"),
            patch.object(module, "create_cognito_user"),
        ):
            args = MagicMock(stack="my-stack", group="Admin", region="us-west-2")
            module.cmd_setup(args)

        printed = capsys.readouterr().out
        assert "teardown my-stack" in printed
        assert "--email ux-test-" in printed
        assert "--region us-west-2" in printed


@pytest.mark.unit
class TestWebUrlResolution:
    def test_a_stack_without_a_web_ui_fails_with_an_explanation(self):
        """Rather than handing the agent an empty URL to browse to."""
        module = _load_module()

        with patch.object(module, "aws", return_value=""):
            with pytest.raises(RuntimeError, match="ApplicationWebURL"):
                module._web_url("s", "us-east-1")


@pytest.mark.unit
class TestFlowsFile:
    """The skill reads scripts/ux_flows.yaml, so a malformed file breaks the run."""

    @staticmethod
    def _flows():
        with open(_SCRIPTS / "ux_flows.yaml", encoding="utf-8") as handle:
            return yaml.safe_load(handle)["flows"]

    def test_the_flows_file_parses_and_is_not_empty(self):
        flows = self._flows()
        assert len(flows) >= 5

    def test_every_flow_has_what_the_skill_needs(self):
        for flow in self._flows():
            missing = {"id", "title", "persona", "priority", "steps", "expect"} - set(
                flow
            )
            assert not missing, f"flow {flow.get('id')!r} is missing {sorted(missing)}"
            assert flow["steps"], f"flow {flow['id']} has no steps"
            assert flow["expect"], f"flow {flow['id']} has no pass criteria"

    def test_flow_ids_are_unique(self):
        """Ids are referenced by findings, so a duplicate makes a report ambiguous."""
        ids = [flow["id"] for flow in self._flows()]
        assert len(ids) == len(set(ids))

    def test_personas_are_ones_the_session_helper_can_create(self):
        module = _load_module()
        for flow in self._flows():
            assert flow["persona"] in module.VALID_GROUPS, (
                f"flow {flow['id']} wants persona {flow['persona']!r}, which "
                f"ux_test_session.py cannot create"
            )

    def test_priorities_are_from_the_documented_set(self):
        for flow in self._flows():
            assert flow["priority"] in {"p0", "p1", "p2"}

    def test_at_least_one_p0_flow_covers_classification(self):
        """The flow the customer found broken. If it ever drops out of the
        starter set, that is a regression in what we bother to check."""
        flows = self._flows()
        p0_text = " ".join(
            f"{f['title']} {' '.join(f['steps'])}"
            for f in flows
            if f["priority"] == "p0"
        ).lower()
        assert "class" in p0_text


@pytest.mark.unit
def test_the_skill_documents_teardown_and_the_no_false_pass_rule():
    """Two instructions this skill cannot afford to lose.

    A left-behind user is a security consequence, and a flow reported as passed
    without a browser is worse than no report at all — it is the failure this
    whole layer exists to prevent, reintroduced at the reporting step.
    """
    skill = (
        Path(__file__).resolve().parents[4] / ".claude" / "skills" / "ux-test.md"
    ).read_text(encoding="utf-8")

    assert "teardown" in skill.lower()
    assert "AWS_PROFILE=default" in skill
    assert "without exercising it" in skill or "without having exercised it" in skill


@pytest.mark.unit
def test_the_skill_is_registered_in_claude_md():
    """An unregistered skill is one nobody finds."""
    claude_md = (Path(__file__).resolve().parents[4] / "CLAUDE.md").read_text(
        encoding="utf-8"
    )
    assert ".claude/skills/ux-test.md" in claude_md


@pytest.mark.unit
def test_the_cline_skill_is_a_symlink_not_a_copy():
    """`.claude/skills/` is canonical; a real file here would silently diverge."""
    cline = Path(__file__).resolve().parents[4] / ".cline" / "skills" / "ux-test.md"
    assert cline.is_symlink(), f"{cline} must be a symlink to the .claude skill"
    assert os.path.realpath(cline).endswith(".claude/skills/ux-test.md")
