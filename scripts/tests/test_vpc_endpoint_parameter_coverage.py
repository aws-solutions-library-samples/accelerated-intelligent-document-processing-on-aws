# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every VPC endpoint parameter is either deployed by the helpers or deprecated.

``scripts/vpc-endpoints.yaml`` declares one ``Create<Service>Endpoint`` parameter per
interface endpoint a PRIVATE deployment can need. Two helpers carry a *copy* of that
list — ``scripts/deploy-vpc-endpoints.py``'s ``REQUIRED_ENDPOINTS`` (which creates them)
and ``scripts/check-vpc-endpoints.sh``'s ``ENDPOINTS`` (which reports on them) — and
each deliberately omits the two retired AppSync parameters.

Nothing compared any of the three. A new endpoint parameter added to the template was
simply not deployed by the helper, and the failure surfaced at deploy time as a PRIVATE
stack missing an endpoint rather than as a red gate. This closes that by **deriving**
the universe from the template and requiring every member to be accounted for:

* named by both helpers, or
* deprecated — ``DEPRECATED`` in its ``Description`` **and** ``Default: "false"``.

The second arm is why there is no authored exclusion list here. "These two are retired"
is a fact the template already states in a machine-readable way, so it is computed
rather than asserted, and a third retirement needs no edit to this file. A parameter
that is neither deployed nor marked deprecated fails, in both directions: an
undeployed live parameter, and a deprecated one a helper still names.

Scope: this is about the *parameter* list. Whether each endpoint's service name is
right, and whether the resources behind the two deprecated parameters still work, are
different questions — both deprecated parameters do still declare real
``AWS::EC2::VPCEndpoint`` resources behind a condition, so omitting them from the
helpers is correct rather than lossy: a caller whose old parameter file sets one to
"true" still gets the endpoint from CloudFormation.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO / "scripts" / "vpc-endpoints.yaml"
_PY_HELPER = _REPO / "scripts" / "deploy-vpc-endpoints.py"
_SH_HELPER = _REPO / "scripts" / "check-vpc-endpoints.sh"

_PARAM_RE = re.compile(r"^  (Create\w*Endpoint):\s*$", re.MULTILINE)


def _declared_parameters() -> dict[str, str]:
    """Every ``Create*Endpoint`` parameter, mapped to its own YAML block.

    Read as text rather than through a YAML parser because the template uses
    CloudFormation short tags (``!Ref``, ``!Sub``, ``!Equals``) that ``yaml.safe_load``
    refuses, and the alternative — a custom loader — would be more machinery than a
    parameter-name scan needs.
    """
    text = _TEMPLATE.read_text(encoding="utf-8")
    matches = list(_PARAM_RE.finditer(text))
    assert matches, (
        f"no Create*Endpoint parameters found in {_TEMPLATE.name}; the scan is broken "
        "and every assertion below would pass vacuously"
    )
    # The last parameter's block ends at the next top-level section, not at the end of
    # the file. Running it to `len(text)` sweeps Conditions, Resources and Outputs into
    # that one parameter, and there are two `# DEPRECATED` comments down there on the
    # AppSync endpoint resources — so the last parameter satisfies half of
    # `_is_deprecated` for reasons that have nothing to do with it. Only its
    # `Default: "true"` keeps that from mattering today, which is the wrong thing to
    # depend on: appending a parameter that defaults to "false", or flipping this one's
    # default, would silently move it into the deprecated arm and out of the closure.
    tail = re.compile(r"^[A-Za-z]", re.MULTILINE)
    blocks: dict[str, str] = {}
    for i, m in enumerate(matches):
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            section = tail.search(text, m.end())
            end = section.start() if section else len(text)
        blocks[m.group(1)] = text[m.start() : end]
    return blocks


def _is_deprecated(block: str) -> bool:
    """Both halves are required: the word, and an off-by-default value.

    ``DEPRECATED`` alone would let a live endpoint be dropped from the helpers by
    editing a description. ``Default: "false"`` alone is true of most of these
    parameters, since the helper turns them on explicitly.
    """
    return "DEPRECATED" in block and re.search(r'Default:\s*"false"', block) is not None


