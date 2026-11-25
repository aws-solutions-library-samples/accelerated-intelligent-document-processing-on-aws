# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp_common.field_values`` and ``idp_common.field_paths`` stay light.

Review routing reads extracted values, missing required fields and field paths in a
Lambda that installs neither the assessment extra (Pillow) nor the rule validation
extra (z3). Both modules therefore must import without ``idp_common.assessment``,
``idp_common.rule_validation``, ``PIL`` or ``z3``. The unit suite installs every
extra, so only a child interpreter that refuses those imports can see a regression.
"""

import json
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys

import pytest

_BLOCKED = ["idp_common.assessment", "idp_common.rule_validation", "PIL", "z3"]

_CHILD = r"""
import importlib.abc
import json
import sys

BLOCKED = json.loads(sys.argv[1])


class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        for blocked in BLOCKED:
            if name == blocked or name.startswith(blocked + "."):
                raise ModuleNotFoundError(f"No module named '{name}'")
        return None


sys.meta_path.insert(0, Refuse())

from idp_common.field_paths import (
    FieldPathError,
    coerce_numeric_reading,
    exact_numeric_reading,
    extract_path,
)
from idp_common.field_values import (
    collect_missing_required_fields,
    has_extracted_value,
)

schema = {
    "type": "object",
    "required": ["Total", "Vendor"],
    "properties": {
        "Total": {"type": "number"},
        "Vendor": {"type": "string"},
        "Lines": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["Amount"],
                "properties": {"Amount": {"type": "number"}},
            },
        },
    },
}
data = {"Total": 12.5, "Vendor": "  ", "Lines": [{"Amount": 3}, {"Amount": None}]}
try:
    extract_path(data, "[0].Amount")
    malformed = "accepted"
except FieldPathError:
    malformed = "refused"
print(
    json.dumps(
        {
            "present": has_extracted_value({"a": [0]}),
            "absent": has_extracted_value({"a": ["", None]}),
            "missing": [
                entry["attribute_name"]
                for entry in collect_missing_required_fields(data, schema)
            ],
            "path": extract_path(data, "Lines[0].Amount"),
            "last": extract_path(data, "Lines[-1].Amount"),
            "miss": extract_path(data, "Lines[7].Amount"),
            "malformed": malformed,
            "exact": exact_numeric_reading("30.0", "Int"),
            "coerced": coerce_numeric_reading("0.5", "Real"),
            "heavy": sorted(
                name
                for name in sys.modules
                if any(name == b or name.startswith(b + ".") for b in BLOCKED)
            ),
        }
    )
)
"""


def _run_child(blocked):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", _CHILD, json.dumps(blocked)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


@pytest.mark.unit
class TestLightValueModules:
    def test_both_modules_work_without_the_assessment_and_rule_validation_extras(self):
        run = _run_child(_BLOCKED)
        assert run.returncode == 0, run.stderr
        result = json.loads(run.stdout.strip().splitlines()[-1])
        assert result == {
            "present": True,
            "absent": False,
            "missing": ["Vendor", "Lines[1].Amount"],
            "path": 3,
            "last": None,
            "miss": None,
            "malformed": "refused",
            "exact": 30,
            "coerced": 0.5,
            "heavy": [],
        }

    def test_the_child_refuses_a_blocked_import(self):
        # Non-vacuous: the finder really does stop the packages it names, so a
        # heavy import added to either module fails the case above.
        probe = _CHILD.replace(
            "from idp_common.field_paths import (",
            "import idp_common.assessment\nfrom idp_common.field_paths import (",
            1,
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        run = subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-c", probe, json.dumps(_BLOCKED)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        assert run.returncode != 0
        assert "idp_common.assessment" in run.stderr

    def test_the_old_locations_re_export_the_same_objects(self):
        from idp_common import field_paths, field_values
        from idp_common.assessment import batching, presence
        from idp_common.rule_validation.z3 import type_coercion

        for name in (
            "has_extracted_value",
            "iter_assessed_leaf_values",
            "absent_value_paths",
            "exclude_absent_value_alerts",
            "collect_missing_required_fields",
        ):
            assert getattr(presence, name) is getattr(field_values, name), name
        assert batching._is_populated_value is field_values._is_populated_value
        for name in (
            "NUMERIC_TYPES",
            "exact_numeric_reading",
            "coerce_numeric_reading",
        ):
            assert getattr(type_coercion, name) is getattr(field_paths, name), name

    def test_presence_re_exports_public_names_only(self):
        """The private helpers are reached through ``field_values`` alone.

        A private name on ``presence`` would be a second place to patch that no
        caller reads, so a patch of it would change nothing and say nothing.
        """
        from idp_common.assessment import presence

        private = [
            name
            for name in vars(presence)
            if name.startswith("_") and not name.startswith("__")
        ]

        assert private == []
        assert [name for name in presence.__all__ if name.startswith("_")] == []
