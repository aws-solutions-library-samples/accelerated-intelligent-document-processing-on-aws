# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""S4's per-item rule: a `scope_filtered` operation must filter EACH returned item.

The reference half of S4 establishes that an operation's code mentions the caller's
scope. A listing can do that — resolve the scope, log it, even check it once — and
still return every row. The per-item half requires a recognised scope predicate
called inside an iteration over results (or a whole-list scope filter) somewhere in
the code the operation reaches, following calls from the resolver into
`idp_common`, and requires that predicate to bottom out at `scope_allows`.

Every case below is a synthetic resolver (and, where the filter lives in a library
service, a synthetic `idp_common` module under a temporary root), so the rule keeps
its teeth independently of the live tree, which exercises only the passing side.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

_SDLC_DIR = Path(__file__).resolve().parents[1]


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "scan_api_rbac_per_item", _SDLC_DIR / "scan_api_rbac.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_api_rbac_per_item"] = module
    spec.loader.exec_module(module)
    return module


scanner = _load_scanner()

pytestmark = pytest.mark.unit


def _sites(text: str, op: str, root: Path, *, sole_op: bool = False) -> list[str]:
    error, sites = scanner.op_per_item_scope_filter(
        textwrap.dedent(text), op, sole_op=sole_op, first_party_root=root
    )
    assert error is None, error
    return sites


# --- resolver-local filtering -------------------------------------------------

_LOCAL = """
from idp_common.config_scope import scope_allows

def handler(event, context):
    field = event["info"]["fieldName"]
    if field == "listThings":
        return list_things(event)
    elif field == "countThings":
        return count_things(event)

def list_things(event):
    allowed = _caller_scope(event)
    return [t for t in _query() if scope_allows(allowed, t.get("ConfigVersion"))]

def count_things(event):
    allowed = _caller_scope(event)
    logger.info("scope %s", allowed)
    return {"items": _query()}

def _caller_scope(event):
    return users.get("allowedConfigVersions")

def _query():
    return []
"""


class TestResolverLocal:
    def test_a_comprehension_filter_passes(self, tmp_path):
        assert _sites(_LOCAL, "listThings", tmp_path) == [
            "resolver:list_things:scope_allows"
        ]

    def test_resolving_the_scope_but_returning_every_item_fails(self, tmp_path):
        """Passes the reference check (it names allowedConfigVersions) — not this one."""
        error, reachable = scanner.op_scope_source(
            textwrap.dedent(_LOCAL), "countThings", declared_entry=None, sole_op=False
        )
        assert error is None
        assert any(p in reachable for p in scanner.SCOPE_PATTERNS)

        assert _sites(_LOCAL, "countThings", tmp_path) == []

    def test_a_single_check_outside_any_iteration_is_not_per_item(self, tmp_path):
        text = """
        def handler(event, context):
            allowed = _scope(event)
            if not scope_allows(allowed, event["arguments"]["profile"]):
                raise PermissionError("OUT_OF_SCOPE")
            return _query()
        """
        assert _sites(text, "listThings", tmp_path, sole_op=True) == []

    def test_a_for_loop_body_counts(self, tmp_path):
        text = """
        def handler(event, context):
            allowed = _scope(event)
            out = []
            for row in _query():
                if not _item_in_config_scope(row, allowed):
                    continue
                out.append(row)
            return out

        def _item_in_config_scope(item, allowed):
            return scope_allows(allowed, item.get("ConfigVersion"))
        """
        assert _sites(text, "listThings", tmp_path, sole_op=True) == [
            "resolver:handler:_item_in_config_scope"
        ]

    def test_a_filter_call_counts(self, tmp_path):
        text = """
        def handler(event, context):
            return list(filter(lambda r: scope_allows(_scope(event), r), _query()))
        """
        assert _sites(text, "listThings", tmp_path, sole_op=True)

    def test_a_named_helper_that_no_longer_consults_the_scope_is_not_credited(
        self, tmp_path
    ):
        """The name alone is not enough: the helper must bottom out at scope_allows."""
        text = """
        def handler(event, context):
            return [r for r in _query() if _item_in_config_scope(r, None)]

        def _item_in_config_scope(item, allowed):
            return True
        """
        assert _sites(text, "listThings", tmp_path, sole_op=True) == []

    def test_an_unknown_predicate_name_is_not_credited(self, tmp_path):
        text = """
        def handler(event, context):
            return [r for r in _query() if looks_fine(r)]
        """
        assert _sites(text, "listThings", tmp_path, sole_op=True) == []

    def test_an_undiscoverable_entry_in_a_shared_file_is_an_error(self, tmp_path):
        error, sites = scanner.op_per_item_scope_filter(
            textwrap.dedent(_LOCAL),
            "someOtherOp",
            sole_op=False,
            first_party_root=tmp_path,
        )
        assert error is not None and sites == []


