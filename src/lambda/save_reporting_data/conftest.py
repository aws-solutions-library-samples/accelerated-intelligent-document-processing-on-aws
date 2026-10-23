# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Suite-local pytest configuration."""


# --- No outbound AWS from this suite -----------------------------------------
# Measured in #1398: this suite issued requests that reached botocore's real send,
# signed with whatever credentials the machine could resolve, and nothing failed or
# reported it — each call sits inside a production `except Exception`. Stripping the
# AWS environment (which `make test` now does) stops a request only while no suite
# reinstates placeholder credentials and no test clears the environment; the refusal
# below is the half that cannot be undone from inside a test. One definition, walked
# up to rather than copied: scripts/tests/no_outbound_aws.py carries the reasoning,
# the seam and the measurements, and the ``integration``-marker carve-out.
def _no_outbound_aws_fixture():
    import sys as _sys
    from pathlib import Path as _Path

    for ancestor in _Path(__file__).resolve().parents:
        helper = ancestor / "scripts" / "tests"
        if not (helper / "no_outbound_aws.py").is_file():
            continue
        _sys.path.insert(0, str(helper))
        try:
            from no_outbound_aws import fixture

            return fixture()
        finally:
            _sys.path.pop(0)
    raise RuntimeError(
        "scripts/tests/no_outbound_aws.py not found above this file; it carries the "
        "control that stops this suite transacting with a real AWS account (#1398)"
    )


no_outbound_aws = _no_outbound_aws_fixture()
