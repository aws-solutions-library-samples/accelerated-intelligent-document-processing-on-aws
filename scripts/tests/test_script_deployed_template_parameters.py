# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Python deployers vs the templates they deploy: parameter names must match.

``test_nested_stack_parameters.py`` asserts this contract for CFN parent →
nested stack. Nothing asserted it for the other direction the repo deploys
templates in — a **Python script** calling ``cloudformation:CreateStack`` with a
``TemplateBody`` read from this tree — and that gap shipped a broken CI for every
run between two releases:

``iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml``
gained a *required* ``CreatedRolePermissionsBoundaryArn`` (issue #927, so that
``iam:CreateRole`` could be gated on ``iam:PermissionsBoundary`` and the role
would stop being a transitive account administrator). The template change was
correct and well tested — ``scripts/sdlc/validate_service_role_permissions.py``
and ``scripts/tests/test_iam_privilege_escalation.py`` both defend the policy's
shape. But ``create_iam_resources()`` in ``scripts/sdlc/codebuild_deployment.py``
still called ``create_stack()`` with no ``Parameters`` at all, so every
integration-test run died at step 0 with::

    ValidationError: Parameters: [CreatedRolePermissionsBoundaryArn] must have values

before creating anything — and because the main stack never existed, the failure
summary reported "stack does not exist" rather than the real cause.

The security property was gated; the caller contract was not. This test gates the
caller contract, in both directions:

* a required template parameter (no ``Default``) no caller supplies →
  ``Parameters: [X] must have values`` at CreateStack;
* a parameter a caller passes that the template does not declare →
  ``Parameters: [X] do not exist in the template``.

It also fails when a **new** deployer calls ``create_stack``/``update_stack`` (or
builds ``sam deploy --parameter-overrides``) without being registered below, so the
next template-deploying module cannot quietly reintroduce the gap.

Why the completeness walk covers ``lib/`` and not only ``scripts/``
------------------------------------------------------------------
It used to walk ``scripts/`` alone, which made every deployer under ``lib/``
structurally invisible to it — and two real mismatches lived there:

* ``lib/idp_sdk/idp_sdk/_core/stack.py``'s ``build_parameters`` emitted
  ``EnableHITL``, which the root ``template.yaml`` stopped declaring in v0.4.11 when
  HITL became a configuration setting. ``idp-cli deploy --enable-hitl true``
  therefore failed at CreateStack with ``Parameters: [EnableHITL] do not exist in
  the template``, creating nothing. ``lib/idp_cli_pkg/tests/test_deploy_params.py``
  asserted ``params["EnableHITL"] == "true"`` — it pinned the name the code
  produced, never the names the template accepts, so it passed on a deploy that
  could not run.