# --- filtering inside a first-party library service --------------------------

_DISPATCH_TABLE_RESOLVER = """
from idp_common.things.service import create_service

_SERVICE = create_service()
_FIELD_GROUPS = {"listMyThings": _WORKERS, "listAllThings": _WORKERS}

def _caller_context(event):
    return resolve_allowed_config_versions(event)

def _my_things(c, args):
    return _SERVICE.list_mine(c)

def _all_things(c, args):
    return _SERVICE.list_all(c)

_FIELDS = {"listMyThings": _my_things, "listAllThings": _all_things}

def handler(event, context):
    caller = _caller_context(event)
    return _FIELDS[event["info"]["fieldName"]](caller, {})
"""

_SERVICE_FILTERED = """
from idp_common.config_scope import scope_allows

class ThingService:
    @staticmethod
    def _admits(caller, profile):
        return caller.is_admin or scope_allows(caller.allowed_versions, profile)

    @classmethod
    def _sees(cls, caller, record):
        return cls._admits(caller, record.profile)

    def list_mine(self, caller):
        items = self.store.query_owner(caller.key)
        return [i for i in items if self._sees(caller, i)]

    def list_all(self, caller):
        return [i for i in self.store.query_all() if self._sees(caller, i)]

def create_service():
    return ThingService()
"""


def _library(root: Path, service_src: str) -> Path:
    pkg = root / "idp_common" / "things"
    pkg.mkdir(parents=True)
    (root / "idp_common" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "service.py").write_text(textwrap.dedent(service_src))
    (root / "idp_common" / "config_scope.py").write_text(
        "def scope_allows(scope, profile):\n    return not scope or profile in scope\n"
        "\n\ndef filter_profiles(scope, names):\n"
        "    return [n for n in names if scope_allows(scope, n)]\n"
    )
    return root


class TestThroughALibraryService:
    def test_a_dispatch_table_entry_reaches_the_service_filter(self, tmp_path):
        root = _library(tmp_path, _SERVICE_FILTERED)
        assert _sites(_DISPATCH_TABLE_RESOLVER, "listMyThings", root) == [
            "idp_common.things.service:list_mine:_sees"
        ]

    def test_an_unfiltered_service_listing_fails(self, tmp_path):
        """The resolver resolves the scope; the service returns every item it read."""
        root = _library(
            tmp_path,
            _SERVICE_FILTERED.replace(
                "return [i for i in items if self._sees(caller, i)]",
                "return list(items)",
            ),
        )
        assert _sites(_DISPATCH_TABLE_RESOLVER, "listMyThings", root) == []
        # The sibling's filter in the same service is not credited to it.
        assert _sites(_DISPATCH_TABLE_RESOLVER, "listAllThings", root)

    def test_scope_enforced_in_is_not_the_entry(self, tmp_path):
        """The caller-context helper resolves the scope and iterates over nothing.

        Using it as the entry would make every op in the module pass or fail
        together; the operation's own dispatch-table function is the entry.
        """
        root = _library(tmp_path, _SERVICE_FILTERED)
        entries = scanner._item_dispatch_entries(
            __import__("ast").parse(textwrap.dedent(_DISPATCH_TABLE_RESOLVER)),
            "listMyThings",
        )
        assert [e.name for e in entries] == ["_my_things"]
        assert _sites(_DISPATCH_TABLE_RESOLVER, "listMyThings", root)

    def test_a_whole_list_filter_counts_anywhere(self, tmp_path):
        root = _library(tmp_path, _SERVICE_FILTERED)
        text = """
        from idp_common.config_scope import filter_profiles

        def handler(event, context):
            return filter_profiles(_scope(event), _names())
        """
        assert "resolver:handler:filter_profiles" in _sites(
            text, "listThings", root, sole_op=True
        )


