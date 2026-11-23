# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""S10: an operation that names a profile, document or session says how it is scoped.

S4 checks the operations that declare ``scope_checked``/``scope_filtered``, and
nothing made an operation declare one. Four discovery operations took a
``configVersion`` and declared nothing, so S4 never looked at them and their missing
check was invisible to the gate. S10 derives the universe from the operations'
arguments instead — ``schema.graphql`` (input types included) and the entry's own
``args:`` — and requires every member to declare a scope, or be callable only by
callers that have none (Admin, IAM), or carry a registered per-operation exemption.

Every case drives ``run_checks`` over a fixture tree containing the defect, so the
S10 block has to be wired and reachable for these to pass, plus one case over the
real repository.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SDLC_DIR = Path(__file__).resolve().parents[1]


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "scan_api_rbac_s10", _SDLC_DIR / "scan_api_rbac.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_api_rbac_s10"] = module
    spec.loader.exec_module(module)
    return module


scanner = _load_scanner()

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_repository_exemptions(request, monkeypatch):
    """A fixture tree holds none of the repository's operations, so the real
    exemption list would read as stale there. The repository tests keep it."""
    if request.cls is not TestTheRepository:
        monkeypatch.setattr(scanner, "SCOPE_KEYED_ARGUMENT_EXEMPT", {})


_SCOPED_RESOLVER = """
def handler(event, context):
    field = event["info"]["fieldName"]
    if field == "doThing":
        allowed = event["allowedConfigVersions"]
        if not scope_allows(allowed, event["arguments"]["configVersion"]):
            raise PermissionError("Unauthorized: out of scope")
        return {}
    raise PermissionError("Unauthorized")
"""

_SCHEMA = """
input ThingInput {
  inner: InnerInput
  note: String
}

input InnerInput {
  objectKey: String
}

type Query {
  noop: String
}

type Mutation {
  doThing(configVersion: String!): String
  doNested(input: ThingInput!): String
  doUpper(ObjectKey: String!): String
  doPlain(name: String): String
}
"""


def _fixture(tmp_path: Path, operations: str, resolver: str = _SCOPED_RESOLVER) -> Path:
    (tmp_path / "scripts").mkdir(parents=True)
    nested = tmp_path / "nested" / "api-resolvers"
    disp = nested / "src" / "lambda" / "http_api_dispatcher"
    disp.mkdir(parents=True)
    (disp / "index.py").write_text("")
    (disp / "ddb_direct.py").write_text("")
    api = nested / "src" / "api"
    api.mkdir(parents=True)
    (api / "schema.graphql").write_text(_SCHEMA)
    (nested / "template.yaml").write_text("Resources: {}\n")
    proc = tmp_path / "src" / "lambda" / "proc"
    proc.mkdir(parents=True)
    (proc / "index.py").write_text(resolver)
    (tmp_path / "template.yaml").write_text(
        "Resources:\n"
        + "".join(
            f"  {g}Group:\n    Type: AWS::Cognito::UserPoolGroup\n"
            f"    Properties:\n      GroupName: {g}\n"
            for g in ("Admin", "Author", "Annotator")
        )
    )
    (tmp_path / "scripts" / "api_rbac_expectations.yaml").write_text(
        "operations:\n" + operations + "known_gaps:\n  GAP-99:\n    summary: x\n"
    )
    return tmp_path


def _entry(op, extra="", groups="[Admin, Author]", args="{}"):
    return (
        f"  {op}:\n"
        f"    groups: {groups}\n"
        "    kind: mutation\n"
        "    enforced_in: src/lambda/proc/index.py\n"
        f"    args: {args}\n" + "".join(f"    {line}\n" for line in extra.splitlines())
    )


def _s10(repo, level="FAIL"):
    return [
        (f.op, f.message)
        for f in scanner.run_checks(strict=False, repo=repo)
        if f.check == "S10" and f.level == level
    ]


class TestTheUniverseIsDerived:
    def test_an_undeclared_profile_argument_fails(self, tmp_path):
        """The discovery shape: a configVersion argument and no declaration."""
        repo = _fixture(tmp_path, _entry("doThing"))
        assert [op for op, _ in _s10(repo)] == ["doThing"]

    def test_declaring_the_scope_check_satisfies_it(self, tmp_path):
        repo = _fixture(tmp_path, _entry("doThing", "scope_checked: true"))
        assert _s10(repo) == []

    def test_an_argument_inside_a_nested_input_type_is_found(self, tmp_path):
        repo = _fixture(tmp_path, _entry("doNested"))
        assert [op for op, _ in _s10(repo)] == ["doNested"]

    def test_the_name_is_compared_case_insensitively(self, tmp_path):
        repo = _fixture(tmp_path, _entry("doUpper"))
        assert [op for op, _ in _s10(repo)] == ["doUpper"]

    def test_an_argument_only_in_the_entrys_args_is_found(self, tmp_path):
        repo = _fixture(tmp_path, _entry("ddbOnly", args='{ sessionId: "x" }'))
        assert [op for op, _ in _s10(repo)] == ["ddbOnly"]

    def test_an_operation_naming_nothing_scoped_is_not_in_the_universe(self, tmp_path):
        repo = _fixture(tmp_path, _entry("doPlain"))
        assert _s10(repo) == []


