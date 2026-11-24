# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``review_queues.draft_diff``: leaf values and the fields a draft changed.

The module is light (standard library only) and is the one place both the
service's draft save and the apply-review step read leaf paths from.
"""

import ast
import sys
from pathlib import Path

import pytest

from idp_common.field_paths import extract_path
from idp_common.review_queues import draft_diff, service
from idp_common.review_queues.draft_diff import changed_fields, leaf_values

_RESULT = {
    "Vendor": {"Name": "Acme", "Address": {"City": "Bonn"}},
    "Lines": [{"Amount": 1, "Tags": ["a", "b"]}, {"Amount": None}],
    "Total": 6,
}


@pytest.mark.unit
class TestLeafValues:
    def test_every_leaf_is_listed_by_its_path(self):
        assert leaf_values(_RESULT) == {
            "Vendor.Name": "Acme",
            "Vendor.Address.City": "Bonn",
            "Lines[0].Amount": 1,
            "Lines[0].Tags[0]": "a",
            "Lines[0].Tags[1]": "b",
            "Lines[1].Amount": None,
            "Total": 6,
        }

    def test_a_path_reads_the_same_value_through_field_paths(self):
        leaves = leaf_values(_RESULT)

        assert leaves
        for path, value in leaves.items():
            assert extract_path(_RESULT, path) == value, path

    def test_an_empty_container_has_no_leaves(self):
        assert leaf_values({}) == {}
        assert leaf_values([]) == {}
        assert leaf_values({"Lines": [], "Vendor": {}}) == {}

    def test_a_prefix_is_put_in_front_of_every_path(self):
        assert leaf_values({"a": [1]}, "root") == {"root.a[0]": 1}


@pytest.mark.unit
class TestChangedFields:
    def test_changed_added_and_removed_leaves_are_listed_in_order(self):
        base = {"inference_result": {"B": 1, "A": 1, "Gone": 1, "Same": 1}}
        new = {"inference_result": {"B": 2, "A": "1", "Added": 1, "Same": 1}}

        assert changed_fields(base, new) == ["A", "Added", "B", "Gone"]

    def test_a_changed_list_row_is_named_by_its_index(self):
        base = {"inference_result": {"Lines": [{"Amount": 1}, {"Amount": 2}]}}
        new = {"inference_result": {"Lines": [{"Amount": 1}, {"Amount": 5}, {"X": 1}]}}

        assert changed_fields(base, new) == ["Lines[1].Amount", "Lines[2].X"]

    def test_equal_results_have_no_changed_fields(self):
        result = {"inference_result": _RESULT}

        assert changed_fields(result, {"inference_result": dict(_RESULT)}) == []

    def test_only_the_inference_result_is_compared(self):
        base = {"inference_result": {"A": 1}, "metadata": {"t": 1}}
        new = {"inference_result": {"A": 1}, "metadata": {"t": 2}, "extra": True}

        assert changed_fields(base, new) == []

    @pytest.mark.parametrize("base", [None, {}, [], "text", {"other": 1}])
    def test_a_base_with_no_result_makes_every_new_leaf_a_change(self, base):
        new = {"inference_result": {"A": 1, "B": {"C": 2}}}

        assert changed_fields(base, new) == ["A", "B.C"]
        assert changed_fields(new, base) == ["A", "B.C"]


@pytest.mark.unit
class TestOneImplementation:
    def test_the_service_uses_the_module_function(self):
        assert service.changed_fields is draft_diff.changed_fields

    def test_the_service_keeps_no_copy_of_its_own(self):
        assert not hasattr(service, "_leaf_paths")
        tree = ast.parse(Path(service.__file__).read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert not defined & {"_leaf_paths", "leaf_values", "changed_fields"}

    def test_the_module_imports_the_standard_library_only(self):
        tree = ast.parse(Path(draft_diff.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                # A relative import would reach into the package.
                assert node.level == 0, ast.dump(node)
                imported.add((node.module or "").split(".")[0])

        assert imported
        assert imported <= set(sys.stdlib_module_names), imported
