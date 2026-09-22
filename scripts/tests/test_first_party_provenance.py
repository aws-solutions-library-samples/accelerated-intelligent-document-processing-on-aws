# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the first-party import provenance guard.

Every case here drives the REAL :func:`assert_resolves_in` against a synthetic checkout
built in ``tmp_path``, rather than asserting that the logic works by re-reading it. The
distinction matters for a guard: the failure this one exists to catch is a *silent pass*,
so a test that cannot itself distinguish "checked and agreed" from "did not check" would
be worth nothing. Each positive case below is paired with the negative that proves the
check was actually consulted.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import first_party_provenance as fpp  # noqa: E402

sys.path.pop(0)


def _fake_checkout(root: Path, *, git_is_file: bool = False) -> Path:
    """A directory that looks like a checkout, with the ``.git`` entry git would make."""
    root.mkdir(parents=True, exist_ok=True)
    if git_is_file:
        # What git writes in a WORKTREE: a file, not a directory. Existence rather than
        # type is what the helper tests, and this case is why.
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    else:
        (root / ".git").mkdir(exist_ok=True)
    return root


def _module_at(path: Path) -> types.ModuleType:
    """A module object whose ``__file__`` is ``path``, without importing anything."""
    module = types.ModuleType("pretend_first_party")
    module.__file__ = str(path)
    return module


@pytest.fixture
def pin_import(monkeypatch):
    """Make ``import_module`` return a module whose ``__file__`` the test chooses."""

    def _pin(path: Path | None):
        def fake(name: str):
            if path is None:
                raise ImportError(f"no module named {name}")
            return _module_at(path)

        monkeypatch.setattr(fpp, "import_module", fake)

    return _pin


class TestCheckoutRoot:
    def test_finds_the_root_from_a_nested_file(self, tmp_path):
        root = _fake_checkout(tmp_path / "repo")
        nested = root / "lib" / "pkg" / "tests"
        nested.mkdir(parents=True)
        assert fpp.checkout_root(nested / "conftest.py") == root

    def test_a_worktree_dot_git_file_counts(self, tmp_path):
        """``.git`` is a FILE in a worktree; keying on directory-ness would miss it."""
        root = _fake_checkout(tmp_path / "wt", git_is_file=True)
        (root / "tests").mkdir()
        assert fpp.checkout_root(root / "tests" / "conftest.py") == root

    def test_the_nearest_root_wins_for_a_nested_checkout(self, tmp_path):
        outer = _fake_checkout(tmp_path / "outer")
        inner = _fake_checkout(outer / "vendor" / "inner")
        assert fpp.checkout_root(inner / "a.py") == inner


