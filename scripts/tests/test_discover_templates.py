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

import gate_premises

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


def _arn_exempt_entries() -> tuple[str, list[str]]:
    """``(justification preamble, entries)`` for ``ARN_PARTITION_EXEMPT``."""
    text = MAKEFILE.read_text()
    match = re.search(r"^ARN_PARTITION_EXEMPT\s*:=\s*(.*)$", text, re.M)
    assert match, "ARN_PARTITION_EXEMPT is not defined in the Makefile"
    entries = match.group(1).split()
    assert entries, "an empty exemption list should just be removed"
    return text[: match.start()].rsplit("\n\n", 1)[-1], entries


def _shielded_per_entry() -> dict[str, dict[str, int]]:
    """``{entry: {file: findings hidden}}``, using the gate's own ARN detector.

    Files shielding zero findings are omitted, so the value is exactly the set of
    files an entry is doing work for.
    """
    _, entries = _arn_exempt_entries()
    result: dict[str, dict[str, int]] = {}
    for entry in entries:
        path, _, pattern = entry.partition(":")
        targets = (
            [path]
            if (REPO_ROOT / path).is_file()
            else [
                rel
                for rel in _discover("cfn")
                if rel == path or rel.startswith(path.rstrip("/") + "/")
            ]
        )
        per_file: dict[str, int] = {}
        for rel in targets:
            hits = gate_premises.matching_lines(
                rel, "arn:aws:", exclude=("arn:${AWS::Partition}:",)
            )
            count = sum(1 for _, line in hits if not pattern or pattern in line)
            if count:
                per_file[rel] = count
        result[entry] = per_file
    return result


def test_exemptions_are_per_path_and_justified() -> None:
    """An exemption must name something real, carry a reason, and be as narrow as it can.

    The gate exempts by PATH and LINE, never by rule: a rule switched off for every
    template loses its future value (the objection raised on #870).

    The preferred granularity is a single line, spelled ``<path>:<line-pattern>``.
    This test used to *require* the opposite — ``assert entry.endswith("/")``, "exempt
    by directory, not by file glob" — and that requirement was the reason the defect
    it should have caught could not be expressed. The one directory entry covered four
    templates under a single reason that was a property of two lines in one of them;
    per-directory granularity made it impossible to write down which. A bare path is
    still accepted for a case that genuinely needs one, but nothing may demand it.
    """
    preamble, entries = _arn_exempt_entries()
    for entry in entries:
        path, _, pattern = entry.partition(":")
        assert (REPO_ROOT / path).exists(), f"exempt path {path} does not exist"
        if pattern:
            assert (REPO_ROOT / path).is_file(), (
                f"{entry} exempts lines, so {path} must be a file"
            )
        # The justification block precedes the assignment and names the entry.
        assert entry in preamble, f"no justification comment names {entry}"


def test_no_exemption_hides_nothing() -> None:
    """Non-vacuity: an entry that shields no finding has no expressible reason.

    This is the only check that would have caught the fourth template under the old
    directory entry. It contained no hardcoded ARN at all, so the stated reason
    ("names a commercial-only cross-account principal") could not have been true of
    it, and no amount of reading the reason would reveal that — the reason was
    accurate about its neighbours. An exemption nobody needs is worse than none: it
    is a standing licence for whatever next occupies the path, granted by someone who
    never looked at it. ``check_retired_services.py`` polices its allowlist the same
    way.
    """
    vacuous = [
        f"{entry} (shields 0 of the gate's findings)"
        for entry, shielded in _shielded_per_entry().items()
        if not shielded
    ]
    assert not vacuous, (
        "these ARN_PARTITION_EXEMPT entries hide nothing, so whatever reason is "
        f"written for them cannot be about anything in the tree: {vacuous}. Delete "
        "them — an exemption that shields nothing still pre-exempts the next edit to "
        "that path."
    )


def test_no_exemption_covers_more_than_one_file() -> None:
    """One entry, one file — so one reason can only ever justify one file.

    This is the ratchet that makes the defect self-announcing rather than merely
    detectable. The old entry was a directory covering four templates, and the reason
    beside it was true of two lines in one of them. Nothing was wrong with the
    sentence; what was wrong was that one sentence was allowed to answer for four
    files, and an aggregate reading of it ("do these deploy only in the commercial
    account?") is satisfied.

    Bound an entry to a single file and that stops being possible: covering a second
    file requires a second entry, and a second entry requires its own reason. Three of
    the four templates then have no reason anyone could write, which is the point —
    the mismatch shows up while the exemption is being written, not in an audit
    months later.
    """
    overbroad = {
        entry: sorted(shielded)
        for entry, shielded in _shielded_per_entry().items()
        if len(shielded) > 1
    }
    assert not overbroad, (
        "these ARN_PARTITION_EXEMPT entries shield findings in more than one file, so "
        f"one reason is answering for several: {overbroad}. Split them into one entry "
        "per file (ideally `<path>:<line-pattern>`) and write the reason that holds "
        "for each — if one of them has no reason of its own, that is the finding."
    )