class TestCallersWithNoScope:
    @pytest.mark.parametrize("groups", ["[Admin]", "IAM_ONLY"])
    def test_an_operation_only_unscoped_callers_reach_needs_no_declaration(
        self, tmp_path, groups
    ):
        repo = _fixture(tmp_path, _entry("doThing", groups=groups))
        assert _s10(repo) == []

    def test_admin_plus_another_group_is_not_unscoped(self, tmp_path):
        repo = _fixture(tmp_path, _entry("doThing", groups="[Admin, Annotator]"))
        assert [op for op, _ in _s10(repo)] == ["doThing"]


class TestDeclarationsAreChecked:
    def test_testset_checked_with_no_test_set_check_in_reach_fails(self, tmp_path):
        repo = _fixture(tmp_path, _entry("doUpper", "testset_checked: true"))
        messages = [m for op, m in _s10(repo) if op == "doUpper"]
        assert messages and "testset_checked" in messages[0]

    def test_testset_checked_backed_by_the_shared_rule_passes(self, tmp_path):
        resolver = _SCOPED_RESOLVER.replace(
            '    raise PermissionError("Unauthorized")\n',
            '    if field == "doUpper":\n'
            '        assert_can_access_test_set(event, event["arguments"]["t"])\n'
            "        return {}\n"
            '    raise PermissionError("Unauthorized")\n',
        )
        repo = _fixture(tmp_path, _entry("doUpper", "testset_checked: true"), resolver)
        assert _s10(repo) == []

    def test_ownership_on_a_group_list_op_must_refuse(self, tmp_path):
        """S3 reads ownership only for ANY ops; S10 reads it for the rest."""
        resolver = "def handler(event, context):\n    owner = event['sub']\n    return table.get_item(Key={'sessionId': 1})\n"
        repo = _fixture(
            tmp_path,
            _entry("ddbOnly", "ownership: true", args='{ sessionId: "x" }'),
            resolver,
        )
        messages = [m for op, m in _s10(repo) if op == "ddbOnly"]
        assert messages and "refuses" in messages[0]

    def test_scope_checked_does_not_answer_for_a_document_beside_a_profile(
        self, tmp_path
    ):
        repo = _fixture(
            tmp_path,
            _entry(
                "ddbOnly",
                "scope_checked: true",
                args='{ configVersion: "x", objectKey: "y" }',
            ),
        )
        messages = [m for op, m in _s10(repo) if op == "ddbOnly"]
        assert len(messages) == 1 and "object" in messages[0]


class TestExemptionsAndGaps:
    def test_a_known_gap_downgrades_to_a_warning(self, tmp_path):
        repo = _fixture(tmp_path, _entry("doThing", "known_gap: GAP-99"))
        assert _s10(repo) == []
        assert [op for op, _ in _s10(repo, "WARN")] == ["doThing"]

    def test_a_registered_exemption_shields_its_one_pair(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            scanner, "SCOPE_KEYED_ARGUMENT_EXEMPT", {("doThing", "profile"): "why"}
        )
        repo = _fixture(tmp_path, _entry("doThing") + _entry("doUpper"))
        assert [op for op, _ in _s10(repo)] == ["doUpper"]

    def test_an_exemption_that_shields_nothing_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            scanner,
            "SCOPE_KEYED_ARGUMENT_EXEMPT",
            {("doThing", "profile"): "why", ("goneOp", "object"): "why"},
        )
        repo = _fixture(tmp_path, _entry("doThing", "scope_checked: true"))
        stale = sorted(op for op, m in _s10(repo) if "shields no finding" in m)
        assert stale == ["doThing", "goneOp"]