# --- wired into run_checks ----------------------------------------------------


def _fixture(tmp_path: Path, service_src: str) -> Path:
    """The files run_checks opens, with one scope_filtered op over a library service."""
    nested = tmp_path / "nested" / "api-resolvers"
    disp = nested / "src" / "lambda" / "http_api_dispatcher"
    disp.mkdir(parents=True)
    (disp / "index.py").write_text("")
    (disp / "ddb_direct.py").write_text("")
    api = nested / "src" / "api"
    api.mkdir(parents=True)
    (api / "schema.graphql").write_text("type Query {\n  noop: String\n}\n")
    (nested / "template.yaml").write_text("Resources: {}\n")
    resolver = nested / "src" / "lambda" / "things"
    resolver.mkdir(parents=True)
    (resolver / "index.py").write_text(textwrap.dedent(_DISPATCH_TABLE_RESOLVER))
    _library(tmp_path / "lib" / "idp_common_pkg", service_src)
    (tmp_path / "template.yaml").write_text(
        "Resources:\n"
        "  AdminGroup:\n"
        "    Type: AWS::Cognito::UserPoolGroup\n"
        "    Properties:\n"
        "      GroupName: Admin\n"
    )
    (tmp_path / "scripts").mkdir()
    entry = (
        "    groups: [Admin]\n"
        "    kind: read\n"
        "    scope_filtered: true\n"
        "    scope_enforced_in: _caller_context\n"
        "    enforced_in: nested/api-resolvers/src/lambda/things/index.py\n"
    )
    (tmp_path / "scripts" / "api_rbac_expectations.yaml").write_text(
        "operations:\n  listMyThings:\n" + entry + "  listAllThings:\n" + entry
    )
    return tmp_path


def _per_item(repo: Path) -> list[tuple[str, str]]:
    return [
        (f.op, f.level)
        for f in scanner.run_checks(strict=False, repo=repo)
        if f.check == "S4-per-item"
    ]


class TestRunChecks:
    def test_a_filtered_service_produces_no_per_item_finding(self, tmp_path):
        assert _per_item(_fixture(tmp_path, _SERVICE_FILTERED)) == []

    def test_an_unfiltered_service_listing_is_a_fail_naming_the_operation(
        self, tmp_path
    ):
        repo = _fixture(
            tmp_path,
            _SERVICE_FILTERED.replace(
                "return [i for i in items if self._sees(caller, i)]",
                "return list(items)",
            ),
        )
        assert _per_item(repo) == [("listMyThings", "FAIL")]

    def test_scope_checked_operations_are_not_subject_to_it(self, tmp_path):
        repo = _fixture(
            tmp_path,
            _SERVICE_FILTERED.replace(
                "return [i for i in items if self._sees(caller, i)]",
                "return list(items)",
            ),
        )
        spec = repo / "scripts" / "api_rbac_expectations.yaml"
        spec.write_text(spec.read_text().replace("scope_filtered", "scope_checked"))
        assert _per_item(repo) == []
