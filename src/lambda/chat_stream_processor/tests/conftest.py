# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test configuration for chat_stream_processor.

Pins fake AWS credentials and a region so the suite is **hermetic**. Following
the same pattern as the sibling suites (`chat_with_document_processor`,
`bda_ocr_project`, `send_chat_document_message_resolver`, `idp_common_pkg`, …).

Why this file was needed: `test_doc_processor_set_sink_redirects_emission`
imports `chat_with_document_processor/index.py`, which constructs a boto3 client
at module scope. That suite has its own conftest pinning a region — this one did
not, so the test passed only on machines where a region happened to be
configured in the ambient environment (`AWS_REGION`, `AWS_PROFILE`, or
`~/.aws/config`) and failed with `botocore.exceptions.NoRegionError` anywhere
else. It surfaced when the package/Lambda suites were first gated in CI, where no
AWS configuration exists.

Also note the test is `skipif(find_spec("idp_common") is None)`, so it only
*runs* once the first-party packages are installed — which is why enabling the CI
install step is what exposed this.

`setdefault` throughout, so a real environment (e.g. an integration run) still
wins.
"""

from __future__ import annotations

import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