class TestTheRepository:
    def test_no_s10_failure_in_the_tree(self):
        failures = _s10(scanner.REPO)
        assert failures == [], failures

    def test_the_derived_universe_is_not_empty_and_holds_the_discovery_operations(
        self,
    ):
        """An empty universe would pass the test above with S10 deleted."""
        import yaml

        spec = yaml.safe_load(
            (scanner.REPO / "scripts" / "api_rbac_expectations.yaml").read_text()
        )
        schema = (
            scanner.REPO / "nested" / "api-resolvers" / "src" / "api" / "schema.graphql"
        ).read_text()
        arguments = scanner.schema_field_arguments(schema)
        universe = {
            op
            for op, o in spec["operations"].items()
            if scanner.scope_keyed_kinds(
                arguments.get(op, set()) | set((o.get("args") or {}).keys())
            )
        }
        assert {
            "startMultiDocDiscovery",
            "uploadMultiDocDiscoveryZip",
            "uploadDiscoveryDocument",
            "autoDetectSections",
            "updateTestSetDocumentSections",
        } <= universe

    def test_every_exemption_names_a_real_operation_and_kind(self):
        import yaml

        ops = yaml.safe_load(
            (scanner.REPO / "scripts" / "api_rbac_expectations.yaml").read_text()
        )["operations"]
        assert scanner.SCOPE_KEYED_ARGUMENT_EXEMPT
        for (op, kind), reason in scanner.SCOPE_KEYED_ARGUMENT_EXEMPT.items():
            assert op in ops
            assert kind in scanner.SCOPE_KEYED_DECLARATIONS
            assert len(reason) > 40


class TestTheVocabularyIsARule:
    """The kind is read from the words of a name, so a spelling nobody listed is
    still recognised. The names below are the ones a seven-name list missed."""

    @pytest.mark.parametrize(
        "name,kind",
        [
            ("objectKeys", "object"),
            ("s3Uri", "object"),
            ("s3Key", "object"),
            ("s3Prefix", "object"),
            ("s3Bucket", "object"),
            ("documentKey", "object"),
            ("ObjectKey", "object"),
            ("uiBundlePath", "object"),
            ("profileName", "profile"),
            ("configProfile", "profile"),
            ("metadataProfile", "profile"),
            ("queueProfile", "profile"),
            ("configRevision", "profile"),
            ("configVersion", "profile"),
            ("versionName", "profile"),
            ("version", "profile"),
            ("revision", "profile"),
            ("sessionId", "session"),
            # a version or revision of something other than a profile
            ("versionId", None),
            ("baseRevision", None),
            ("testSetVersion", None),
            ("installedVersion", None),
            # words that are not scope-bearing at all
            ("testSetId", None),
            ("jobId", None),
            ("customConfig", None),
            ("monkeyBusiness", None),
        ],
    )
    def test_the_kind_of_a_name(self, name, kind):
        assert scanner.scope_keyed_kind(name) == kind

    def test_a_name_never_written_down_is_found(self, tmp_path):
        """No word list names `invoiceUris`; the rule reads its words."""
        schema = _SCHEMA.replace(
            "doPlain(name: String): String",
            "doPlain(name: String): String\n  doNew(invoiceUris: [String]): String",
        )
        repo = _fixture(tmp_path, _entry("doNew"))
        (repo / "nested/api-resolvers/src/api/schema.graphql").write_text(schema)
        assert [op for op, _ in _s10(repo)] == ["doNew"]


class TestDocumentScopeDeclaration:
    _ARGS = '{ configVersion: "x", objectKeys: ["y"] }'

    def test_declared_and_backed_passes(self, tmp_path):
        resolver = _SCOPED_RESOLVER.replace(
            '    raise PermissionError("Unauthorized")\n',
            '    if field == "ddbOnly":\n'
            "        if not scope_allows(allowed, v):\n"
            '            raise PermissionError("x")\n'
            "        _enforce_document_scope(allowed, keys)\n"
            "        return {}\n"
            '    raise PermissionError("Unauthorized")\n',
        )
        repo = _fixture(
            tmp_path,
            _entry(
                "ddbOnly",
                "scope_checked: true\ndocument_scope_checked: true",
                args=self._ARGS,
            ),
            resolver,
        )
        assert _s10(repo) == []

    def test_declared_but_not_backed_fails(self, tmp_path):
        repo = _fixture(
            tmp_path,
            _entry(
                "ddbOnly",
                "scope_checked: true\ndocument_scope_checked: true",
                args=self._ARGS,
            ),
        )
        messages = [m for op, m in _s10(repo) if op == "ddbOnly"]
        assert messages and "document_scope_checked" in messages[0]


class TestTheRepositoryPatterns:
    def test_every_document_scope_pattern_is_reached_by_a_declaring_operation(self):
        """A pattern nothing reaches would pre-approve the next file containing it."""
        import yaml

        ops = yaml.safe_load(
            (scanner.REPO / "scripts" / "api_rbac_expectations.yaml").read_text()
        )["operations"]
        per_file = scanner.operations_per_enforced_file(ops)
        reached = ""
        declaring = [op for op, o in ops.items() if o.get("document_scope_checked")]
        assert declaring
        for op in declaring:
            o = ops[op]
            error, source = scanner.op_scope_source(
                (scanner.REPO / o["enforced_in"]).read_text(),
                op,
                declared_entry=o.get("scope_enforced_in"),
                sole_op=per_file[o["enforced_in"]] == 1,
            )
            assert error is None, (op, error)
            reached += source
        dead = [p for p in scanner.DOCUMENT_SCOPE_PATTERNS if p not in reached]
        assert dead == []
