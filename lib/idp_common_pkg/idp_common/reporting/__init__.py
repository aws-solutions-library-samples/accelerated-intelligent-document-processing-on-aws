# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Reporting module for saving document data to reporting storage.

``SaveReportingData`` is imported on first use rather than here, so that
``idp_common.reporting.pricing`` can be imported without pyarrow, which only the
Parquet writer needs.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .save_reporting_data import SaveReportingData

__all__ = ["SaveReportingData"]


def __getattr__(name: str) -> Any:
    if name == "SaveReportingData":
        from .save_reporting_data import SaveReportingData

        return SaveReportingData
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
