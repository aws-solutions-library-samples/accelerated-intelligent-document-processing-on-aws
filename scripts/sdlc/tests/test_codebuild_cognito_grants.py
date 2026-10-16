"""The CodeBuild role's Cognito grant must cover every call the RBAC harness makes.

The defect this exists to prevent has now shipped three times, each time the same
shape: the harness (``scripts/test_api_rbac.py`` + ``scripts/rbac_common.py``)
grows a Cognito call, and the matching action is not added to
``CodeBuildCognitoRBACTest`` in ``scripts/sdlc/cfn/codepipeline-s3.yml``. Nothing
offline notices, because the only thing that can tell is an AccessDenied from a
live pool — so the first report is a red nightly pipeline the morning after merge.

* ``AdminUserGlobalSignOut`` and ``AdminGetUser`` (commit ``a6b21363``): the
  logout-revocation check and the per-user scope row could not run.
* ``AdminListGroupsForUser`` (this file's occasion): ``_verify_group_memberships``
  runs during ``setup_users``, so the gap took out **the whole of Step 12** rather
  than one check — the suite aborted before the matrix ran at all.

So the required set is **derived from the harness source** rather than listed
here, and the assertion runs in **both** directions:

* a call with no grant fails — the regression above;
* a grant no call makes fails — a dead permission on a CI role that is already
  close to admin, and the thing that makes the first direction trustworthy. A
  grant kept "just in case" is how the derived set stops meaning anything.

⚠️ **The required set is the harness's *import closure*, not one file.** The
calls live in two modules today and ``api_security_cases`` is in the closure with
none; all three are read, so a call added to any of them is covered without an
edit here. ``test_every_module_in_the_harness_closure_is_read`` fails if the
closure grows a fourth.

What this does **not** check is whether the grant is *deployed*. The policy is
part of ``genaiic-sdlc-codepipeline``, a stack installed by hand (see
``scripts/sdlc/cfn/README.md``), so a template this gate passes still has to be
pushed to the CI account before the nightly can use it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

# scripts/sdlc/tests/<this file> -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

PIPELINE_TEMPLATE = REPO_ROOT / "scripts" / "sdlc" / "cfn" / "codepipeline-s3.yml"
POLICY_NAME = "CodeBuildCognitoRBACTest"

# Every module Step 12 loads. `test_api_rbac` is the entry point; the other two
# are its first-party imports. Read as source (never imported) so the sweep needs
# no AWS, no installed packages and no import side effects.
HARNESS_MODULES = (
    REPO_ROOT / "scripts" / "test_api_rbac.py",
    REPO_ROOT / "scripts" / "rbac_common.py",
    REPO_ROOT / "scripts" / "api_security_cases.py",
)

# An AWS CLI subcommand of `aws cognito-idp` -> the IAM action it authorizes.
# Pure kebab-to-Pascal holds for every operation in this harness's surface
# (`admin-list-groups-for-user` -> `AdminListGroupsForUser`), and
# `test_a_derived_action_is_a_plausible_iam_action` keeps a subcommand that
# does not convert cleanly from being asserted as though it did.
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def _iam_action(subcommand: str) -> str:
    return "cognito-idp:" + "".join(p.capitalize() for p in subcommand.split("-"))


def _string_literals_in_source_order(path: Path) -> list[str]:
    """Every ``str`` constant in a module, in the order it appears in the file.

    The calls this gate reads take two shapes — ``aws("cognito-idp", "admin-...",
    ...)`` and ``subprocess.run([AWS_BIN, "cognito-idp", "admin-...", ...])`` —
    so matching on the call *site* would need both shapes and would miss a third.
    Reading the literal sequence instead makes the rule "the service name is
    followed by the operation name", which is a property of the CLI's own
    grammar and therefore holds for any call shape.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    consts: list[tuple[int, int, str]] = [
        (node.lineno, node.col_offset, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    consts.sort()
    return [value for _, _, value in consts]


def cognito_subcommands_called() -> dict[str, set[Path]]:
    """Map each ``aws cognito-idp <op>`` the harness invokes to the files calling it."""
    found: dict[str, set[Path]] = {}
    for path in HARNESS_MODULES:
        literals = _string_literals_in_source_order(path)
        for i, literal in enumerate(literals[:-1]):
            if literal != "cognito-idp":
                continue
            nxt = literals[i + 1]
            if _SUBCOMMAND.match(nxt):
                found.setdefault(nxt, set()).add(path)
    return found


class CfnLoader(yaml.SafeLoader):
    """SafeLoader (never the unsafe ``yaml.Loader``) plus CFN short-form tags.

    Registered in ``test_cfn_loader_safety._LOADERS``, which asserts the base
    class and that no ``python/object`` constructor is reachable from it.
    """


def _intrinsic(loader, _tag_suffix, node):
    """Collapse every ``!Foo`` to its operand text; this gate reads plain
    ``Action`` strings, so the operand's own structure is irrelevant.

    ``_tag_suffix`` is unused but positional: PyYAML fixes the multi-constructor
    signature, so it cannot be dropped.
    """
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


CfnLoader.add_multi_constructor("!", _intrinsic)


def granted_cognito_actions() -> set[str]:
    template = yaml.load(  # nosec B506 - SafeLoader subclass, see CfnLoader
        PIPELINE_TEMPLATE.read_text(encoding="utf-8"), Loader=CfnLoader
    )
    policies = [
        res
        for res in template["Resources"].values()
        if res.get("Type") == "AWS::IAM::Policy"
        and res.get("Properties", {}).get("PolicyName") == POLICY_NAME
    ]
    assert policies, (
        f"no AWS::IAM::Policy named {POLICY_NAME} in {PIPELINE_TEMPLATE.name} — "
        "renaming the policy silently empties this gate's granted set, so every "
        "harness call would read as ungranted rather than as a pass"
    )
    actions: set[str] = set()
    for policy in policies:
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Effect") != "Allow":
                continue
            raw = stmt.get("Action") or []
            for action in [raw] if isinstance(raw, str) else raw:
                if str(action).startswith("cognito-idp:"):
                    actions.add(str(action))
    return actions


# --------------------------------------------------------------------------
# The gate's own reading coverage: a sweep that finds nothing passes vacuously.
# --------------------------------------------------------------------------


def test_the_harness_calls_are_actually_found():
    called = cognito_subcommands_called()
    assert len(called) >= 8, (
        "the Cognito call sweep found only "
        f"{sorted(called)} — fewer calls than the harness has always made, so "
        "the literal-sequence reading has stopped matching the call shape and "
        "every assertion below is now vacuous"
    )
    # The one whose absence took out the whole of Step 12, pinned by name so a
    # reading change cannot drop exactly it and stay green.
    assert "admin-list-groups-for-user" in called


def test_the_template_grant_is_actually_found():
    granted = granted_cognito_actions()
    assert len(granted) >= 8, (
        f"only {sorted(granted)} parsed out of {POLICY_NAME} — the policy shape "
        "has changed under this gate, so the comparison below proves nothing"
    )


def test_every_module_in_the_harness_closure_is_read():
    """A new first-party import in the harness grows the universe this gate reads.

    Derived from the entry point's own imports rather than listed, so a module
    added to the closure fails here instead of being silently unswept — which is
    the way this gate itself would go quiet.
    """
    entry = HARNESS_MODULES[0]
    tree = ast.parse(entry.read_text(encoding="utf-8"), filename=str(entry))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
    first_party = {name for name in imported if (entry.parent / f"{name}.py").is_file()}
    read = {path.stem for path in HARNESS_MODULES}
    assert first_party <= read, (
        f"{sorted(first_party - read)} is imported by {entry.name} but not in "
        "HARNESS_MODULES — a Cognito call there would need a grant and this "
        "gate would not see it. Add it to HARNESS_MODULES."
    )


@pytest.mark.parametrize("subcommand", sorted(cognito_subcommands_called()))
def test_a_derived_action_is_a_plausible_iam_action(subcommand):
    action = _iam_action(subcommand)
    name = action.split(":", 1)[1]
    assert name and name[0].isupper() and name.isalnum(), (
        f"`aws cognito-idp {subcommand}` converts to {action!r}, which is not a "
        "well-formed IAM action name. Kebab-to-Pascal does not hold for this "
        "operation, so map it explicitly rather than asserting against a name "
        "that cannot be granted."
    )


# --------------------------------------------------------------------------
# The gate proper, in both directions.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subcommand", "callers"),
    sorted(
        (sub, sorted(p.name for p in files))
        for sub, files in cognito_subcommands_called().items()
    ),
)
def test_every_harness_cognito_call_is_granted(subcommand, callers):
    """A Cognito call the CodeBuild role cannot make is a red nightly pipeline."""
    action = _iam_action(subcommand)
    granted = granted_cognito_actions()
    assert action in granted, (
        f"{', '.join(callers)} calls `aws cognito-idp {subcommand}`, but "
        f"{POLICY_NAME} in {PIPELINE_TEMPLATE.name} does not grant {action}.\n"
        "Step 12 will fail with AccessDenied against the deployed pool. Add the "
        "action to the policy — and remember the policy is in a hand-installed "
        "stack, so the template change also has to be deployed to the CI account."
    )


@pytest.mark.parametrize("action", sorted(granted_cognito_actions()))
def test_every_granted_cognito_action_is_called(action):
    """A grant nothing calls is dead permission on a near-admin CI role.

    This is also what keeps the other direction honest: once an unused grant is
    acceptable, "the harness's calls are granted" stops being a statement about
    the harness and becomes one about how generous the policy is.
    """
    required = {_iam_action(sub) for sub in cognito_subcommands_called()}
    assert action in required, (
        f"{POLICY_NAME} grants {action}, but no module in the RBAC harness "
        f"({', '.join(p.name for p in HARNESS_MODULES)}) calls it. Remove the "
        "grant, or add the module that needs it to HARNESS_MODULES."
    )
