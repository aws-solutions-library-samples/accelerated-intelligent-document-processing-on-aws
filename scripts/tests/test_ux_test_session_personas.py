# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The long-lived persona option of scripts/ux_test_session.py.

The unattended MR UX test signs in as ux-persona-<group>@example.invalid, whose
password lives in the masked CI variable UX_TEST_PERSONAS. So unlike every other
user this script makes, that one is meant to last, and the properties worth
pinning are the ones that keep the pool and the variable in step: the option is
only ever taken deliberately, an existing persona's password is never reset
without --rotate, a persona is never deleted by a teardown meant for a throwaway
session, and the --stale sweep can never select one. The default throwaway path
must not change at all, which lib/idp_common_pkg/tests/unit/test_ux_test_session.py
also pins, loading the module the same way this file does.

Nothing here reaches AWS: the module's AWS helpers are replaced per test, and
subprocess.run is refused outright so a missed one fails instead of running the
CLI.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]
_CTX = {
    "stack": "idp-ux-nightly",
    "user_pool": "us-east-1_AbCdEfGhI",
    "region": "us-east-1",
}
_URL = "https://d111111abcdef8.cloudfront.net/"
_NOT_FOUND = (
    "aws cognito-idp admin-get-user failed: An error occurred "
    "(UserNotFoundException) when calling the AdminGetUser operation: "
    "User does not exist."
)


def _load_module() -> ModuleType:
    """Import scripts/ux_test_session.py by path, as the lib test does.

    It lives outside any package and imports rbac_common as a sibling, so
    sys.path has to carry scripts/ itself. A fresh module per test keeps one
    test's replacements out of the next.
    """
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "ux_test_session", _SCRIPTS / "ux_test_session.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError(f"a test reached subprocess.run: {args!r}")

    monkeypatch.setattr(subprocess, "run", refused)


