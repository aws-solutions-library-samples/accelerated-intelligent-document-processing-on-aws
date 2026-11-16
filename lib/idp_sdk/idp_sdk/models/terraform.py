# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Models for moving a ``genai-idp-terraform`` deployment onto the built-in module."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TerraformMigrationResource(BaseModel):
    """One resource of the old deployment and what the migration does with it."""

    old_address: str = Field(description="Address in the old deployment's state")
    old_type: str = Field(description="Terraform resource type in the old state")
    kind: str = Field(description="Resource kind, e.g. 'input_bucket'")
    verdict: str = Field(
        description="adopt, adopt-after-fix, adopt-by-parameter, recreate, "
        "keep-unmanaged or bucket-or-table-setting"
    )
    evidence: str = Field(
        description="'measured' when a live migration exercised it, else 'inferred'"
    )
    note: str
    physical_id: Optional[str] = None
    new_address: Optional[str] = Field(
        default=None, description="Address in the new deployment's state, when adopted"
    )
    import_id: Optional[str] = Field(
        default=None, description="The id its import block (or terraform import) uses"
    )
    detach: bool = Field(
        default=False,
        description="Removed from the old state, so destroying the old deployment "
        "leaves it alone",
    )
    import_in_adopt_tf: bool = Field(
        default=True,
        description="False for the user pool, which the adopt-user-pool phase imports",
    )


class TerraformMigrationPlanResult(BaseModel):
    """What the ``plan`` phase found and wrote."""

    terraform_dir: str
    resources: List[TerraformMigrationResource] = Field(default_factory=list)
    parameters: Dict[str, str] = Field(
        default_factory=dict,
        description="Stack parameters written to terraform.tfvars, by CloudFormation "
        "name: yours plus the ones the old deployment requires",
    )
    detach_addresses: List[str] = Field(default_factory=list)
    detach_commands: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    files: Dict[str, str] = Field(
        default_factory=dict, description="What was written, by role"
    )

    @property
    def import_count(self) -> int:
        return sum(1 for r in self.resources if r.import_id and r.import_in_adopt_tf)


class TerraformMigrationStepResult(BaseModel):
    """The outcome of one phase after ``plan``.

    ``changes`` lists exactly what the phase changes (in a dry run, what it would
    change); ``findings`` lists what blocks the next step. ``ok`` is false when
    there is any finding.
    """

    phase: str
    applied: bool = Field(
        description="True when the changes were made, not only listed"
    )
    ok: bool
    changes: List[str] = Field(default_factory=list)
    findings: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    back_out: List[str] = Field(
        default_factory=list, description="How to undo this phase's changes"
    )
    backup_path: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