* ``lib/idp_feature_sdk/idp_feature_sdk/seller_service.py`` passed
  ``MarketplaceAgreementRegion`` to a template declaring ``AgreementRegion``
  (issue #1043). That path is ``sam deploy``, which is worse: SAM builds its
  ``CreateChangeSet`` call from the *template's* own ``Parameters`` and emits
  nothing for a name it does not find there, so the override was discarded in
  silence — the deploy succeeded with the parameter at its default.

Both failure modes are the same defect: an assertion about the argv or the dict the
code *built*, with nothing comparing those names to the template they are sent to.
The detector below therefore looks for the ``sam`` shape as well as the boto3 one,
since an argv builder would not have been spotted even with the walk widened.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


# Every script under scripts/ that deploys a template from this tree.
#
# ``static=True`` means the script's parameter list is literal enough to compare
# exactly (every ParameterKey is a string literal). ``static=False`` means the
# list is built at runtime — a loop or a comprehension over a dict — so the exact
# set cannot be read from the source; those get the weaker check that each
# required parameter name at least appears somewhere in the file, which still
# catches "template grew a required parameter and its caller never heard".
#
# ``static=False`` makes ``test_supplied_parameters_exist_in_template`` SKIP, and a
# skipped guard reads as a pass on the summary line. So each such entry carries a
# fourth field naming the test that checks it instead — a
# ``"<path>::<test_name>"`` node id — and
# ``test_every_substitute_check_still_exists`` asserts that test is really there.
# Without it, deleting the substitute leaves the whole site unguarded in silence,
# which is the same shape as the walk-can-only-get-quieter hole above, one level up.
#
# ``None`` means there is no substitute: the by-name check on required parameters is
# all this site gets. That is not free either — the path must be listed in
# ``NO_SUBSTITUTE_REASONS`` below, and the two sets are asserted to match, so a new
# entry cannot quietly opt out.
#
# Adding a script that deploys a template? Add it here. test_registry_is_complete
# fails until you do.
_SELLER_TESTS = "lib/idp_feature_sdk/tests/test_seller_service.py"

DEPLOYERS = [
    pytest.param(
        "scripts/sdlc/codebuild_deployment.py",
        "iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml",
        True,
        None,  # static=True: the exact-set comparison here IS the check.
        id="codebuild_deployment→cfn-service-role",
    ),
    pytest.param(
        "scripts/deploy-vpc-endpoints.py",
        "scripts/vpc-endpoints.yaml",
        # skip_params is looped over to add CreateXxx entries at runtime.
        False,
        None,
        id="deploy-vpc-endpoints→vpc-endpoints",
    ),
    pytest.param(
        "scripts/security/live_checks/oidc_provider/deploy.py",
        "scripts/security/live_checks/oidc_provider/template.yaml",
        # Parameters come from a comprehension over rsa_parameters().
        False,
        None,
        id="oidc_provider→oidc-template",
    ),
    pytest.param(
        "lib/idp_sdk/idp_sdk/_core/stack.py",
        "template.yaml",
        # build_parameters assigns into a plain dict rather than emitting
        # ParameterKey literals, so _literal_parameter_keys finds nothing here and
        # the exact-set comparison would be vacuous. The real check CALLS the
        # function and diffs its output against the template.
        False,
        "scripts/tests/test_script_deployed_template_parameters.py"
        "::test_build_parameters_emits_only_declared_parameters",
        id="idp_sdk.build_parameters→root-template",
    ),
    pytest.param(
        "lib/idp_feature_sdk/idp_feature_sdk/seller_service.py",
        "feature-platform/seller-entitlement-service/template.yaml",
        # Overrides are `_sam_override(...)` calls, not ParameterKey dicts. The
        # function validates them against this template at build time
        # (validate_parameter_overrides) and its own suite asserts that.
        False,
        f"{_SELLER_TESTS}::test_every_override_names_a_parameter_the_template_declares",
        id="seller_service→seller-template",
    ),
]

# Sites whose only check is the by-name one, with the reason. Asserted to be exactly
# the set of entries passing ``None``, so opting out stays a visible decision.
NO_SUBSTITUTE_REASONS = {
    "scripts/sdlc/codebuild_deployment.py": (
        "static=True — the exact-set comparison is not skipped for this entry, so "
        "there is nothing to substitute for."
    ),
    "scripts/deploy-vpc-endpoints.py": (
        "Pre-existing. Parameter names are generated per endpoint in a loop over "
        "ENDPOINTS, so there is no call whose output could be diffed without "
        "executing the script. The by-name required-parameter check still catches a "
        "template growing a required parameter."
    ),
    "scripts/security/live_checks/oidc_provider/deploy.py": (
        "Pre-existing. Throwaway test-fixture stack built from a comprehension over "
        "rsa_parameters(); a wrong name fails the live check that creates it, loudly "
        "and immediately, and nothing customer-facing depends on it."
    ),
}

# Deployers whose target template does not exist in this tree at all: it is
# published to S3 or generated at deploy time, so there is nothing to diff against.
# Registering them here is not a free pass — test_runtime_template_deployers_gate_
# their_submissions asserts what each one actually does instead, and the
# ``unconditional`` names below are re-checked against every in-tree template the
# deployer can be pointed at.
RUNTIME_TEMPLATE_DEPLOYERS = [
    pytest.param(
        "lib/idp_feature_sdk/idp_feature_sdk/pack.py",
        # Target: a pack wrapper template generated at publish time.
        (),
        id="pack.deploy_pack→runtime-wrapper",
    ),
    pytest.param(
        "lib/idp_feature_sdk/idp_feature_sdk/cli.py",
        # Target: a published extension template. Three optional overrides are
        # gated on validate_template; these two are submitted unconditionally as
        # "part of every feature template's contract", which is the claim
        # test_feature_templates_declare_the_unconditional_parameters checks.
        ("MainStackName", "FeatureBucket"),
        id="feature_cli.deploy_cmd→published-extension",
    ),
]


# The call shapes that mean "this module deploys a CloudFormation template".
#
# ``sam deploy`` is in here because it is the shape with the WORST failure mode and
# the one a boto3-only detector misses: CloudFormation rejects an undeclared
# parameter name outright, but SAM discards the override silently.
DEPLOY_CALL_MARKERS = (
    ".create_stack(",
    ".update_stack(",
    ".create_change_set(",
    "--parameter-overrides",
    # idp_feature_sdk's own wrapper around create/update. Without it the feature
    # CLI's deploy — which submits two parameters unconditionally — was invisible to
    # the walk even after the roots were widened, because the boto3 call it
    # ultimately makes lives in pack.py rather than in the module choosing the
    # parameters. test_the_walk_finds_every_deployer_it_is_supposed_to_police is what
    # surfaced that.
    "create_or_update_stack(",
)

# Directories walked for unregistered deployers. ``lib/`` is here because two live
# mismatches sat in it while the walk covered ``scripts/`` alone — see the module
# docstring.
DEPLOYER_SEARCH_ROOTS = ("scripts", "lib", "feature-platform")

# The same completeness property for SHELL deployers. Two already existed and were
# invisible to the walk purely because it filtered on ``.py`` — the identical shape to
# the roots being too narrow, one extension over. Both of these *print* an `aws
# cloudformation` command for an operator to paste rather than running it, so a wrong
# parameter set fails loudly in that operator's terminal; extending the walk is still
# what stops the next one going unregistered.
#
# Only the REVERSE direction is checked for these (every required template parameter
# must appear in the script). The forward direction needs the exact override list, and
# harvesting that from shell is not reliable — a `NAME=$VAR` regex over
# check-vpc-endpoints.sh also picks up `Values`, `count` and a JMESPath fragment, and
# a gate with false positives fails for the wrong reason. Both scripts' override lists
# were read by hand against their templates and match.
SHELL_DEPLOY_MARKERS = (
    "aws cloudformation deploy",
    "aws cloudformation create-stack",
    "aws cloudformation update-stack",
)

SHELL_DEPLOYERS = [
    pytest.param(
        "scripts/check-vpc-endpoints.sh",
        "scripts/vpc-endpoints.yaml",
        id="check-vpc-endpoints→vpc-endpoints",
    ),
    pytest.param(
        "feature-platform/idp-data-generator/publish.sh",
        "feature-platform/idp-data-generator/template.yaml",
        id="idp-data-generator-publish→its-template",
    ),
]

REGISTERED_FILES = (
    {p.values[0] for p in DEPLOYERS}
    | {p.values[0] for p in RUNTIME_TEMPLATE_DEPLOYERS}
    | {p.values[0] for p in SHELL_DEPLOYERS}
)

# A feature-platform directory is installable by `idp-feature-cli deploy` exactly
# when it carries a manifest; that is what makes it a publishable extension, so it
# is the right way to derive the universe rather than listing directory names.
FEATURE_MANIFEST = "feature.yaml"


def _template_parameters(rel_path: str) -> dict[str, dict]:
    doc = yaml.load((REPO_ROOT / rel_path).read_text(), Loader=_CfnLoader) or {}
    return doc.get("Parameters") or {}


def _required_parameters(rel_path: str) -> set[str]:
    """Parameters with no Default — CloudFormation rejects CreateStack without them."""
    return {
        name
        for name, spec in _template_parameters(rel_path).items()
        if isinstance(spec, dict) and "Default" not in spec
    }


def _literal_parameter_keys(rel_path: str) -> set[str]:
    """String literals used as a "ParameterKey" value anywhere in the file."""
    tree = ast.parse((REPO_ROOT / rel_path).read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "ParameterKey"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                keys.add(value.value)
    return keys


@pytest.mark.unit
@pytest.mark.parametrize(("script", "template", "static", "substitute"), DEPLOYERS)
def test_required_parameters_are_supplied(
    script: str, template: str, static: bool, substitute: str | None
):
    """Each required template parameter is supplied by the script that deploys it."""
    required = _required_parameters(template)
    if not required:
        pytest.skip(f"{template} has no parameters without defaults")

    if static:
        supplied = _literal_parameter_keys(script)
        missing = required - supplied
        assert not missing, (
            f"{script} deploys {template} without required parameter(s) "
            f"{sorted(missing)}. CloudFormation will reject CreateStack with "
            f'"Parameters: {sorted(missing)} must have values" before creating '
            "anything. Add them to the Parameters list."
        )
        return

    source = (REPO_ROOT / script).read_text()
    missing = {name for name in required if name not in source}
    assert not missing, (
        f"{script} deploys {template}, which requires {sorted(missing)}, but that "
        f"name does not appear in the script at all. Its parameter list is built "
        "at runtime so this check is by-name only — supply the parameter."
    )


@pytest.mark.unit
@pytest.mark.parametrize(("script", "template", "static", "substitute"), DEPLOYERS)
def test_supplied_parameters_exist_in_template(
    script: str, template: str, static: bool, substitute: str | None
):
    """No script passes a parameter the template does not declare."""
    if not static:
        # Skipped, not weakened — and ``substitute`` records what checks it instead,
        # asserted to exist by test_every_substitute_check_still_exists. Read the skip
        # reason as "checked elsewhere, named", not as "checked".
        pytest.skip(
            f"{script} builds its parameter list at runtime; checked by "
            f"{substitute or 'nothing but the by-name test above (see '
            'NO_SUBSTITUTE_REASONS)'}"
        )

    declared = set(_template_parameters(template))
    unknown = _literal_parameter_keys(script) - declared
    assert not unknown, (
        f"{script} passes {sorted(unknown)} to {template}, which does not declare "
        f'it. CloudFormation will reject the call with "Parameters: '
        f'{sorted(unknown)} do not exist in the template".'
    )


def _tracked_files(suffix: str) -> list[str]:
    """Git-tracked paths with ``suffix`` under the search roots.

    ``git ls-files`` rather than ``rglob``: an untracked scratch script, a stale
    build artifact or a vendored copy in a working tree must not be able to fail
    this gate, and — more importantly — must not be able to *satisfy* it.

    Parametrised by suffix rather than hardcoding ``.py``, because filtering on the
    extension is itself a way for the walk to go blind: two SHELL deployers existed
    and were unreachable for no reason other than that.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", *DEPLOYER_SEARCH_ROOTS],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"git ls-files failed under {REPO_ROOT}, so this gate cannot enumerate the "
        f"modules it is supposed to check: {result.stderr.strip()}"
    )
    return [p for p in result.stdout.splitlines() if p.endswith(suffix)]


def _markers_for(rel_path: str) -> tuple[str, ...]:
    """The deploy-call shapes that apply to a file, chosen by its language."""
    return SHELL_DEPLOY_MARKERS if rel_path.endswith(".sh") else DEPLOY_CALL_MARKERS


def _shell_without_comments(path: Path) -> str:
    """A shell script's executable lines, comments dropped.

    A by-name check over the raw text is satisfied by a *comment* mentioning the
    parameter — including a comment explaining why the parameter is needed, which is
    exactly the comment someone adds while removing the line that supplies it. Found
    by mutation: deleting the `FeatureBucket` echo from publish.sh left this gate green
    because the comment above it still named it.

    Only whole-line comments are dropped. A trailing `#` inside a quoted string is not
    worth parsing for, and a parameter name appearing in a trailing comment on an
    otherwise-live line is not the failure mode this guards.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


@pytest.mark.unit
def test_the_walk_finds_every_deployer_it_is_supposed_to_police():
    """Universe closure, the direction ``test_registry_is_complete`` cannot check.

    That test only reports modules the walk finds and the registry does not list, so
    narrowing ``DEPLOYER_SEARCH_ROOTS`` or weakening ``DEPLOY_CALL_MARKERS`` makes it
    *quieter*, never red — which is exactly how the old ``scripts/``-only walk could
    sit beside two live mismatches in ``lib/`` and stay green. This asserts the other
    direction: every module already known to deploy a template must be REACHABLE by
    the walk and RECOGNISED by the detector.

    So a root removed, or a call shape dropped from the markers, fails here by name.
    """
    reachable = set(_tracked_files(".py")) | set(_tracked_files(".sh"))
    for module in sorted(REGISTERED_FILES):
        assert module in reachable, (
            f"{module} is registered as a deployer but the walk cannot reach it: it "
            f"is not a git-tracked .py or .sh file under {DEPLOYER_SEARCH_ROOTS}. "
            "Widen DEPLOYER_SEARCH_ROOTS or the extensions walked, or the walk "
            "polices a set that excludes a deployer this file already knows about."
        )
        source = (REPO_ROOT / module).read_text()
        markers = _markers_for(module)
        matched = [m for m in markers if m in source]
        assert matched, (
            f"{module} is registered as a deployer but none of {list(markers)} "
            "appears in it, so the detector would not recognise a NEW module "
            "deploying a template the same way. Add that call shape."
        )


@pytest.mark.unit
def test_registry_is_complete():
    """Every template-deploying module under the search roots is registered above.

    Without this, the next module to deploy a stack silently escapes the
    parameter-name check — the way create_iam_resources did, and the way
    build_parameters and seller_service did for longer (see the module docstring).
    """
    python_files = _tracked_files(".py")
    shell_files = _tracked_files(".sh")
    # Non-vacuity, per language: a broken enumeration would make "nothing
    # unregistered" trivially true, which is the failure mode this whole module exists
    # to catch. Shell is checked separately because it is the smaller set and would
    # vanish unnoticed inside a combined count.
    assert len(python_files) > 100, (
        f"only {len(python_files)} tracked .py files found under "
        f"{DEPLOYER_SEARCH_ROOTS}; the enumeration is broken, and an empty walk "
        "would pass this gate while checking nothing"
    )
    assert len(shell_files) > 10, (
        f"only {len(shell_files)} tracked .sh files found under "
        f"{DEPLOYER_SEARCH_ROOTS}; shell deployers would then be invisible again"
    )

    unregistered = []
    for rel in sorted(python_files + shell_files):
        if "/tests/" in rel or Path(rel).name.startswith("test_"):
            continue
        if rel in REGISTERED_FILES:
            continue
        source = (REPO_ROOT / rel).read_text()
        if any(marker in source for marker in _markers_for(rel)):
            unregistered.append(rel)

    assert not unregistered, (
        "These modules deploy a CloudFormation stack but are not in DEPLOYERS, so "
        f"nothing checks their parameter names against the template they target: "
        f"{unregistered}. Add an entry (see the comment on DEPLOYERS)."
    )


@pytest.mark.unit
def test_build_parameters_emits_only_declared_parameters():
    """``idp_sdk.build_parameters`` vs the root template's ``Parameters``.

    The check the pre-existing tests for this function did not make. They asserted
    the keys it produced — including ``EnableHITL``, which the root template stopped
    declaring in v0.4.11 — so they passed on a parameter set CloudFormation rejects.
    Calling the function and comparing its output against the template is stronger
    than scraping the source, and it is the only reason a name removed from the
    template can no longer sit in this function unnoticed.

    Every optional argument is supplied so no branch is left unexercised, and
    ``additional_params`` is deliberately NOT passed: those are the operator's own
    ``--parameters key=value`` pass-through and are their responsibility, whereas
    everything else here is a name this repository chose.
    """
    from idp_sdk._core.stack import build_parameters

    supplied = build_parameters(
        admin_email="admin@example.com",
        max_concurrent=100,
        log_level="INFO",
        custom_config="s3://example-bucket/config.yaml",
    )
    declared = set(_template_parameters("template.yaml"))
    assert len(declared) > 10, (
        f"only {len(declared)} parameters parsed from template.yaml; a failed parse "
        "would make the comparison below vacuous"
    )
    assert supplied, "build_parameters emitted nothing, so this check is vacuous"

    unknown = set(supplied) - declared
    assert not unknown, (
        f"build_parameters passes {sorted(unknown)} to template.yaml, which does "
        f'not declare it. CloudFormation rejects the call with "Parameters: '
        f'{sorted(unknown)} do not exist in the template" and creates nothing.'
    )


@pytest.mark.unit
@pytest.mark.parametrize(("module", "unconditional"), RUNTIME_TEMPLATE_DEPLOYERS)
def test_runtime_template_deployers_gate_their_submissions(
    module: str, unconditional: tuple[str, ...]
):
    """A deployer with no in-tree template must gate submissions at runtime.

    ``validate_template`` returns the target's declared ``Parameters``, so gating on
    it is the runtime form of the static check the other entries get. Asserting the
    call is present is what stops this category from becoming the place a deployer is
    parked to escape the gate — the cheapest way to make a mismatch invisible is to
    claim the template cannot be found.
    """
    source = (REPO_ROOT / module).read_text()
    assert "validate_template(" in source, (
        f"{module} deploys a template that does not exist in this tree, so the only "
        "available check on its parameter names is cfn.validate_template() at "
        "runtime — and it does not call it. Every submitted name would reach "
        "CloudFormation unchecked."
    )
    for name in unconditional:
        assert f'"{name}"' in source, (
            f"{module} is registered as submitting {name} unconditionally, but that "
            "name no longer appears in it. Update RUNTIME_TEMPLATE_DEPLOYERS, or the "
            "template check below is guarding a parameter nobody sends."
        )


@pytest.mark.unit
def test_feature_templates_declare_the_unconditional_parameters():
    """`idp-feature-cli deploy` submits MainStackName + FeatureBucket without asking.

    That is safe only while every template it can be pointed at declares them, which
    was a claim in a comment and nothing more. The universe is derived from the
    presence of a feature manifest — the thing that makes a directory publishable and
    therefore deployable — so a new extension is covered the moment it is publishable,
    and `main-stack-extensions` / `seller-entitlement-service` are correctly out of
    scope because `deploy-feature` cannot target them.
    """
    unconditional = next(
        p.values[1]
        for p in RUNTIME_TEMPLATE_DEPLOYERS
        if str(p.values[0]).endswith("idp_feature_sdk/cli.py")
    )
    assert unconditional, "no unconditional parameters registered; check is vacuous"

    manifests = sorted(
        path
        for path in (REPO_ROOT / "feature-platform").glob(f"*/{FEATURE_MANIFEST}")
        if (path.parent / "template.yaml").is_file()
    )
    assert len(manifests) >= 4, (
        f"only {len(manifests)} publishable feature template(s) discovered under "
        "feature-platform/; the derivation is broken and an empty universe would "
        "pass this gate while checking nothing"
    )

    missing = {}
    for manifest in manifests:
        relative = manifest.parent.relative_to(REPO_ROOT).as_posix()
        declared = set(_template_parameters(f"{relative}/template.yaml"))
        absent = [name for name in unconditional if name not in declared]
        if absent:
            missing[relative] = absent

    assert not missing, (
        "`idp-feature-cli deploy` submits these parameters unconditionally, but "
        f"these publishable feature templates do not declare them: {missing}. "
        'CloudFormation would reject the deploy with "Parameters: [...] do not '
        'exist in the template".'
    )


def _test_functions_in(rel_path: str) -> set[str]:
    """Names of test functions defined in a module, by AST.

    Parsed rather than imported: the substitutes live in packages with their own
    conftest and fixtures, and importing one from here would couple this gate to
    another suite's setup for no gain — the question is only whether the function is
    still defined.
    """
    tree = ast.parse((REPO_ROOT / rel_path).read_text())
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


@pytest.mark.unit
@pytest.mark.parametrize(("script", "template", "static", "substitute"), DEPLOYERS)
def test_every_substitute_check_still_exists(
    script: str, template: str, static: bool, substitute: str | None
):
    """A named substitute must resolve to a test that is actually defined.

    ``static=False`` makes ``test_supplied_parameters_exist_in_template`` skip, so for
    those entries the substitute is the ONLY thing checking that the site's parameter
    names match its template. Nothing asserted the substitute existed, which means
    deleting it left the site unguarded and every suite still green — the same hole as
    a walk that can only get quieter, one level up.

    Proven by chained mutation: removing
    ``test_build_parameters_emits_only_declared_parameters`` and reintroducing an
    undeclared name in ``build_parameters`` produced no failure anywhere.
    """
    if substitute is None:
        assert script in NO_SUBSTITUTE_REASONS, (
            f"{script} is registered with no substitute check, so the only thing "
            "guarding its parameter names is the by-name required-parameter test. "
            "That may be acceptable, but it has to be a stated decision: add an "
            "entry to NO_SUBSTITUTE_REASONS saying why, or name a substitute."
        )
        return

    path, _, name = substitute.partition("::")
    assert path and name, f"substitute for {script} is not a node id: {substitute!r}"
    module = REPO_ROOT / path
    assert module.is_file(), (
        f"{script}'s substitute check names {path}, which does not exist. Its "
        "parameter names are otherwise unchecked."
    )
    defined = _test_functions_in(path)
    assert defined, f"no test functions found in {path}; the parse is broken"
    assert name in defined, (
        f"{script}'s substitute check {substitute} is gone. Because this entry is "
        f"static=False, test_supplied_parameters_exist_in_template SKIPS for it, so "
        f"nothing now checks that its parameter names match {template}. Restore the "
        "test, point this entry at its replacement, or move the entry to "
        "NO_SUBSTITUTE_REASONS with a reason."
    )


@pytest.mark.unit
def test_no_substitute_reasons_matches_the_registry():
    """Universe closure over the substitute field, in both directions.

    Without the reverse direction a reason could be left behind after its entry grew a
    real substitute, and the dict would drift into a list of stale excuses that reads
    as though someone had considered each one.
    """
    # str() because pytest types ``param.values`` as ``tuple[object | NotSetType,
    # ...]``, so the set difference below is not otherwise well-typed.
    declared_none = {str(p.values[0]) for p in DEPLOYERS if p.values[3] is None}
    assert declared_none == set(NO_SUBSTITUTE_REASONS), (
        "NO_SUBSTITUTE_REASONS must name exactly the DEPLOYERS entries with no "
        f"substitute.\n  registered with None but unexplained: "
        f"{sorted(declared_none - set(NO_SUBSTITUTE_REASONS))}\n"
        f"  explained but no longer needs it: "
        f"{sorted(set(NO_SUBSTITUTE_REASONS) - declared_none)}"
    )
    for script, reason in NO_SUBSTITUTE_REASONS.items():
        assert len(reason) > 40, f"{script}'s reason is too short to be one: {reason!r}"


@pytest.mark.unit
@pytest.mark.parametrize(("script", "template"), SHELL_DEPLOYERS)
def test_shell_deployers_name_every_required_parameter(script: str, template: str):
    """A printed `aws cloudformation` command must not omit a required parameter.

    These two scripts print a command for an operator to paste. A required parameter
    left out of it is rejected with ``Parameters: [X] must have values`` and creates
    nothing — loud, but only after the operator has run it, and the remedy is not
    obvious from a script that looked authoritative.

    Both were missing one when this check was added: ``check-vpc-endpoints.sh`` omitted
    ``VpcCidr`` (which feeds the endpoint security group's rules) and
    ``idp-data-generator/publish.sh`` omitted ``FeatureBucket``.

    By-name only, in the same sense as the ``static=False`` Python entries: a reliable
    forward check needs the exact override list, and harvesting that from shell
    produces false positives (see the note on SHELL_DEPLOYERS).
    """
    required = _required_parameters(template)
    assert required, (
        f"{template} declares no parameter without a Default, so this check is "
        "vacuous — verify the template still has the shape this test assumes"
    )
    source = _shell_without_comments(REPO_ROOT / script)
    missing = sorted(name for name in required if name not in source)
    assert not missing, (
        f"{script} prints a deploy command for {template} but never mentions "
        f"{missing}, which {template} requires. CloudFormation rejects the pasted "
        f'command with "Parameters: {missing} must have values" and creates nothing.'
    )
