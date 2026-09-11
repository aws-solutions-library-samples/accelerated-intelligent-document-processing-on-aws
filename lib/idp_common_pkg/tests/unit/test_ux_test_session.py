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
from datetime import datetime, timedelta, timezone
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
class TestTheAppClientIsNeverWidened:
    """The session must not modify the UI app client's auth flows at all.

    It used to enable ALLOW_ADMIN_USER_PASSWORD_AUTH for the duration of the
    session, on the theory that setting a known password non-interactively needed
    it. It does not: admin-set-user-password is an admin API on the user pool and
    ignores the client's ExplicitAuthFlows, and the browser then signs in over SRP,
    which the UI client already allows. That flow is required only by
    admin-initiate-auth, which this script never calls.

    It mattered more here than in test_api_rbac.py, which reverts in a `finally`
    even under --no-teardown: this is two separate invocations with an open-ended
    agent session in between, guarded only by a printed reminder.
    """

    def test_setup_does_not_touch_the_app_client(self):
        module = _load_module()
        ctx = {"user_pool": "pool", "region": "us-east-1"}

        with (
            patch.object(module, "resolve_stack", return_value=ctx),
            patch.object(module, "_web_url", return_value="https://example.test/"),
            patch.object(module, "create_cognito_user"),
        ):
            args = MagicMock(stack="s", group="Admin", region="us-east-1")
            module.cmd_setup(args)

        # Not merely unused — not even imported, so it cannot be reintroduced by a
        # stray call without this failing.
        assert not hasattr(module, "enable_admin_auth")
        assert not hasattr(module, "restore_auth_flows")

    def test_teardown_deletes_the_user_and_restores_nothing(self):
        module = _load_module()
        ctx = {"user_pool": "pool", "region": "us-east-1"}

        with (
            patch.object(module, "resolve_stack", return_value=ctx),
            patch.object(module, "delete_cognito_user") as delete,
        ):
            # stale=None explicitly: MagicMock auto-creates a truthy attribute
            # otherwise, which would send this down the sweep branch instead.
            args = MagicMock(
                stack="s",
                email="ux-test-1@example.invalid",
                stale=None,
                region="us-east-1",
            )
            rc = module.cmd_teardown(args)

        assert rc == 0
        delete.assert_called_once()

    def test_teardown_still_needs_an_email_when_not_sweeping(self):
        # --email stopped being argparse-required once --stale existed, so the
        # either-or has to be enforced in the command itself.
        module = _load_module()
        with patch.object(module, "resolve_stack", return_value={}):
            args = MagicMock(stack="s", email=None, stale=None, region="us-east-1")
            with pytest.raises(SystemExit, match="--email"):
                module.cmd_teardown(args)