class _Pool:
    """The stack's user pool as the module's AWS helpers see it."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = set(existing or ())
        self.aws_calls: list[tuple[str, ...]] = []
        self.created: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self.resolved: list[tuple[str, str]] = []
        self.attributes: dict[str, dict[str, str]] = {}

    def aws(self, *args: str, region: str | None = None) -> dict | None:
        self.aws_calls.append(args)
        username = args[args.index("--username") + 1]
        if args[:2] == ("cognito-idp", "admin-update-user-attributes"):
            assert username in self.existing, "attributes set on a user not created"
            pairs = json.loads(args[args.index("--user-attributes") + 1])
            named = self.attributes.setdefault(username, {})
            named.update({pair["Name"]: pair["Value"] for pair in pairs})
            return None
        if args[:2] != ("cognito-idp", "admin-get-user"):
            raise AssertionError(f"unexpected aws call {args!r}")
        if username not in self.existing:
            raise RuntimeError(_NOT_FOUND)
        return {"Username": username}

    def create(self, ctx: dict, email: str, group: str, secret: str) -> None:
        self.created.append((email, group, secret))
        self.existing.add(email)

    def delete(self, ctx: dict, email: str) -> None:
        self.deleted.append(email)

    def resolve(self, stack: str, region: str) -> dict:
        self.resolved.append((stack, region))
        return dict(_CTX)


def _module_with(pool: _Pool, monkeypatch) -> ModuleType:
    module = _load_module()
    monkeypatch.setattr(module, "aws", pool.aws)
    monkeypatch.setattr(module, "create_cognito_user", pool.create)
    monkeypatch.setattr(module, "delete_cognito_user", pool.delete)
    monkeypatch.setattr(module, "resolve_stack", pool.resolve)
    monkeypatch.setattr(module, "_web_url", lambda stack, region: _URL)
    return module


def _setup_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "stack": "idp-ux-nightly",
        "group": "Admin",
        "region": "us-east-1",
        "long_lived": True,
        "rotate": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _teardown_args(email: str, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "stack": "idp-ux-nightly",
        "email": email,
        "stale": None,
        "region": "us-east-1",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.unit
def test_the_persona_has_a_fixed_reserved_address_per_group(monkeypatch):
    module = _module_with(_Pool(), monkeypatch)

    assert module.persona_email("Admin") == "ux-persona-admin@example.invalid"
    assert module.persona_email("Reviewer") == "ux-persona-reviewer@example.invalid"
    assert module.UX_PERSONA_PREFIX == "ux-persona-"
    assert not module.UX_PERSONA_PREFIX.startswith(module.UX_TEST_PREFIX)


@pytest.mark.unit
def test_long_lived_setup_creates_the_admin_persona(monkeypatch):
    pool = _Pool()
    module = _module_with(pool, monkeypatch)

    assert module.cmd_setup(_setup_args()) == 0

    assert [(email, group) for email, group, _ in pool.created] == [
        ("ux-persona-admin@example.invalid", "Admin")
    ]
    assert pool.aws_calls[0] == (
        "cognito-idp",
        "admin-get-user",
        "--user-pool-id",
        _CTX["user_pool"],
        "--username",
        "ux-persona-admin@example.invalid",
    )
    assert [call[:2] for call in pool.aws_calls[1:]] == [
        ("cognito-idp", "admin-update-user-attributes")
    ]


@pytest.mark.unit
@pytest.mark.parametrize("rotate", [False, True])
def test_the_persona_gets_the_display_name_the_ui_shows_instead_of_its_address(
    monkeypatch, capsys, rotate
):
    """The UI's top bar falls back to the e-mail claim without given or family name,
    and every recorded page and every snapshot the model reads shows that bar."""
    email = "ux-persona-admin@example.invalid"
    pool = _Pool(existing={email} if rotate else None)
    module = _module_with(pool, monkeypatch)

    assert module.cmd_setup(_setup_args(rotate=rotate)) == 0

    assert pool.attributes == {
        email: {"given_name": "UX", "family_name": "Persona Admin"}
    }
    name = " ".join(pool.attributes[email].values())
    assert "@" not in name and email not in name
    update = next(c for c in pool.aws_calls if "admin-update-user-attributes" in c)
    assert update[update.index("--user-pool-id") + 1] == _CTX["user_pool"]
    assert json.loads(capsys.readouterr().out)["display_name"] == "UX Persona Admin"


@pytest.mark.unit
def test_the_printed_entry_is_what_ux_test_personas_holds(monkeypatch, capsys):
    pool = _Pool()
    module = _module_with(pool, monkeypatch)

    module.cmd_setup(_setup_args(group="Reviewer"))

    printed = json.loads(capsys.readouterr().out)
    ((email, group, secret),) = pool.created
    assert printed["persona"] == {group: {"username": email, "password": secret}}
    assert printed["email"] == email == "ux-persona-reviewer@example.invalid"
    assert printed["password"] == secret
    assert printed["url"] == _URL
    assert (printed["group"], printed["stack"], printed["region"]) == (
        "Reviewer",
        "idp-ux-nightly",
        "us-east-1",
    )
    assert "UX_TEST_PERSONAS" in printed["next"]
    assert "base64" in printed["next"]


@pytest.mark.unit
def test_the_printed_hint_names_the_scope_the_ux_jobs_declare(monkeypatch, capsys):
    """The hint is read at the one moment the operator stores the password, so it
    names the environment scope, as the CI jobs declare it, and says that a copy
    left at the default scope reaches every job."""
    ci = yaml.safe_load((_SCRIPTS.parent / ".gitlab-ci.yml").read_text("utf-8"))
    scope = ci["ai_ux_test"]["environment"]["name"]
    pool = _Pool()
    module = _module_with(pool, monkeypatch)

    module.cmd_setup(_setup_args())

    hint = json.loads(capsys.readouterr().out)["next"]
    assert f"environment scope {scope}" in hint, hint
    assert "masked, not protected" in hint, hint
    assert "default scope, which reaches every job" in hint, hint


@pytest.mark.unit
def test_the_persona_password_satisfies_the_pool_policy(monkeypatch):
    pool = _Pool()
    module = _module_with(pool, monkeypatch)

    module.cmd_setup(_setup_args())

    ((_, _, secret),) = pool.created
    assert len(secret) >= 16
    assert any(c.isupper() for c in secret) and any(c.islower() for c in secret)
    assert any(c.isdigit() for c in secret) and any(not c.isalnum() for c in secret)


@pytest.mark.unit
def test_an_existing_persona_is_refused_without_rotate(monkeypatch):
    pool = _Pool(existing={"ux-persona-admin@example.invalid"})
    module = _module_with(pool, monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        module.cmd_setup(_setup_args())

    message = str(excinfo.value)
    assert "UX_TEST_PERSONAS" in message and "--rotate" in message
    assert pool.created == []


@pytest.mark.unit
def test_an_existing_persona_gets_its_display_name_without_a_new_password(
    monkeypatch,
):
    """One made before the display name existed would keep showing its address in
    every recorded top bar; naming it changes no password."""
    email = "ux-persona-admin@example.invalid"
    pool = _Pool(existing={email})
    module = _module_with(pool, monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        module.cmd_setup(_setup_args())

    assert pool.attributes == {
        email: {"given_name": "UX", "family_name": "Persona Admin"}
    }
    assert pool.created == []
    message = str(excinfo.value)
    assert "'UX Persona Admin'" in message and "password is unchanged" in message


@pytest.mark.unit
def test_an_existing_persona_is_rotated_only_with_rotate(monkeypatch):
    pool = _Pool(existing={"ux-persona-admin@example.invalid"})
    module = _module_with(pool, monkeypatch)

    assert module.cmd_setup(_setup_args(rotate=True)) == 0

    assert [email for email, _, _ in pool.created] == [
        "ux-persona-admin@example.invalid"
    ]


@pytest.mark.unit
def test_a_rotate_flag_that_is_not_literally_true_does_not_rotate(monkeypatch):
    """A MagicMock attribute is truthy; only the parsed flag may reset a password."""
    pool = _Pool(existing={"ux-persona-admin@example.invalid"})
    module = _module_with(pool, monkeypatch)

    with pytest.raises(SystemExit):
        module.cmd_setup(_setup_args(rotate=MagicMock()))

    assert pool.created == []


@pytest.mark.unit
def test_an_annotator_persona_is_refused(monkeypatch):
    pool = _Pool()
    module = _module_with(pool, monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        module.cmd_setup(_setup_args(group="Annotator"))

    assert "UsersTable" in str(excinfo.value)
    assert pool.created == [] and pool.aws_calls == []


@pytest.mark.unit
def test_a_pool_that_cannot_be_asked_stops_setup_rather_than_resetting(monkeypatch):
    """Read as absence, a denial would reset the existing persona's password."""
    pool = _Pool()
    module = _module_with(pool, monkeypatch)

    def denied(*args, region=None):
        raise RuntimeError(
            "aws cognito-idp admin-get-user failed: AccessDeniedException"
        )

    monkeypatch.setattr(module, "aws", denied)

    with pytest.raises(SystemExit) as excinfo:
        module.cmd_setup(_setup_args())

    assert "AccessDeniedException" in str(excinfo.value)
    assert pool.created == []