class TestAssertResolvesIn:
    def test_passes_when_the_module_is_inside_the_same_checkout(
        self, tmp_path, pin_import
    ):
        root = _fake_checkout(tmp_path / "repo")
        pin_import(root / "lib" / "idp_common_pkg" / "idp_common" / "__init__.py")
        fpp.assert_resolves_in("idp_common", root / "tests" / "conftest.py")

    def test_raises_when_the_module_is_in_a_sibling_checkout(
        self, tmp_path, pin_import
    ):
        root = _fake_checkout(tmp_path / "repo")
        foreign = _fake_checkout(tmp_path / "other")
        pin_import(foreign / "lib" / "idp_common_pkg" / "idp_common" / "__init__.py")
        with pytest.raises(fpp.ForeignCheckoutError) as excinfo:
            fpp.assert_resolves_in("idp_common", root / "tests" / "conftest.py")
        message = str(excinfo.value)
        # The message has to name BOTH trees: "wrong import" without saying which tree
        # was actually measured leaves the reader exactly where they started.
        assert str(root) in message
        assert str(foreign) in message

    def test_a_worktree_validates_against_itself(self, tmp_path, pin_import):
        """The anchor is per-call, so a worktree is not judged against another root.

        This is the case that decides whether the guard is usable: worktrees are a normal
        way to work here, and a check keyed to one canonical path would fail every one of
        them and train people to set the escape hatch permanently.
        """
        worktree = _fake_checkout(tmp_path / "wt", git_is_file=True)
        pin_import(worktree / "lib" / "idp_common_pkg" / "idp_common" / "__init__.py")
        fpp.assert_resolves_in("idp_common", worktree / "tests" / "conftest.py")

    def test_a_worktree_NESTED_INSIDE_the_checkout_is_still_foreign(
        self, tmp_path, pin_import
    ):
        """The case an ancestry test accepts and an identity test rejects.

        A git worktree lives at ``<root>/.claude/worktrees/<name>/`` — the path
        ``.gitignore`` reserves and that tooling here creates. Asking only whether the
        module's path is *under* the checkout root accepts a module from such a
        worktree, although it is a different revision of the library. This is not a
        corner case: it is the layout the live foreign tree had on the machine this
        guard was written on.
        """
        root = _fake_checkout(tmp_path / "repo")
        nested = _fake_checkout(
            root / ".claude" / "worktrees" / "agent-x", git_is_file=True
        )
        pin_import(nested / "lib" / "idp_common_pkg" / "idp_common" / "__init__.py")
        with pytest.raises(fpp.ForeignCheckoutError) as excinfo:
            fpp.assert_resolves_in("idp_common", root / "tests" / "conftest.py")
        assert str(nested) in str(excinfo.value)

    def test_the_primary_checkout_is_foreign_to_a_nested_worktree(
        self, tmp_path, pin_import
    ):
        # The same pair in the other direction, which an ancestry test already got
        # right. Kept so the two are asserted as a matched set rather than leaving the
        # fix above able to pass by refusing everything.
        root = _fake_checkout(tmp_path / "repo")
        nested = _fake_checkout(
            root / ".claude" / "worktrees" / "agent-x", git_is_file=True
        )
        pin_import(root / "lib" / "idp_common_pkg" / "idp_common" / "__init__.py")
        with pytest.raises(fpp.ForeignCheckoutError):
            fpp.assert_resolves_in("idp_common", nested / "tests" / "conftest.py")

    def test_the_escape_hatch_warns_instead_of_raising(
        self, tmp_path, pin_import, monkeypatch
    ):
        root = _fake_checkout(tmp_path / "repo")
        foreign = _fake_checkout(tmp_path / "other")
        pin_import(foreign / "lib" / "idp_common_pkg" / "idp_common" / "__init__.py")
        monkeypatch.setenv(fpp.ESCAPE_HATCH, "1")
        with pytest.warns(UserWarning, match="provenance check was skipped"):
            fpp.assert_resolves_in("idp_common", root / "tests" / "conftest.py")

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "y", "on", " on "])
    def test_affirmative_spellings_honour_the_escape_hatch(
        self, tmp_path, pin_import, monkeypatch, value
    ):
        root = _fake_checkout(tmp_path / "repo")
        foreign = _fake_checkout(tmp_path / "other")
        pin_import(foreign / "idp_common" / "__init__.py")
        monkeypatch.setenv(fpp.ESCAPE_HATCH, value)
        with pytest.warns(UserWarning):
            fpp.assert_resolves_in("idp_common", root / "tests" / "conftest.py")

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "off", "maybe"])
    def test_non_affirmative_spellings_leave_the_guard_on(
        self, tmp_path, pin_import, monkeypatch, value
    ):
        """``=0`` must not read as "set". The shared-branch hook makes the same choice."""
        root = _fake_checkout(tmp_path / "repo")
        foreign = _fake_checkout(tmp_path / "other")
        pin_import(foreign / "idp_common" / "__init__.py")
        monkeypatch.setenv(fpp.ESCAPE_HATCH, value)
        with pytest.raises(fpp.ForeignCheckoutError):
            fpp.assert_resolves_in("idp_common", root / "tests" / "conftest.py")

    def test_an_unimportable_module_is_left_alone(self, tmp_path, pin_import):
        """Raising here would mask the real ImportError from the test that needs it."""
        root = _fake_checkout(tmp_path / "repo")
        pin_import(None)
        fpp.assert_resolves_in("idp_common", root / "tests" / "conftest.py")

    def test_a_namespace_package_is_not_flagged(self, tmp_path, monkeypatch):
        """No ``__file__`` means nothing to compare; silence is correct, not lenient."""
        root = _fake_checkout(tmp_path / "repo")
        module = types.ModuleType("pretend")
        monkeypatch.setattr(fpp, "import_module", lambda name: module)
        fpp.assert_resolves_in("idp_common", root / "tests" / "conftest.py")

    def test_no_git_entry_means_no_verdict(self, tmp_path, pin_import):
        """An exported source tree with no ``.git`` cannot be judged, so it is skipped.

        Paired with the sibling-checkout case above: without that pairing this test would
        also pass if the function simply never checked anything.
        """
        loose = tmp_path / "exported"
        (loose / "tests").mkdir(parents=True)
        pin_import(tmp_path / "somewhere-else" / "idp_common" / "__init__.py")
        fpp.assert_resolves_in("idp_common", loose / "tests" / "conftest.py")


class TestGuardIsWiredIn:
    """The helper existing is not the same as it being called.

    A control that exists but is never consulted is one of the two recurring defect
    classes named in ``.claude/skills/repo-quality-review.md``, so the wiring is asserted
    rather than assumed.
    """

    REPO_ROOT = Path(__file__).resolve().parents[2]

    CONFTESTS = (
        "lib/idp_common_pkg/tests/conftest.py",
        "feature-platform/main-stack-extensions/tests/conftest.py",
    )

    @pytest.mark.parametrize("conftest", CONFTESTS)
    def test_conftest_calls_the_guard(self, conftest):
        source = (self.REPO_ROOT / conftest).read_text(encoding="utf-8")
        assert "from first_party_provenance import assert_resolves_in" in source
        assert 'assert_resolves_in("idp_common", __file__)' in source

    @pytest.mark.parametrize("conftest", CONFTESTS)
    def test_the_guard_is_not_wrapped_in_a_blanket_import_suppressor(self, conftest):
        """Wired and *working*, which a source grep alone cannot establish.

        The call used to sit inside ``try: ... except ImportError: pass``. That made the
        two assertions above satisfiable by a conftest in which the guard never runs:
        rename the helper's exported symbol and the import fails, the clause swallows it,
        both suites lose the guard silently, and every test in this file still passes.
        A guard whose own wiring test cannot tell "consulted" from "skipped" is the
        defect this module exists to catch, so the suppressor's absence is asserted
        rather than its presence being assumed.
        """
        source = (self.REPO_ROOT / conftest).read_text(encoding="utf-8")

        # Only the window between the helper import and the call, not the whole file.
        # `lib/idp_common_pkg/tests/conftest.py` has a legitimate `except ImportError`
        # in `_stub_if_absent`, hundreds of lines earlier and about something else
        # entirely, so a file-wide search reports a finding that is not one.
        start = source.index("from first_party_provenance import assert_resolves_in")
        end = source.index('assert_resolves_in("idp_common", __file__)')
        window = source[start:end]
        assert "except ImportError" not in window, (
            "the guard is inside an ImportError suppressor, so a renamed symbol would "
            "disable it silently; gate on the helper file existing instead"
        )
        assert '"first_party_provenance.py").is_file()' in source, (
            "the guard should be gated on the helper FILE existing, which is the only "
            "condition its skip actually claims to cover"
        )
