# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`docs/configuration.md` points at `system_defaults/` as the canonical key list.

That claim is load-bearing in two directions, which is why it is pinned here rather
than left as prose:

* **For readers.** `configuration.md` is organized by topic and does not describe
  every key — a newly added option often lands in `system_defaults/` and in a
  feature doc, and never in `configuration.md`. Measured at the time this was
  written: of the release's new options, `configuration.md` mentioned **none** of
  `multi_instance_detection`, `forced_tool`, `restate_schema_in_system_prompt` or
  `contextPagesCount`. The fix was to stop implying the page is exhaustive and name
  the directory that is.

* **For agents reading the bundled source.** The accelerator's `docs/`, `lib/` and
  `config_library/` trees are shipped read-only inside the auto-optimizer extension
  image, which greps and reads them to decide what to tune. A table that names a
  file that has been renamed sends it to a dead path, and a defaults file absent
  from the table is a setting it will not find.

So: the mapping must stay complete in both directions.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_REPO = pathlib.Path(__file__).resolve().parents[5]
_DEFAULTS = (
    _REPO / "lib" / "idp_common_pkg" / "idp_common" / "config" / "system_defaults"
)
_DOC = _REPO / "docs" / "configuration.md"

#: The two inventories that live in the directory itself. Same both-directions rule as
#: the published page: a stale name here sends a reader to a deleted module, and a
#: module missing from the list is one they will not find. Both carried
#: ``base-assessment.yaml`` for a release after it was deleted, alongside the
#: ``pattern-1.yaml`` ``_inherits`` entry that was the actual break (#1203) — the same
#: omission in three places, which is what a check over one file would have caught.
_IN_TREE_INVENTORIES = (
    _DEFAULTS / "README.md",
    _DEFAULTS / "__init__.py",
)

_FILENAME_RE = r"(?:base|pattern)-?[a-z0-9-]*\.yaml"


def _named_in_doc() -> set[str]:
    """Every ``base-*.yaml`` / ``pattern-*.yaml`` named in backticks in the doc."""
    return set(re.findall(f"`({_FILENAME_RE})`", _DOC.read_text()))


def _named_in(path: pathlib.Path) -> set[str]:
    """Every defaults filename named anywhere in ``path``.

    Unbracketed, because these two inventories are a fenced directory listing and a
    module docstring rather than prose with inline code spans.
    """
    return set(re.findall(_FILENAME_RE, path.read_text()))


def test_every_defaults_file_is_named_in_the_configuration_doc():
    """A defaults file missing from the table is a whole stage's settings that a
    reader — or the extension agent grepping the bundled docs — will not discover."""
    on_disk = {p.name for p in _DEFAULTS.glob("*.yaml")}
    assert on_disk, f"no defaults files found under {_DEFAULTS}"
    missing = sorted(on_disk - _named_in_doc())
    assert not missing, (
        "these system_defaults files are not named in docs/configuration.md, so "
        f"their settings are undiscoverable from it: {missing}"
    )


def test_every_file_named_in_the_doc_exists():
    """The other direction: a renamed defaults file leaves the doc pointing at a
    path that is not there, which is worse than saying nothing."""
    stale = sorted(n for n in _named_in_doc() if not (_DEFAULTS / n).exists())
    assert not stale, (
        f"docs/configuration.md names defaults files that do not exist: {stale}"
    )


@pytest.mark.parametrize("inventory", _IN_TREE_INVENTORIES, ids=lambda p: p.name)
def test_every_defaults_file_is_named_in_the_in_tree_inventory(
    inventory: pathlib.Path,
):
    """The directory's own two lists are read more often than the published page."""
    on_disk = {p.name for p in _DEFAULTS.glob("*.yaml")}
    assert on_disk, f"no defaults files found under {_DEFAULTS}"
    missing = sorted(on_disk - _named_in(inventory))
    assert not missing, (
        f"{inventory.name} does not list these system_defaults files: {missing}"
    )


@pytest.mark.parametrize("inventory", _IN_TREE_INVENTORIES, ids=lambda p: p.name)
def test_the_in_tree_inventory_names_no_file_that_is_gone(inventory: pathlib.Path):
    """The direction that failed: a name kept after the module was deleted."""
    stale = sorted(n for n in _named_in(inventory) if not (_DEFAULTS / n).exists())
    assert not stale, (
        f"{inventory.name} names defaults files that do not exist: {stale}"
    )


def test_the_doc_disclaims_being_exhaustive_and_points_somewhere_useful():
    """The specific failure this guards is a reader concluding an option does not
    exist because this page does not mention it."""
    text = _DOC.read_text()
    assert "not** an exhaustive key reference" in text, (
        "docs/configuration.md must not imply it lists every setting"
    )
    assert "config-guidance.md" in text, (
        "it should point at the measured guidance paper for which settings to change"
    )
