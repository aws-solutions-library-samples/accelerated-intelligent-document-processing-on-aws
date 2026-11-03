# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tenant-isolation models: declarations, plans, audit findings and live checks."""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class TenantInfo(BaseModel):
    """One tenant declaration, as stored in the stack's ConfigurationTable."""

    tenant_id: str = Field(description="Lowercase identifier of the tenant")
    prefixes: List[str] = Field(
        default_factory=list,
        description="Input-bucket prefix subtrees the tenant owns, each ending in '/'",
    )
    profile_pattern: str = Field(
        description="Configuration Profile pattern the tenant owns, e.g. 'acme-*'"
    )
    description: Optional[str] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class TenantListResult(BaseModel):
    """Every tenant declared on a stack, and the stack's isolation mode."""

    tenants: List[TenantInfo] = Field(default_factory=list)
    count: int = 0
    isolation_mode: str = Field(
        description=(
            "The stack's TenantIsolationMode parameter: 'Off' or 'Strict'. "
            "'Unknown' when the stack declares no such parameter (a stack deployed "
            "before tenant isolation existed, which behaves as 'Off')."
        )
    )


class TenantPlanAction(str, Enum):
    """What applying a declarations file would do to one tenant."""

    CREATE = "create"
    UPDATE = "update"
    UNCHANGED = "unchanged"
    DELETE = "delete"
    KEEP = "keep"


class TenantPlanItem(BaseModel):
    tenant_id: str
    action: TenantPlanAction
    before: Optional[TenantInfo] = None
    after: Optional[TenantInfo] = None
    error: Optional[str] = Field(
        default=None,
        description="Why this change would be refused, when it would be",
    )


class TenantApplyResult(BaseModel):
    """The plan for a declarations file, and whether it was written."""

    plan: List[TenantPlanItem] = Field(default_factory=list)
    applied: bool = Field(
        default=False,
        description="False for a dry run, and for a plan containing any refusal",
    )


class TenantPolicyResult(BaseModel):
    """A generated CloudFormation template granting tenant principals S3 access."""

    template: str = Field(description="The CloudFormation template, as YAML")
    access_points: List[str] = Field(
        default_factory=list, description="Access point names the template creates"
    )
    warnings: List[str] = Field(default_factory=list)


class TenantAuditFinding(BaseModel):
    """One thing the audit found that weakens tenant isolation."""

    kind: str = Field(description="Machine-readable category, e.g. 'crossing-mapping'")
    subject: str = Field(description="The mapping, user, document or resource")
    detail: str = Field(description="What is wrong, written for a person")


class TenantAuditResult(BaseModel):
    isolation_mode: str
    tenant_count: int = 0
    findings: List[TenantAuditFinding] = Field(default_factory=list)
    notes: List[str] = Field(
        default_factory=list,
        description="What the audit could not examine, and why",
    )

    @property
    def clean(self) -> bool:
        return not self.findings


class TenantVerifyOutcome(str, Enum):
    """The outcome of one live check. Only PASS is a pass."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"


class TenantVerifyCheck(BaseModel):
    name: str
    outcome: TenantVerifyOutcome
    detail: str = ""


class TenantVerifyResult(BaseModel):
    """The live cross-tenant check, one row per assertion."""

    isolation_mode: str
    checks: List[TenantVerifyCheck] = Field(default_factory=list)

    @property
    def outcome(self) -> TenantVerifyOutcome:
        """FAIL beats ERROR beats SKIP beats PASS: the worst row decides.

        A run with no rows is ERROR rather than PASS — nothing was checked.
        """
        if not self.checks:
            return TenantVerifyOutcome.ERROR
        outcomes = {c.outcome for c in self.checks}
        for worst in (
            TenantVerifyOutcome.FAIL,
            TenantVerifyOutcome.ERROR,
            TenantVerifyOutcome.SKIP,
        ):
            if worst in outcomes:
                return worst
        return TenantVerifyOutcome.PASS
