# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``scripts/check_first_party_deps.py`` answers WHICH source tree, not just "source".

That script runs in both CIs and in ``make setup`` / ``make setup-venv`` /
``make install-first-party``, and it is the control a reader reaches for when asking
whether the first-party packages are the ones in this checkout. It answered a narrower
question — source versus package index — and so stayed green while ``idp_common``
resolved into another worktree of this repository and four more into a different project
(#1094). Both are ``file://`` installs, so PEP 610 is satisfied either way.

The cases below are paired on purpose. A check that reports every editable install as
foreign would pass a one-sided test just as happily as a correct one, and would be
disabled by the first person it inconvenienced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_first_party_deps as checker  # noqa: E402

sys.path.pop(0)

pytestmark = pytest.mark.unit


def _checkout(root: Path, *, git_is_file: bool = False) -> Path:
    """A directory that looks like a checkout, with the ``.git`` entry git would make."""
    root.mkdir(parents=True, exist_ok=True)
    if git_is_file:
        # What git writes in a WORKTREE: a file, not a directory.
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    else:
        (root / ".git").mkdir(exist_ok=True)
    return root


@pytest.fixture
def here(tmp_path, monkeypatch) -> Path:
    """Pretend this script belongs to a synthetic checkout, so paths are controllable."""
    root = _checkout(tmp_path / "here")
    monkeypatch.setattr(checker, "THIS_CHECKOUT", root)
    monkeypatch.setattr(checker, "CAN_COMPARE_CHECKOUTS", True)
    return root


@pytest.fixture
def record(monkeypatch):
    """Stand in for one distribution's PEP 610 ``direct_url.json``."""

    def _record(info: dict | None) -> None:
        monkeypatch.setattr(checker, "_direct_url", lambda name: info)

    return _record


def _editable(path: Path) -> dict:
    return {"url": path.as_uri(), "dir_info": {"editable": True}}


class TestWhichTree:
    def test_an_editable_pointer_into_this_checkout_is_ok(self, here, record):
        record(_editable(here / "lib" / "idp_common_pkg"))
        status, _ = checker._classify("idp_common")
        assert status == "ok"

    def test_an_editable_pointer_into_another_checkout_is_a_finding(
        self, here, record, tmp_path
    ):
        theirs = _checkout(tmp_path / "theirs")
        record(_editable(theirs / "lib" / "idp_common_pkg"))
        status, detail = checker._classify("idp_common")
        assert status == "foreign"
        # Both trees named: "wrong tree" without saying which leaves the reader where
        # they started.
        assert str(theirs) in detail
        assert str(here) in detail

    def test_a_worktree_nested_inside_this_checkout_is_still_foreign(
        self, here, record
    ):
        """Identity, not ancestry — the case an ancestry test accepts.

        A git worktree of this repository lives at ``<root>/.claude/worktrees/<name>/``,
        a path the tooling here creates, and it is a different revision. Asking only
        whether the recorded path is *under* this root accepts exactly the layout the
        live foreign pointer on the machine this was written on actually had.
        """
        nested = _checkout(here / ".claude" / "worktrees" / "agent-x", git_is_file=True)
        record(_editable(nested / "lib" / "idp_common_pkg"))
        assert checker._classify("idp_common")[0] == "foreign"

    def test_a_non_editable_local_install_is_not_compared(self, here, record, tmp_path):
        """Its code was copied at install time, so the path says nothing about now."""
        theirs = _checkout(tmp_path / "theirs")
        record({"url": (theirs / "lib" / "idp_common_pkg").as_uri(), "dir_info": {}})
        assert checker._classify("idp_common")[0] == "ok"

    def test_without_a_git_entry_the_comparison_is_skipped(
        self, record, tmp_path, monkeypatch
    ):
        """`make setup` runs in an exported archive; a false failure there is worse."""
        exported = tmp_path / "exported"
        exported.mkdir()
        monkeypatch.setattr(checker, "THIS_CHECKOUT", exported)
        monkeypatch.setattr(checker, "CAN_COMPARE_CHECKOUTS", False)
        record(_editable(_checkout(tmp_path / "theirs") / "lib" / "idp_common_pkg"))
        assert checker._classify("idp_common")[0] == "ok"

    def test_the_index_install_finding_is_unchanged(self, here, record):
        """The original question still answered, and still the louder of the two."""
        record(None)
        status, detail = checker._classify("idp_common")
        assert status == "bad"
        assert "INDEX" in detail


class TestExitStatus:
    def _run(self, capsys) -> int:
        code = checker.main()
        capsys.readouterr()
        return code

    def test_a_foreign_pointer_fails_the_check(
        self, here, record, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(checker, "FIRST_PARTY", ["idp_common"])
        monkeypatch.delenv(checker.ESCAPE_HATCH, raising=False)
        record(_editable(_checkout(tmp_path / "theirs") / "lib" / "idp_common_pkg"))
        assert self._run(capsys) == 1

    def test_the_escape_hatch_makes_it_a_note(
        self, here, record, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(checker, "FIRST_PARTY", ["idp_common"])
        monkeypatch.setenv(checker.ESCAPE_HATCH, "1")
        record(_editable(_checkout(tmp_path / "theirs") / "lib" / "idp_common_pkg"))
        assert self._run(capsys) == 0

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
    def test_a_non_affirmative_value_leaves_the_check_on(
        self, here, record, tmp_path, monkeypatch, capsys, value
    ):
        """``=0`` must not read as "set", the same choice the other guards make."""
        monkeypatch.setattr(checker, "FIRST_PARTY", ["idp_common"])
        monkeypatch.setenv(checker.ESCAPE_HATCH, value)
        record(_editable(_checkout(tmp_path / "theirs") / "lib" / "idp_common_pkg"))
        assert self._run(capsys) == 1

    def test_a_pointer_into_this_checkout_passes(
        self, here, record, monkeypatch, capsys
    ):
        monkeypatch.setattr(checker, "FIRST_PARTY", ["idp_common"])
        record(_editable(here / "lib" / "idp_common_pkg"))
        assert self._run(capsys) == 0
