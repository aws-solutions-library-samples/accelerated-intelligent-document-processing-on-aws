# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``field_paths.set_path``: write a value where a path already leads.

It creates nothing on the way. A caller that re-applies a reviewer's correction
to a fresh extraction relies on that: a correction whose row or group is no
longer there is reported as not applied, and the result is left as extracted.
"""

import copy

import pytest

from idp_common import field_paths
from idp_common.field_paths import FieldPathError, extract_path, set_path


def _data():
    return {
        "Vendor": {"Name": "Acme", "Address": {"City": "Bonn"}},
        "rows": [{"amount": 1}, {"amount": 2}, {"amount": 3}],
        "grid": [[1, 2], [3, 4]],
        "Total": 6,
        "Note": "text",
    }


@pytest.mark.unit
class TestSetPathWrites:
    def test_a_dotted_key_is_replaced(self):
        data = _data()

        assert set_path(data, "Vendor.Address.City", "Berlin") is True

        assert data["Vendor"]["Address"]["City"] == "Berlin"
        # Nothing else moved.
        expected = _data()
        expected["Vendor"]["Address"]["City"] = "Berlin"
        assert data == expected

    def test_a_top_level_key_is_replaced(self):
        data = _data()

        assert set_path(data, "Total", 7) is True

        assert data["Total"] == 7

    def test_a_missing_leaf_key_is_added_to_the_dict_that_exists(self):
        data = _data()

        assert set_path(data, "Vendor.TaxId", "DE1") is True

        assert data["Vendor"] == {
            "Name": "Acme",
            "Address": {"City": "Bonn"},
            "TaxId": "DE1",
        }

    def test_a_key_under_a_list_element_is_replaced(self):
        data = _data()

        assert set_path(data, "rows[1].amount", 20) is True

        assert data["rows"] == [{"amount": 1}, {"amount": 20}, {"amount": 3}]

    def test_the_last_index_replaces_the_element(self):
        data = _data()

        assert set_path(data, "rows[1]", {"amount": 9}) is True
        assert set_path(data, "grid[0][1]", 22) is True

        assert data["rows"] == [{"amount": 1}, {"amount": 9}, {"amount": 3}]
        assert data["grid"] == [[1, 22], [3, 4]]

    def test_a_negative_index_counts_from_the_end(self):
        data = _data()

        assert set_path(data, "rows[-1].amount", 30) is True
        assert set_path(data, "grid[-2][-1]", 0) is True

        assert data["rows"][2] == {"amount": 30}
        assert data["grid"] == [[1, 0], [3, 4]]

    def test_a_written_value_reads_back_by_the_same_path(self):
        data = _data()

        for path, value in (("rows[0].amount", None), ("Vendor.Name", ["a"])):
            assert set_path(data, path, value) is True
            assert extract_path(data, path) == value


@pytest.mark.unit
class TestSetPathMisses:
    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("rows[3].amount", id="index-out-of-range-on-the-way"),
            pytest.param("rows[3]", id="last-index-out-of-range"),
            pytest.param("rows[-4]", id="negative-index-out-of-range"),
            pytest.param("grid[0][2]", id="inner-index-out-of-range"),
            pytest.param("Vendor.Contact.Email", id="missing-intermediate-dict"),
            pytest.param("Lines[0].Amount", id="missing-list"),
            pytest.param("Lines[0]", id="missing-list-as-the-last-step"),
            pytest.param("Note.Length", id="a-string-is-not-a-dict"),
            pytest.param("Total[0]", id="a-number-is-not-a-list"),
            pytest.param("Vendor[0]", id="a-dict-is-not-a-list"),
            pytest.param("rows.amount", id="a-list-is-not-a-dict"),
        ],
    )
    def test_a_miss_is_false_and_leaves_the_data_unchanged(self, path):
        data = _data()
        before = copy.deepcopy(data)

        assert set_path(data, path, "x") is False

        assert data == before

    def test_a_missing_intermediate_container_is_not_created(self):
        # The case the function exists for: a correction under a group the new
        # extraction does not have must not invent that group.
        data = {"Vendor": {"Name": "Acme"}}

        assert set_path(data, "Vendor.Address.City", "Bonn") is False

        assert data == {"Vendor": {"Name": "Acme"}}
        assert "Address" not in data["Vendor"]

    @pytest.mark.parametrize("data", [None, [], "text", 3])
    def test_data_that_is_not_a_dict_is_a_miss(self, data):
        assert set_path(data, "a", 1) is False
        assert set_path(data, "a.b", 1) is False


@pytest.mark.unit
class TestSetPathMalformed:
    @pytest.mark.parametrize("path", ["", None, 3, "[0].amount", "rows.[1].amount"])
    def test_a_malformed_path_raises_whatever_the_data_holds(self, path):
        data = _data()
        before = copy.deepcopy(data)

        with pytest.raises(FieldPathError):
            set_path(data, path, "x")
        with pytest.raises(FieldPathError):
            set_path(None, path, "x")

        assert data == before

    def test_a_malformed_later_segment_is_not_masked_by_an_earlier_miss(self):
        with pytest.raises(FieldPathError):
            set_path({}, "missing.[0]", "x")


@pytest.mark.unit
def test_set_path_is_exported():
    assert "set_path" in field_paths.__all__
