# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pin the content-based template discovery behind the GovCloud partition gate.

`make check-arn-partitions` used to iterate a hardcoded glob list (template.yaml,
patterns/*/template.yaml, ...) and so never scanned nested/, samples/, notebooks/,
scripts/ or iam-roles/. The state-machine loop had the same shape and missed
src/lambda/ — where two definitions carried hardcoded `arn:aws:states:::`
service-integration ARNs that Step Functions rejects in GovCloud.

Discovery now lives in scripts/discover_templates.sh, shared with `make cfn-lint`.
These tests assert the property that matters: one representative file from each
directory the glob list was blind to is discovered, so a regression back to a
filename list — or a discovery script that silently returns a subset — fails here
rather than in a GovCloud deployment.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "discover_templates.sh"
MAKEFILE = REPO_ROOT / "Makefile"

pytestmark = pytest.mark.unit


def _discover(kind: str) -> list[str]:
    out = subprocess.run(
        [str(SCRIPT), kind],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


# One template per directory the old glob list never looked at. Keep these to
# files that exist for their own reasons (a shipped sample, a customer-facing IAM
# role, a notebook demo); if one is deleted, replace it with a neighbour rather
# than dropping the directory from the list.
BLIND_SPOT_TEMPLATES = [
    "nested/api-resolvers/template.yaml",
    "samples/lambda-hook-inference/template.yaml",
    "notebooks/examples/demo-lambda/template.yml",
    "iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml",
    "scripts/vpc-endpoints.yaml",
    # And the ones the glob list DID cover, so the port lost nothing.
    "template.yaml",
    "patterns/unified/template.yaml",
    "feature-platform/sample-feature/template.yaml",
]

# The two state machines the old `patterns/*/statemachine/*.asl.json` glob
# missed, plus the one it covered. Note the fine-tuning definition is not even
# named `*.asl.json` — a filename rule would need a second pattern for it.
BLIND_SPOT_STATE_MACHINES = [
    "src/lambda/multi_doc_discovery/statemachine.asl.json",
    "src/lambda/finetuning_state_machine/definition.json",
    "patterns/unified/statemachine/workflow.asl.json",
]


@pytest.mark.parametrize("template", BLIND_SPOT_TEMPLATES)
def test_discovers_template_in_previously_blind_directory(template: str) -> None:
    assert template in _discover("cfn"), (
        f"{template} declares AWSTemplateFormatVersion but discovery did not list "
        "it — the ARN-partition gate and cfn-lint are blind to it again"
    )


@pytest.mark.parametrize("definition", BLIND_SPOT_STATE_MACHINES)
def test_discovers_state_machine_outside_patterns(definition: str) -> None:
    assert definition in _discover("asl"), (
        f"{definition} is a Step Functions definition but discovery did not list "
        "it — hardcoded arn:aws:states::: integrations there would ship again"
    )


def test_every_discovered_template_exists_and_declares_the_marker() -> None:
    found = _discover("cfn")
    assert len(found) >= 25, f"suspiciously few templates discovered: {found}"
    for rel in found:
        path = REPO_ROOT / rel
        assert path.is_file(), rel
        assert re.search(r"^AWSTemplateFormatVersion", path.read_text(), re.M), rel


def test_discovery_honours_gitignore() -> None:
    """Ignored trees (build output, scratch worktrees) must not be scanned.

    Otherwise a stale .aws-sam/ copy or a scratch/ worktree gets linted too, and a
    violation in a copy of a file that has since been fixed fails the gate.
    """
    found = _discover("cfn")
    for rel in found:
        assert not re.match(r"(\.aws-sam|scratch|node_modules|dist|build)/", rel), (
            f"discovery listed an ignored path: {rel}"
        )
        assert "/.aws-sam/" not in rel and "/node_modules/" not in rel, rel


def test_both_gates_use_the_shared_discovery() -> None:
    """Two copies of the discovery would drift — that is how the glob list rotted."""
    text = MAKEFILE.read_text()
    arn_gate = text[text.index("\ncheck-arn-partitions:") :]
    # The recipe ends by handing off to the Python half of the gate.
    arn_gate = arn_gate[: arn_gate.index("scripts/check_python_arn_partitions.py")]
    cfn_lint = text[text.index("\ncfn-lint:") :]
    cfn_lint = cfn_lint[: cfn_lint.index("\n\n", 1)]

    assert "scripts/discover_templates.sh cfn" in arn_gate
    assert "scripts/discover_templates.sh asl" in arn_gate
    assert "scripts/discover_templates.sh cfn" in cfn_lint
    for glob in ("patterns/*/template.yaml", "patterns/*/statemachine/*.asl.json"):
        assert glob not in arn_gate, f"hardcoded glob {glob!r} is back in the gate"


def test_exemptions_are_per_path_and_justified() -> None:
    """A path exemption must name a directory that exists and carry a reason.

    The gate exempts by PATH prefix, never by rule: a rule switched off for every
    template loses its future value (the objection raised on #870). Each entry in
    ARN_PARTITION_EXEMPT must therefore be a real path with a comment explaining
    why it cannot be fixed instead.
    """
    text = MAKEFILE.read_text()
    match = re.search(r"^ARN_PARTITION_EXEMPT\s*:=\s*(.*)$", text, re.M)
    assert match, "ARN_PARTITION_EXEMPT is not defined in the Makefile"
    entries = match.group(1).split()
    assert entries, "an empty exemption list should just be removed"
    for entry in entries:
        assert (REPO_ROOT / entry).exists(), f"exempt path {entry} does not exist"
        assert entry.endswith("/"), f"exempt {entry} by directory, not by file glob"
        # The justification block precedes the assignment and names the entry.
        preamble = text[: match.start()].rsplit("\n\n", 1)[-1]
        assert entry in preamble, f"no justification comment names {entry}"
