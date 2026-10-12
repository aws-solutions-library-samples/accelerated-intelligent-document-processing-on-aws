# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The malware gate has TWO reprocess doors, and both must refuse the same set.

Reprocess does not go through the ingest gate: it re-queues a document directly.
That is the hazard the gate's reprocess guards exist for — but there is more than
one way in, and they share no code:

* ``nested/api-resolvers/src/lambda/reprocess_document_resolver`` serves the web
  UI and the REST API.
* ``lib/idp_sdk/idp_sdk/_core/rerun_processor.RerunProcessor.rerun_documents``
  serves ``idp-cli reprocess``, ``documents.reprocess()`` and
  ``batches.reprocess()``. It runs **client-side** under the caller's own
  credentials, reads the tracking row itself and sends to SQS itself, so nothing
  in the resolver can constrain it.

Guarding one and not the other leaves the gate fully bypassable, and the CLI door
is the worse one to leave open: ``--batch-id`` re-queues every document in a batch
unattended, so a quarantined file rides along with its clean siblings and nobody
is watching a tooltip.

⚠️ The two cannot share a constant. The resolver imports ``idp_common.models``
directly; the SDK reaches ``idp_common`` through a lazy path shim and must not
fail at import on a machine without it. So the sets are written twice and
reconciled here — which is the only reason this file exists.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RESOLVER = (
    REPO
    / "nested"
    / "api-resolvers"
    / "src"
    / "lambda"
    / "reprocess_document_resolver"
    / "index.py"
)
RERUN = REPO / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "rerun_processor.py"


def _resolver_refused_statuses() -> set[str]:
    """The resolver's set, read from source.

    Parsed rather than imported: the module needs a stubbed ``idp_common`` and a
    pile of environment variables to import, and this check is about the values
    rather than about the module running.
    """
    tree = ast.parse(RESOLVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_REPROCESS_REFUSED_STATUSES" not in names:
            continue
        # frozenset({Status.X.value, ...}) -> the X names
        return {
            attr.value.attr
            for attr in ast.walk(node.value)
            if isinstance(attr, ast.Attribute)
            and attr.attr == "value"
            and isinstance(attr.value, ast.Attribute)
        }
    raise AssertionError("_REPROCESS_REFUSED_STATUSES not found in the resolver")


def _sdk_refused_statuses() -> set[str]:
    source = RERUN.read_text(encoding="utf-8")
    block = re.search(r"_MALWARE_GATE_REFUSALS\s*=\s*\{(.*?)\n\}", source, re.S)
    assert block, "_MALWARE_GATE_REFUSALS not found in the rerun processor"
    return set(re.findall(r'"([A-Z_]+)":', block.group(1)))


@pytest.mark.unit
def test_both_reprocess_doors_refuse_the_same_statuses():
    resolver = _resolver_refused_statuses()
    sdk = _sdk_refused_statuses()
    assert resolver, "parsed an empty set from the resolver; has it been renamed?"
    assert resolver == sdk, (
        f"the two reprocess paths disagree.\n"
        f"  resolver (UI/API) refuses: {sorted(resolver)}\n"
        f"  SDK/CLI refuses:           {sorted(sdk)}\n"
        f"Only in resolver: {sorted(resolver - sdk)}\n"
        f"Only in SDK:      {sorted(sdk - resolver)}\n"
        f"A status refused by one and allowed by the other leaves the malware "
        f"gate bypassable through whichever path is permissive."
    )


@pytest.mark.unit
def test_the_set_is_derived_from_the_status_enum():
    """Non-vacuity, and the reason the two lists are allowed to be hand-written:
    they must name real ``Status`` members, not strings nobody produces."""
    import sys

    sys.path.insert(0, str(REPO / "lib" / "idp_common_pkg"))
    from idp_common.models import Status

    known = {s.name for s in Status}
    for name in _resolver_refused_statuses() | _sdk_refused_statuses():
        assert name in known, (
            f"{name} is refused for reprocess but is not a Status member; it can "
            f"never match a real document and the guard is dead for that value."
        )


@pytest.mark.unit
def test_the_sdk_guard_is_actually_called():
    """A refusal table nothing consults is the shape of defect this whole file is
    about. Asserted against the source because importing the module pulls in
    boto3 clients and a stack lookup."""
    source = RERUN.read_text(encoding="utf-8")
    # ⚠️ The needle includes the assignment deliberately. A bare
    # `_malware_gate_refusal(document)` also matches the function DEFINITION
    # (`def _malware_gate_refusal(document):`), so it would be satisfied by the
    # definition alone and this check would pass with no call site at all.
    needle = "refusal = _malware_gate_refusal(document)"
    assert needle in source, (
        "the SDK's refusal table is defined but never consulted in "
        "rerun_documents, so every CLI and SDK reprocess bypasses the gate"
    )
    # Before the send, not after it.
    call = source.index(needle)
    send = source.index("self._send_to_queue(document)")
    assert call < send, (
        "the malware check must run before _send_to_queue; ordering is the whole "
        "control, and a refusal after the send has already handed over the file"
    )