def _python_helper_keys() -> set[str]:
    """``REQUIRED_ENDPOINTS``' keys, parsed rather than imported.

    The module runs argument parsing and boto3 setup at import, so it is read as source
    — the same reason ``exemption_discovery`` reads source.
    """
    tree = ast.parse(_PY_HELPER.read_text(encoding="utf-8"), filename=str(_PY_HELPER))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "REQUIRED_ENDPOINTS"
            for t in node.targets
        ):
            continue
        assert isinstance(node.value, ast.Dict), "REQUIRED_ENDPOINTS is not a dict"
        return {
            k.value
            for k in node.value.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
    raise AssertionError(f"REQUIRED_ENDPOINTS not found in {_PY_HELPER.name}")


def _shell_helper_keys() -> set[str]:
    """The ``ENDPOINTS`` associative-array keys in the shell reporter."""
    text = _SH_HELPER.read_text(encoding="utf-8")
    body = re.search(r"declare -A ENDPOINTS=\((.*?)\n\)", text, re.DOTALL)
    assert body, f"ENDPOINTS array not found in {_SH_HELPER.name}"
    keys = set(re.findall(r"\[(\w+)\]=", body.group(1)))
    assert keys, f"ENDPOINTS in {_SH_HELPER.name} parsed as empty"
    return keys


@pytest.mark.parametrize("parameter", sorted(_declared_parameters()))
def test_every_endpoint_parameter_is_deployed_or_deprecated(parameter: str) -> None:
    """Universe closure, one parameter at a time."""
    block = _declared_parameters()[parameter]
    in_python = parameter in _python_helper_keys()
    deprecated = _is_deprecated(block)

    if deprecated:
        assert not in_python, (
            f'{parameter} is marked DEPRECATED with Default "false" in '
            f"{_TEMPLATE.name}, but {_PY_HELPER.name} still lists it, so the helper "
            "would report it as 'missing — will create' for an endpoint nothing needs. "
            "Drop it from REQUIRED_ENDPOINTS, or un-deprecate the parameter."
        )
        return

    assert in_python, (
        f"{parameter} is declared in {_TEMPLATE.name} and is not deprecated, but "
        f"{_PY_HELPER.name}'s REQUIRED_ENDPOINTS does not name it — so the helper never "
        "creates it and nothing says so until a PRIVATE deployment fails at runtime. "
        "Add it with the service short name, or mark the parameter DEPRECATED with "
        'Default: "false" if it is retired.'
    )


def test_the_two_helpers_carry_the_same_list() -> None:
    """One list in two files must not drift; they are read by the same operator."""
    python_keys = _python_helper_keys()
    shell_keys = _shell_helper_keys()
    assert python_keys == shell_keys, (
        "scripts/deploy-vpc-endpoints.py and scripts/check-vpc-endpoints.sh disagree "
        "about which endpoint parameters exist, so the reporter and the deployer would "
        "give different answers for the same stack.\n"
        f"  only in deploy-vpc-endpoints.py: {sorted(python_keys - shell_keys)}\n"
        f"  only in check-vpc-endpoints.sh:  {sorted(shell_keys - python_keys)}"
    )


def test_the_deprecated_arm_is_not_vacuous() -> None:
    """A closure rule nothing exercises is a rule nobody has tested.

    If no parameter is deprecated, ``_is_deprecated`` could be broken — always
    returning False — and every case above would still pass on the other arm.
    """
    deprecated = sorted(
        name for name, block in _declared_parameters().items() if _is_deprecated(block)
    )
    assert deprecated, (
        'no Create*Endpoint parameter is marked DEPRECATED with Default "false", so '
        "the deprecation arm of this closure is untested. If the retired parameters "
        "were removed outright, delete that arm rather than leaving it unexercised."
    )
