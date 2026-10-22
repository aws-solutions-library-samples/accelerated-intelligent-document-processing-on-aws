# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The shared UX runner: the pieces of the unattended UX test, one module each.

What lives here is everything the unattended test needs that is not the
orchestrator itself: toolchain locations and the fixed host, port and uid
contract, process groups, redaction, the tool sandbox, the ``claude`` invocation,
the account guard, the egress proxy's harness side, the confined Chrome, the CDP
sign-in, recorder control, reading back what another uid wrote, change
classification, flow selection, MR inputs, note composition, the GitLab client and
the prompt. The MR UX test
(``scripts/sdlc/ai_ux_test.py``) uses it now; the nightly user-story tier is meant
to reuse the same pieces rather than grow a second copy of them.

Import rule
-----------
Callers put ``scripts/`` on ``sys.path`` and import ``from ux_runner import x``.
``browser.py`` is executed by path and inserts ``scripts/`` itself.
``run_in_docker.py`` and ``ui_change.py`` are self-contained stdlib, because they
run on developer hosts and in CI jobs where nothing is installed, and a sibling
import there would be one more thing that has to be present for a precheck to
answer.

Two import boundaries are load-bearing
--------------------------------------
* ``scripts/sdlc/ai_mr_review.py`` is imported in exactly one module,
  ``gitlab_api.py``, and everything else reaches it as ``gitlab_api.review``.
  ``test_ai_mr_review.py`` re-executes that module and replaces
  ``sys.modules['ai_mr_review']`` under xdist, so two import sites could hold two
  different ``Skip`` classes, and an ``except review.Skip`` would then silently
  stop matching.
* ``boto3``, ``botocore``, ``PIL`` and ``yaml`` are imported inside the function
  that needs them, never at module level, so the precheck and ``ui_change.py``
  run on a bare ``python3`` before the pinned toolchain is installed.

This file is deliberately a docstring and nothing else: importing the package
must not import any of its modules, so a precheck that needs one module does not
pay for, or depend on, the rest.
"""