@pytest.mark.unit
def test_a_magicmock_args_still_takes_the_throwaway_path(monkeypatch, capsys):
    """Every existing caller and test passes a MagicMock, whose attributes are truthy."""
    pool = _Pool()
    module = _module_with(pool, monkeypatch)

    module.cmd_setup(MagicMock(stack="s", group="Admin", region="us-east-1"))

    ((email, group, _),) = pool.created
    assert email.startswith("ux-test-") and email.endswith("@example.invalid")
    assert group == "Admin"
    assert pool.aws_calls == []
    assert "persona" not in json.loads(capsys.readouterr().out)


@pytest.mark.unit
def test_a_namespace_without_the_flag_takes_the_throwaway_path(monkeypatch):
    pool = _Pool()
    module = _module_with(pool, monkeypatch)

    module.cmd_setup(argparse.Namespace(stack="s", group="Admin", region="us-east-1"))

    ((email, _, _),) = pool.created
    assert email.startswith(module.UX_TEST_PREFIX)
    assert pool.aws_calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "email",
    ["ux-persona-admin@example.invalid", "UX-Persona-Admin@example.invalid"],
)
@pytest.mark.parametrize("args_kind", ["namespace", "magicmock"])
def test_teardown_of_a_persona_is_refused_without_long_lived(
    monkeypatch, email, args_kind
):
    pool = _Pool()
    module = _module_with(pool, monkeypatch)
    args = (
        _teardown_args(email)
        if args_kind == "namespace"
        else MagicMock(stack="s", email=email, stale=None, region="us-east-1")
    )

    with pytest.raises(SystemExit) as excinfo:
        module.cmd_teardown(args)

    assert "--long-lived" in str(excinfo.value)
    assert pool.deleted == [] and pool.resolved == []


