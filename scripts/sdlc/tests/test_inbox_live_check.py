# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for scripts/inbox_live_check.py (``make stacktest-inbox``).

Nothing here reaches AWS. Cognito, CloudFormation and the REST API are fakes
injected through ``Deps``; the producer and reader sides both run the REAL
``idp_common.notifications`` service on a moto DynamoDB table, so the posting
checks exercise the real de-duplication and idempotency code. The fake API keys
every request by the caller's token, the way the deployed resolver does, and
each knob breaks one property the live check exists to catch.
"""

import importlib.util
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.unit

REGION = "us-east-1"
TABLE = "inbox-live-check-test"
STACK = "IDP1"


def _load():
    path = Path(__file__).resolve().parents[2] / "inbox_live_check.py"
    spec = importlib.util.spec_from_file_location("inbox_live_check", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["inbox_live_check"] = mod
    spec.loader.exec_module(mod)
    return mod


live = _load()

from idp_common.notifications.exceptions import (  # noqa: E402
    NotificationNotFoundError,
)
from idp_common.notifications.keys import normalize_recipient_key  # noqa: E402
from idp_common.notifications.models import NotificationSettings  # noqa: E402
from idp_common.notifications.service import NotificationService  # noqa: E402
from idp_common.notifications.store import NotificationStore  # noqa: E402


def _create_table():
    boto3.resource("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "UnreadPK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "UnreadIndex",
                "KeySchema": [
                    {"AttributeName": "UnreadPK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["ExpiresAfter", "Type", "Severity"],
                },
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _all_items():
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    return table.scan()["Items"]


class FakeStack:
    """Cognito, CloudFormation and the REST API for one fake deployment."""

    def __init__(self, *, has_inbox=True, **knobs):
        self.has_inbox = has_inbox
        self.knobs = knobs
        self.groups = {}  # group -> [emails]
        self.users = {}  # email -> app group
        self.events = []
        self.auth_enabled = False
        self.reader = None

    # -- Deps -------------------------------------------------------------
    def deps(self):
        return live.Deps(
            resolve_stack=self.resolve_stack,
            aws=self.aws,
            enable_admin_auth=self.enable_admin_auth,
            restore_auth_flows=self.restore_auth_flows,
            create_cognito_user=self.create_cognito_user,
            delete_cognito_user=self.delete_cognito_user,
            get_id_token=self.get_id_token,
            call_body=self.call_body,
            service_factory=self.service_factory,
            sleep=lambda _s: None,
            rand=lambda: "abcd1234",
        )

    def resolve_stack(self, stack, region):
        return {
            "stack": stack,
            "region": region,
            "user_pool": "us-east-1_pool",
            "client": "client",
            "api_base": "https://api.example.invalid/api",
        }

    def aws(self, *args, region=None):
        self.events.append(args[:2])
        if args[:2] == ("cloudformation", "describe-stacks"):
            return TABLE if self.has_inbox else ""
        if args[:2] == ("cognito-idp", "create-group"):
            self.groups[args[args.index("--group-name") + 1]] = []
        elif args[:2] == ("cognito-idp", "delete-group"):
            self.groups.pop(args[args.index("--group-name") + 1], None)
        elif args[:2] == ("cognito-idp", "admin-add-user-to-group"):
            group = args[args.index("--group-name") + 1]
            self.groups[group].append(args[args.index("--username") + 1])
        return None

    def enable_admin_auth(self, ctx):
        self.auth_enabled = True

    def restore_auth_flows(self, ctx):
        self.auth_enabled = False
        self.events.append(("restore", "auth"))

    def create_cognito_user(self, ctx, email, group, password):
        if self.knobs.get("fail_second_user") and self.users:
            raise RuntimeError("admin-create-user failed")
        self.users[email] = group

    def delete_cognito_user(self, ctx, email):
        self.users.pop(email, None)

    def get_id_token(self, ctx, email, password):
        return f"token:{email}:" + "x" * 120

    def service_factory(self, table, user_pool_id, region):
        def lister(pool, group):
            return [
                {"Username": e, "Attributes": [{"Name": "email", "Value": e}]}
                for e in self.groups.get(group, [])
            ]

        svc = NotificationService(
            store=NotificationStore(table_name=table, region=region),
            user_pool_id=user_pool_id,
            region=region,
            settings_provider=NotificationSettings,
            group_lister=lister,
        )
        self.reader = svc
        return svc

    # -- the REST API -----------------------------------------------------
    def call_body(self, api_base, field, args, token):
        override = self.knobs.get("status", {}).get(field)
        if override is not None:
            return override
        email = token.split(":")[1]
        rk = normalize_recipient_key(email)
        is_b = email.startswith("inbox-live-b-")
        if is_b and self.knobs.get("b_reads_as_a"):
            rk = normalize_recipient_key(email.replace("-b-", "-a-"))
        svc = self.reader
        if field == "listInboxMessages":
            page = svc.list_messages(rk, limit=args.get("limit", 20))
            return 200, json.dumps(page.model_dump(by_alias=True, mode="json"))
        if field == "getInboxUnreadCount":
            return 200, json.dumps(svc.unread_count(rk))
        if field == "markInboxMessages":
            try:
                res = svc.mark(rk, args["ids"], read=args["read"])
            except NotificationNotFoundError:
                return 404, '{"errors":[{"errorType":"ResourceNotFound"}]}'
            return 200, json.dumps(res.model_dump(by_alias=True, mode="json"))
        return 400, '{"errors":[{"errorType":"BadRequest"}]}'


@pytest.fixture
def aws_env(monkeypatch):
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        _create_table()
        yield


def _run(stack):
    return live.main(["--stack-name", STACK, "--region", REGION], deps=stack.deps())


def _outcomes(capsys):
    out = capsys.readouterr().out
    return out, [
        line.split("]")[0].split("[")[1].strip()
        for line in out.splitlines()
        if line.startswith("  [")
    ]


class TestArguments:
    def test_stack_name_is_required(self):
        with pytest.raises(SystemExit):
            live.parse_args(["--region", REGION])

    def test_region_falls_back_to_the_environment(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")

        args = live.parse_args(["--stack-name", STACK])

        assert args.region == "eu-west-1"

    def test_no_region_anywhere_is_an_error(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        with pytest.raises(SystemExit):
            live.parse_args(["--stack-name", STACK])


class TestExitCodes:
    @pytest.mark.parametrize(
        "outcomes, code",
        [
            (["PASS", "PASS"], 0),
            (["PASS", "FAIL"], 1),
            (["FAIL", "ERROR", "SKIP"], 1),
            (["PASS", "ERROR"], 2),
            (["PASS", "SKIP"], 2),
            (["SKIP"], 2),
            ([], 2),
        ],
    )
    def test_each_outcome_maps_to_its_own_exit_code(self, outcomes, code):
        assert live.exit_code(outcomes) == code

    def test_the_three_exit_codes_are_distinct(self):
        assert len({live.EXIT_PASS, live.EXIT_FAIL, live.EXIT_INCOMPLETE}) == 3


class TestLiveCheckOffline:
    def test_a_correct_deployment_passes_every_check(self, aws_env, capsys):
        stack = FakeStack()

        code = _run(stack)

        out, outcomes = _outcomes(capsys)
        assert code == 0, out
        assert outcomes and set(outcomes) == {"PASS"}
        assert "RESULT: PASS (exit 0)" in out

    def test_cleanup_removes_users_group_items_type_and_auth(self, aws_env):
        stack = FakeStack()

        _run(stack)

        assert stack.users == {}
        assert stack.groups == {}
        assert stack.auth_enabled is False
        assert _all_items() == []
        from idp_common.notifications.catalogue import get_type

        assert get_type(live.PROBE_TYPE_ID) is None

    def test_the_temporary_group_holds_only_user_a(self, aws_env):
        stack = FakeStack()
        seen = {}
        original = stack.aws

        def spy(*args, region=None):
            result = original(*args, region=region)
            if args[:2] == ("cognito-idp", "admin-add-user-to-group"):
                seen.update({k: list(v) for k, v in stack.groups.items()})
            return result

        stack.aws = spy

        _run(stack)

        assert seen == {
            "inbox-live-check-abcd1234": ["inbox-live-a-abcd1234@example.invalid"]
        }

    def test_a_stack_without_the_inbox_is_a_skip_and_touches_nothing(
        self, aws_env, capsys
    ):
        stack = FakeStack(has_inbox=False)

        code = _run(stack)

        out, outcomes = _outcomes(capsys)
        assert code == 2
        assert outcomes == ["SKIP"]
        assert "RESULT: INCOMPLETE" in out
        assert stack.users == {} and stack.groups == {}
        assert ("cognito-idp", "create-group") not in stack.events

    def test_a_user_b_who_can_read_user_a_is_a_fail(self, aws_env, capsys):
        stack = FakeStack(b_reads_as_a=True)

        code = _run(stack)

        out, outcomes = _outcomes(capsys)
        assert code == 1, out
        assert "FAIL" in outcomes
        assert "[FAIL ] api-user-b-sees-nothing-of-user-a" in out
        assert "[FAIL ] api-user-b-mark-of-user-a-id-is-404" in out
        assert _all_items() == [], "cleanup must run after a failure"
        assert stack.users == {} and stack.auth_enabled is False

    def test_an_api_that_cannot_be_reached_is_an_error_not_a_pass(
        self, aws_env, capsys
    ):
        stack = FakeStack(status={"listInboxMessages": (0, "<request error: x>")})

        code = _run(stack)

        out, outcomes = _outcomes(capsys)
        assert code == 2, out
        assert "ERROR" in outcomes and "FAIL" not in outcomes
        assert _all_items() == []

    def test_a_server_error_from_the_api_is_an_error(self, aws_env, capsys):
        stack = FakeStack(status={"getInboxUnreadCount": (502, "Bad Gateway")})

        code = _run(stack)

        _out, outcomes = _outcomes(capsys)
        assert code == 2
        assert outcomes[-1] == "ERROR"

    def test_an_unread_count_that_never_matches_is_a_fail(self, aws_env, capsys):
        stack = FakeStack(status={"getInboxUnreadCount": (200, "99")})

        code = _run(stack)

        out, _ = _outcomes(capsys)
        assert code == 1
        assert "[FAIL ] api-unread-count-matches" in out

    def test_a_setup_failure_still_cleans_up_what_was_created(self, aws_env, capsys):
        stack = FakeStack(fail_second_user=True)

        code = _run(stack)

        _out, outcomes = _outcomes(capsys)
        assert code == 2
        assert outcomes == ["ERROR"]
        assert stack.users == {}
        assert stack.groups == {}
        assert stack.auth_enabled is False
        assert ("restore", "auth") in stack.events

    def test_cleanup_continues_when_one_step_fails(self, aws_env, capsys):
        stack = FakeStack()

        def boom(ctx, email):
            raise RuntimeError("admin-delete-user failed")

        stack.delete_cognito_user = boom

        _run(stack)

        out, _ = _outcomes(capsys)
        assert "cleanup of users failed" in out
        assert stack.groups == {}
        assert stack.auth_enabled is False
        assert _all_items() == []
