# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The type catalogue, and the guard that keeps register_type out of production."""

import ast
from pathlib import Path

import pytest

from idp_common.notifications import catalogue
from idp_common.notifications.catalogue import (
    PLATFORM_TYPES,
    get_type,
    list_types,
    register_type,
    require_type,
    unregister_type,
)
from idp_common.notifications.exceptions import UnknownNotificationTypeError
from idp_common.notifications.models import NotificationType

_PKG_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT = _PKG_ROOT.parents[1]
_CATALOGUE = _PKG_ROOT / "idp_common" / "notifications" / "catalogue.py"
_SCANNED_ROOTS = [
    _PKG_ROOT / "idp_common",
    _REPO_ROOT / "src",
    _REPO_ROOT / "nested",
    _REPO_ROOT / "patterns",
]


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


@pytest.mark.unit
class TestRegistry:
    def test_register_get_list(self):
        a = NotificationType(id="zz.alpha", display_name="Alpha", description="a")
        b = NotificationType(id="zz.beta", display_name="Alpha", description="b")
        c = NotificationType(id="zz.gamma", display_name="Aardvark", description="c")
        try:
            for t in (a, b, c):
                register_type(t)
            assert get_type("zz.alpha") == a
            ids = [t.id for t in list_types() if t.id.startswith("zz.")]
            assert ids == ["zz.gamma", "zz.alpha", "zz.beta"]
        finally:
            for t in (a, b, c):
                unregister_type(t.id)
        assert get_type("zz.alpha") is None

    def test_identical_reregister_is_fine_conflict_raises(self, test_type):
        register_type(test_type)
        conflicting = test_type.model_copy(update={"display_name": "Other"})
        with pytest.raises(ValueError):
            register_type(conflicting)
        assert get_type(test_type.id) == test_type

    def test_require_type_raises_for_unknown(self):
        with pytest.raises(UnknownNotificationTypeError):
            require_type("no.such.type")

    def test_platform_types_are_valid_and_unique(self):
        ids = [t.id for t in PLATFORM_TYPES]
        assert len(ids) == len(set(ids))
        for t in PLATFORM_TYPES:
            assert NotificationType.model_validate(t.model_dump()) == t
            assert get_type(t.id) == t

    def test_registry_is_seeded_from_platform_types(self):
        assert set(t.id for t in PLATFORM_TYPES) <= set(catalogue._registry)


@pytest.mark.unit
class TestRegisterTypeGuard:
    def test_no_production_code_calls_register_type(self):
        offenders = []
        scanned = 0
        for root in _SCANNED_ROOTS:
            for path in root.rglob("*.py"):
                if "tests" in path.parts or path == _CATALOGUE:
                    continue
                if "node_modules" in path.parts or ".aws-sam" in path.parts:
                    continue
                scanned += 1
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeDecodeError):
                    continue
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Call)
                        and _call_name(node) == "register_type"
                    ):
                        offenders.append(f"{path}:{node.lineno}")
        assert scanned > 100, "the guard scanned too few files to mean anything"
        assert offenders == [], offenders


def _posted_type_ids() -> dict[str, list[str]]:
    """Every ``type_id="..."`` literal passed in production code, with its sites."""
    posted: dict[str, list[str]] = {}
    for root in _SCANNED_ROOTS:
        for path in root.rglob("*.py"):
            if "tests" in path.parts or path == _CATALOGUE:
                continue
            if "node_modules" in path.parts or ".aws-sam" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if (
                        kw.arg == "type_id"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        posted.setdefault(kw.value.value, []).append(
                            f"{path.relative_to(_REPO_ROOT)}:{node.lineno}"
                        )
    return posted


@pytest.mark.unit
class TestCatalogueMatchesProducers:
    """Inbox settings list the catalogue, so it must name exactly what code posts."""

    def test_every_catalogued_type_has_a_producer(self):
        posted = _posted_type_ids()
        unposted = sorted(t.id for t in PLATFORM_TYPES if t.id not in posted)
        assert unposted == [], f"catalogued but never posted: {unposted}"

    def test_every_posted_type_is_catalogued(self):
        catalogued = {t.id for t in PLATFORM_TYPES}
        posted = _posted_type_ids()
        assert posted, "the scan found no producers at all"
        unknown = {k: v for k, v in posted.items() if k not in catalogued}
        assert unknown == {}, f"posted but not catalogued: {unknown}"