@pytest.mark.unit
def test_teardown_of_a_persona_with_long_lived_deletes_it(monkeypatch):
    pool = _Pool()
    module = _module_with(pool, monkeypatch)

    rc = module.cmd_teardown(
        _teardown_args("ux-persona-admin@example.invalid", long_lived=True)
    )

    assert rc == 0
    assert pool.deleted == ["ux-persona-admin@example.invalid"]


@pytest.mark.unit
def test_teardown_of_a_throwaway_user_needs_no_flag(monkeypatch):
    pool = _Pool()
    module = _module_with(pool, monkeypatch)

    assert module.cmd_teardown(_teardown_args("ux-test-1a2b3c4d@example.invalid")) == 0

    assert pool.deleted == ["ux-test-1a2b3c4d@example.invalid"]


@pytest.mark.unit
def test_the_stale_sweep_never_selects_a_persona(monkeypatch):
    module = _load_module()
    users = {
        "Users": [
            {
                "Username": "ux-persona-admin@example.invalid",
                "UserCreateDate": "2020-01-01T00:00:00Z",
            },
            {
                "Username": "ux-persona-reviewer@example.invalid",
                "UserCreateDate": "2020-01-01T00:00:00Z",
            },
            {
                "Username": "ux-test-abcd1234@example.invalid",
                "UserCreateDate": "2020-01-01T00:00:00Z",
            },
        ]
    }
    monkeypatch.setattr(module, "aws", lambda *args, region=None: users)

    stale = module._stale_ux_users(_CTX, older_than_hours=1)

    assert stale == ["ux-test-abcd1234@example.invalid"]


def _parsed(module: ModuleType, monkeypatch, argv: list[str]) -> argparse.Namespace:
    seen: list[argparse.Namespace] = []

    def record(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(module, "cmd_setup", record)
    monkeypatch.setattr(module, "cmd_teardown", record)
    monkeypatch.setattr(sys, "argv", ["ux_test_session.py", *argv])
    assert module.main() == 0
    (args,) = seen
    return args


@pytest.mark.unit
def test_the_command_line_offers_long_lived_and_rotate_on_setup(monkeypatch):
    module = _load_module()

    args = _parsed(
        module, monkeypatch, ["setup", "idp-ux-nightly", "--long-lived", "--rotate"]
    )

    assert (args.long_lived, args.rotate, args.group) == (True, True, "Admin")
    default = _parsed(_load_module(), monkeypatch, ["setup", "idp-ux-nightly"])
    assert (default.long_lived, default.rotate) == (False, False)


@pytest.mark.unit
def test_the_command_line_offers_long_lived_on_teardown(monkeypatch):
    module = _load_module()

    args = _parsed(
        module,
        monkeypatch,
        [
            "teardown",
            "idp-ux-nightly",
            "--email",
            "ux-persona-admin@example.invalid",
            "--long-lived",
        ],
    )

    assert args.long_lived is True
    default = _parsed(
        _load_module(),
        monkeypatch,
        ["teardown", "idp-ux-nightly", "--email", "ux-test-1@example.invalid"],
    )
    assert default.long_lived is False
