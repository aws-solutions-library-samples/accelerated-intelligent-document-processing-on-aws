# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The UI's deny marker is the one the grant writers store.

`NO_TENANT_GRANTED` in `idp_common/user_scope.py` is the scope written for a user
granted no tenant. `src/ui/src/hooks/use-tenants.ts` carries a hand-maintained copy
so User Management can show it as "No tenant granted". If the two drift the UI
renders the raw marker as though it were a profile name, which is the outcome
`UserManagementLayout.tenantGrants.test.tsx` exists to prevent, and that test
cannot see a drift because it uses the TypeScript copy on both sides.
"""

from __future__ import annotations

import re
from pathlib import Path

from idp_common.user_scope import NO_TENANT_GRANTED

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "src/ui/src/hooks/use-tenants.ts"


def test_the_ui_copy_matches_the_python_constant():
    found = re.findall(
        r"export const NO_TENANT_GRANTED = '([^']*)';", HOOK.read_text("utf-8")
    )
    assert found == [NO_TENANT_GRANTED], found