class TestStaleSweep:
    """Sweeping abandoned sessions, and never anything else.

    setup and teardown are separate invocations with an open-ended session between
    them, so an abandoned run leaves a real Cognito account — permanent password,
    Admin by default — in a live pool with nothing to expire it. The sweep closes
    that, which means it deletes users; the property worth pinning is therefore what
    it refuses to touch.
    """

    @staticmethod
    def _ctx():
        return {"user_pool": "pool", "region": "us-east-1"}

    def test_only_ux_test_users_on_the_reserved_domain_are_selected(self):
        module = _load_module()
        users = {
            "Users": [
                # Real operators, in every shape that might fool a prefix-only match.
                {
                    "Username": "operator@example.com",
                    "UserCreateDate": "2020-01-01T00:00:00Z",
                },
                {
                    "Username": "ux-test-lead@example.com",
                    "UserCreateDate": "2020-01-01T00:00:00Z",
                },
                {
                    "Username": "admin@example.invalid",
                    "UserCreateDate": "2020-01-01T00:00:00Z",
                },
                # A genuine abandoned session.
                {
                    "Username": "ux-test-abcd1234@example.invalid",
                    "UserCreateDate": "2020-01-01T00:00:00Z",
                },
            ]
        }
        with patch.object(module, "aws", return_value=users):
            stale = module._stale_ux_users(self._ctx(), older_than_hours=1)

        assert stale == ["ux-test-abcd1234@example.invalid"]

    def test_a_session_younger_than_the_cutoff_is_left_alone(self):
        # Otherwise a sweep run during someone else's review would delete the account
        # out from under them.
        module = _load_module()
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        users = {
            "Users": [
                {
                    "Username": "ux-test-inuse01@example.invalid",
                    "UserCreateDate": recent.isoformat().replace("+00:00", "Z"),
                }
            ]
        }
        with patch.object(module, "aws", return_value=users):
            assert module._stale_ux_users(self._ctx(), older_than_hours=12) == []

    def test_sweeping_deletes_every_stale_user_it_found(self):
        module = _load_module()
        stale = ["ux-test-a@example.invalid", "ux-test-b@example.invalid"]
        with (
            patch.object(module, "resolve_stack", return_value=self._ctx()),
            patch.object(module, "_stale_ux_users", return_value=stale),
            patch.object(module, "delete_cognito_user") as delete,
        ):
            args = MagicMock(stack="s", email=None, stale=12.0, region="us-east-1")
            rc = module.cmd_teardown(args)

        assert rc == 0
        assert [c.args[1] for c in delete.call_args_list] == stale

    def test_setup_reports_its_own_teardown_command(self, capsys):
        """The user must not have to reconstruct it — an unrun teardown is the
        failure mode that leaves a known password on the stack."""
        module = _load_module()

        with (
            patch.object(module, "resolve_stack", return_value={}),
            patch.object(module, "_web_url", return_value="https://example.test/"),
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
class TestWrongStackOrRegion:
    """A wrong --region is the likeliest way to mis-invoke this, and the hardest
    to spot: the stack genuinely does not exist where you looked. The default was
    a nine-frame traceback ending in a botocore ValidationError — exactly the
    "does the error say what to do?" failure this harness exists to catch in the
    product."""

    def test_a_missing_stack_names_the_region_and_suggests_alternatives(self):
        module = _load_module()

        with (
            patch.object(
                module,
                "resolve_stack",
                side_effect=RuntimeError("Stack with id X does not exist"),
            ),
            patch.object(
                module,
                "_candidate_stacks",
                return_value=["IDP-dev-stack4", "IDP-other"],
            ),
        ):
            with pytest.raises(SystemExit) as excinfo:
                module._resolve_or_explain("IDP-typo", "us-east-1")

        message = str(excinfo.value)
        assert "us-east-1" in message
        assert "--region" in message
        assert "IDP-dev-stack4" in message
        # Not a traceback.
        assert "Traceback" not in message

    def test_no_candidates_points_at_region_and_profile(self):
        """The other real cause: credentials for a different account."""
        module = _load_module()

        with (
            patch.object(
                module,
                "resolve_stack",
                side_effect=RuntimeError("Stack with id X does not exist"),
            ),
            patch.object(module, "_candidate_stacks", return_value=[]),
        ):
            with pytest.raises(SystemExit) as excinfo:
                module._resolve_or_explain("IDP-typo", "eu-west-1")

        assert "AWS_PROFILE" in str(excinfo.value)

    def test_an_unrelated_failure_is_not_swallowed(self):
        """Only the not-found case gets the friendly treatment; a permissions or
        network failure must still surface as itself."""
        module = _load_module()

        with patch.object(
            module, "resolve_stack", side_effect=RuntimeError("AccessDenied")
        ):
            with pytest.raises(RuntimeError, match="AccessDenied"):
                module._resolve_or_explain("s", "us-east-1")

    def test_suggestions_never_break_the_error_message(self):
        """If listing stacks also fails, the primary error still gets reported."""
        module = _load_module()

        with patch.object(module, "aws", side_effect=RuntimeError("boom")):
            assert module._candidate_stacks("us-east-1") == []


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
        """The flow that shipped broken for several versions. If it ever drops out of the
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

    lowered = skill.lower()
    assert "teardown" in lowered
    assert "AWS_PROFILE=default" in skill
    # Asserted as a rule rather than a phrase, so rewording the skill does not
    # fail this for no reason — but dropping the rule does.
    assert "did not load" in lowered or "didn't load" in lowered, (
        "the skill must still forbid reporting on a screen that was never loaded"
    )
    # And the reason the whole layer exists: a real browser, driven.
    assert "screenshot" in lowered


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
