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


def _named_in_doc() -> set[str]:
    """Every ``base-*.yaml`` / ``pattern-*.yaml`` named in backticks in the doc."""
    return set(re.findall(r"`((?:base|pattern)-?[a-z0-9-]*\.yaml)`", _DOC.read_text()))


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
