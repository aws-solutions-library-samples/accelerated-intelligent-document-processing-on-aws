# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Launch-time integrity check: the config FILE must match the index that names it.

This exists because of a real, silently-wrong benchmark comparison. Suites share
cell names (``core_cells`` backs corefast, core and coresynth), and config files
were named ``<cell>__<class>.yaml`` with no suite in the name. So:

    make_configs.py --suite coresynth --set extraction_model=sonnet5   # writes files + index
    make_configs.py --suite corefast                                   # OVERWRITES the same files

left coresynth's index advertising ``extraction_model: sonnet5`` while the file it
pointed at was pinned to sonnet-4-6. The run then executed on sonnet-4-6, recorded
"sonnet5" in its metadata, and the resulting before/after comparison silently
spanned two different models — every number attributed to the wrong configuration,
with nothing in the output looking unusual.

Filenames are namespaced per suite now, and this check is the backstop for any
other way the two can drift (hand-edited file, partial rebuild, stale index).
"""

import sys

import pytest
import yaml

sys.path.insert(0, "benchmarks/harness")


@pytest.fixture(scope="module")
def rm():
    import run_matrix

    return run_matrix


def _cell(tmp_path, resolved, cfg):
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return [{"cell": "c1", "path": str(p), "resolved": resolved}]


@pytest.mark.unit
def test_matching_config_passes(rm, tmp_path, capsys):
    cells = _cell(
        tmp_path,
        {"extraction_model": "sonnet5"},
        {"extraction": {"model": "us.anthropic.claude-sonnet-5"}},
    )
    rm.verify_config_axes(cells)
    assert "match their index" in capsys.readouterr().out


@pytest.mark.unit
def test_model_mismatch_aborts(rm, tmp_path):
    """The exact bug: index says sonnet5, file is pinned to sonnet-4-6."""
    cells = _cell(
        tmp_path,
        {"extraction_model": "sonnet5"},
        {"extraction": {"model": "us.anthropic.claude-sonnet-4-6"}},
    )
    with pytest.raises(SystemExit) as e:
        rm.verify_config_axes(cells)
    msg = str(e.value)
    assert "extraction.model" in msg
    assert "sonnet-4-6" in msg and "sonnet-5" in msg


@pytest.mark.unit
def test_reshaped_knob_is_not_a_false_positive(rm, tmp_path):
    """``ocr.features`` is written as [{name: X}], not the raw axis list.

    The check compares against what make_configs.set_path would actually write,
    so a shape transform can never be mistaken for a value difference.
    """
    cells = _cell(
        tmp_path,
        {"ocr": "textract_tables"},
        {
            # The axis sets all three of these; ocr.features is the reshaped one.
            "use_bda": False,
            "ocr": {
                "backend": "textract",
                "features": [{"name": "TABLES"}, {"name": "LAYOUT"}],
            },
        },
    )
    rm.verify_config_axes(cells)  # must not raise


@pytest.mark.unit
def test_missing_config_file_aborts(rm, tmp_path):
    cells = [
        {"cell": "c1", "path": str(tmp_path / "nope.yaml"), "resolved": {"ocr": "bda"}}
    ]
    with pytest.raises(SystemExit) as e:
        rm.verify_config_axes(cells)
    assert "missing" in str(e.value)


@pytest.mark.unit
def test_unknown_axis_value_is_skipped_not_failed(rm, tmp_path):
    """An axis the matrix doesn't express as config paths can't be checked."""
    cells = _cell(
        tmp_path, {"not_a_real_axis": "whatever"}, {"extraction": {"model": "x"}}
    )
    rm.verify_config_axes(cells)  # must not raise


@pytest.mark.unit
def test_generated_configs_are_suite_namespaced():
    """Two suites must not be able to overwrite each other's config files."""
    import make_configs

    assert "args.suite" in open(make_configs.__file__).read(), (
        "the config filename must include the suite, or two suites sharing a cell "
        "name will overwrite each other's files and desync from their indexes"
    )
