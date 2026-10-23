# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The UI's list of pre-0.9 classification keys matches the migration's.

The editor tells a user importing an old file which keys will be converted
(`src/ui/src/utils/classification-config.ts`). A key the migration converts but
the UI does not list would be converted with no notice.
"""

import re
from pathlib import Path

import pytest

from idp_common.config.migrations.v08_to_v09 import FLAT_KEYS

REPO = Path(__file__).resolve().parents[2]
UI_FILE = REPO / "src/ui/src/utils/classification-config.ts"


@pytest.mark.unit
def test_the_ui_lists_exactly_the_keys_the_migration_converts():
    source = UI_FILE.read_text()
    start = source.index("export const FLAT_CLASSIFICATION_KEYS")
    end = source.index("] as const;", start)
    ui_keys = set(re.findall(r"'(\w+)'", source[start:end]))
    # classificationMethod is converted by the hop before (v07_to_v08).
    assert ui_keys == set(FLAT_KEYS) | {"classificationMethod"}
