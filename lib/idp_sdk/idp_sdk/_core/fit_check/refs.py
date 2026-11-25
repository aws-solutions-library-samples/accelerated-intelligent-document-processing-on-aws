# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The solution-profile refs the fit check can report.

Each is an id in ``solution-profile/limitations.yaml`` of the release this code
ships in; ``scripts/tests/test_fit_report_contract.py`` resolves every one of them
there, so a ref cannot be invented or outlive the item it names.
"""

UNSUPPORTED_FORMAT = "limitation:unsupported-file-format"
LEGACY_OFFICE = "limitation:legacy-office-formats"
SIMPLE_ROW_LIMIT = "limitation:simple-extraction-row-limit"
SIMPLE_PAGE_LIMIT = "limitation:simple-extraction-page-limit"
PACKET_LONG_CONTEXT = "limitation:packet-classification-long-context"
LANGUAGE_SCRIPT = "limitation:language-script-support"

ALL_REFS = (
    UNSUPPORTED_FORMAT,
    LEGACY_OFFICE,
    SIMPLE_ROW_LIMIT,
    SIMPLE_PAGE_LIMIT,
    PACKET_LONG_CONTEXT,
    LANGUAGE_SCRIPT,
)
