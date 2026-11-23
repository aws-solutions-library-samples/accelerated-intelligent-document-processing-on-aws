# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Models for the fit check (``client.fit_check``)."""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class FitCheckResult(BaseModel):
    """Where a fit check wrote its report, and what it found."""

    output_dir: str = Field(description="Directory holding the report files")
    report_path: str = Field(description="Path of fit-report.json")
    markdown_path: str = Field(description="Path of fit-report.md")
    record_path: Optional[str] = Field(
        default=None,
        description="Path of fit-run-record.json, from which --from-record rebuilds "
        "the report offline",
    )
    report: Dict[str, Any] = Field(description="The fit-report.json content")
    config_profile: Optional[str] = Field(
        default=None, description="The configuration profile the samples ran under"
    )
    batch_id: Optional[str] = Field(default=None, description="The upload batch id")
    active_profile_unchanged: Optional[bool] = Field(
        default=None,
        description="Whether the active profile's name and content, and "
        "Config#default's content, were the same after the run as before it",
    )
    guard: Dict[str, Any] = Field(
        default_factory=dict,
        description="The before and after names and SHA-256 hashes the check compared",
    )
    cleanup: Optional[Dict[str, Any]] = Field(
        default=None, description="What --cleanup removed, when it ran"
    )
