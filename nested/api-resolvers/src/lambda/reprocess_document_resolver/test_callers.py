# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Who may ask for a reprocess, and inside which configuration scope.

Three kinds of caller reach the resolver. A call through the REST route carries
the identity the dispatcher verified: it must be an Admin or an Author, and a
scoped Author stays in their scope. A direct Lambda invoke carries no identity
and is trusted (the SDK, the CLI, a send-back). A direct invoke made for someone
(the MCP handler) names that principal as ``actingCaller``, which can only
narrow what the invoke may do.

The service is a recorder here: these cases pin what reaches it, and what is
refused before it.
"""

import pytest
from _harness import (
    OPTIONS,
    REPROCESS,
    acting_caller,
    direct_event,
    load,
    rest_event,
)

from idp_common.run_plan.models import RunKind
from idp_common.run_plan.reprocess import ReprocessOrigin, ReprocessOutcome

pytestmark = pytest.mark.unit

index = load("reprocess_resolver_index_callers")

KEY = "claims/a.pdf"
ORIGIN = {
    "kind": "sent_back",
    "actor": "reviewer@example.com",
    "cycleId": "0123456789abcdef01234",
    "note": "wrong class",
}


class _Service:
    """Stands in for ``ReprocessService``: records each call and queues it all."""

    def __init__(self):
        self.reprocessed = []
        self.asked = []

    def reprocess(self, object_keys, **kwargs):
        self.reprocessed.append((list(object_keys), kwargs))
        return [
            ReprocessOutcome(object_key=key, status="QUEUED", start_step="beginning")
            for key in object_keys
        ]

    def options(self, object_keys, **kwargs):
        self.asked.append((list(object_keys), kwargs))
        return [{"objectKey": key, "mode": "pipeline"} for key in object_keys]

    @property
    def calls(self):
        return self.reprocessed + self.asked


@pytest.fixture
def service(monkeypatch):
    recorder = _Service()
    monkeypatch.setattr(index, "_service", lambda: recorder)
    monkeypatch.setattr(
        index, "sanitize_event_for_logging", lambda event: {"redacted": True}
    )
    # Every document was last processed under `lending`.
    monkeypatch.setattr(index, "_document_config_version", lambda key: "lending")
    return recorder


@pytest.fixture
def scope(monkeypatch):
    """Sets the REST caller's ``allowedConfigVersions``; records each lookup."""
    state = {"allowed": None, "lookups": 0}

    def lookup(email, sub=""):
        state["lookups"] += 1
        return state["allowed"]

    monkeypatch.setattr(index, "_get_user_allowed_config_versions", lookup)
    return state


def _passed(service):
    ((_, kwargs),) = service.calls
    return kwargs


class TestTheRestRoute:
    def test_an_author_is_allowed_and_their_scope_is_passed_on(self, service, scope):
        scope["allowed"] = ["lending"]

        result = index.handler(rest_event(["Author"]), None)

        assert result["queued"] == 1
        assert _passed(service)["allowed_versions"] == ["lending"]
        assert scope["lookups"] == 1

    def test_an_author_outside_the_documents_profile_is_refused(self, service, scope):
        scope["allowed"] = ["mortgage"]

        with pytest.raises(PermissionError, match="outside your allowed"):
            index.handler(rest_event(["Author"]), None)
        assert service.calls == []

    def test_an_admin_is_not_scoped(self, service, scope):
        index.handler(rest_event(["Admin"]), None)

        assert _passed(service)["allowed_versions"] is None
        assert scope["lookups"] == 0

    @pytest.mark.parametrize("groups", [["Reviewer"], ["Viewer"], ["Annotator"], []])
    @pytest.mark.parametrize("field", [REPROCESS, OPTIONS])
    def test_another_group_is_refused(self, service, scope, groups, field):
        with pytest.raises(PermissionError, match="Admin or Author"):
            index.handler(rest_event(groups, field=field), None)
        assert service.calls == []
        assert scope["lookups"] == 0

    def test_an_acting_caller_in_the_request_is_ignored(self, service, scope):
        """The verified identity decides, whatever else the request says."""
        event = rest_event(["Reviewer"])
        event["actingCaller"] = acting_caller(["Admin"])

        with pytest.raises(PermissionError, match="Admin or Author"):
            index.handler(event, None)
        assert service.calls == []

    def test_an_acting_caller_cannot_widen_an_authors_scope(self, service, scope):
        scope["allowed"] = ["mortgage"]
        event = rest_event(["Author"])
        event["actingCaller"] = acting_caller(["Admin"], scope=None)

        with pytest.raises(PermissionError, match="outside your allowed"):
            index.handler(event, None)
        assert service.calls == []

    def test_an_acting_caller_cannot_narrow_an_admin(self, service, scope):
        """Ignored means ignored: it is not read at all on this route."""
        event = rest_event(["Admin"])
        event["actingCaller"] = acting_caller(["Viewer"], scope=["mortgage"])

        assert index.handler(event, None)["queued"] == 1
        assert _passed(service)["allowed_versions"] is None

    def test_an_origin_in_the_request_is_ignored(self, service, scope):
        """A caller cannot record a reprocess as someone's send-back."""
        index.handler(rest_event(["Admin"], origin=ORIGIN), None)

        assert _passed(service)["origin"] is None


class TestADirectInvoke:
    def test_it_is_allowed_without_a_scope(self, service, scope):
        result = index.handler(direct_event(), None)

        assert result["queued"] == 1
        assert _passed(service)["allowed_versions"] is None
        assert scope["lookups"] == 0

    def test_an_explicit_null_identity_is_a_direct_invoke(self, service, scope):
        event = direct_event()
        event["identity"] = None

        assert index.handler(event, None)["queued"] == 1

    def test_an_identity_with_no_groups_is_not_one(self, service, scope):
        event = direct_event()
        event["identity"] = {}

        with pytest.raises(PermissionError, match="Admin or Author"):
            index.handler(event, None)

    def test_its_origin_is_passed_on(self, service, scope):
        """A send-back, as the review queues resolver asks for it: the origin and
        no step. Where the run starts is the service's decision, from the origin."""
        index.handler(direct_event(origin=ORIGIN), None)

        origin = _passed(service)["origin"]
        assert isinstance(origin, ReprocessOrigin)
        assert origin.kind == RunKind.SENT_BACK
        assert (origin.actor, origin.cycle_id, origin.note) == (
            "reviewer@example.com",
            "0123456789abcdef01234",
            "wrong class",
        )
        # The resolver names no step of its own for a send-back.
        assert _passed(service)["start"] == "beginning"

    def test_a_step_named_with_an_origin_is_passed_on_for_the_service_to_judge(
        self, service, scope
    ):
        index.handler(direct_event(startStep="extraction", origin=ORIGIN), None)

        assert _passed(service)["start"] == "extraction"
        assert _passed(service)["origin"].kind == RunKind.SENT_BACK

    @pytest.mark.parametrize(
        "origin", ["sent_back", ["sent_back"], {"kind": "new_upload"}, {"kind": "x"}]
    )
    def test_an_origin_it_cannot_read_is_a_bad_request(self, service, scope, origin):
        with pytest.raises(ValueError, match="BAD_ARGUMENT: origin"):
            index.handler(direct_event(origin=origin), None)
        assert service.calls == []

    def test_it_may_ask_for_the_options(self, service, scope):
        result = index.handler(direct_event(field=OPTIONS), None)

        assert result == [{"objectKey": KEY, "mode": "pipeline"}]
        assert _passed(service)["allowed_versions"] is None


class TestADirectInvokeForSomeone:
    def test_an_acting_admin_is_allowed_and_unscoped(self, service, scope):
        index.handler(direct_event(acting=acting_caller(["Admin"])), None)

        assert _passed(service)["allowed_versions"] is None

    @pytest.mark.parametrize("roles", [["Viewer"], ["Reviewer"], ["Annotator"], []])
    def test_an_acting_caller_with_no_reprocess_role_is_refused(
        self, service, scope, roles
    ):
        with pytest.raises(PermissionError, match="Admin or Author"):
            index.handler(direct_event(acting=acting_caller(roles)), None)
        assert service.calls == []

    def test_a_role_this_stack_does_not_define_does_not_count(self, service, scope):
        with pytest.raises(PermissionError):
            index.handler(direct_event(acting=acting_caller(["SuperAdmin"])), None)

    @pytest.mark.parametrize("acting", ["Admin", ["Admin"], 1])
    def test_an_acting_caller_that_cannot_be_read_is_refused(
        self, service, scope, acting
    ):
        """Present and unreadable is not the same as absent: it does not fall
        back to the trusted, unscoped invoke."""
        with pytest.raises(PermissionError):
            index.handler(direct_event(acting=acting), None)
        assert service.calls == []

    def test_an_acting_authors_scope_is_the_scope(self, service, scope):
        acting = acting_caller(["Author"], scope=["lending"])

        index.handler(direct_event(acting=acting), None)

        assert _passed(service)["allowed_versions"] == ["lending"]
        # The scope is the one the MCP handler resolved; it is not looked up again.
        assert scope["lookups"] == 0

    def test_a_document_outside_the_acting_scope_refuses_the_whole_call(
        self, service, scope, monkeypatch
    ):
        versions = {"claims/a.pdf": "lending", "claims/b.pdf": "mortgage"}
        monkeypatch.setattr(index, "_document_config_version", versions.get)
        acting = acting_caller(["Author"], scope=["lending"])

        with pytest.raises(PermissionError, match="outside your allowed"):
            index.handler(
                direct_event(("claims/a.pdf", "claims/b.pdf"), acting=acting), None
            )
        assert service.calls == []

    def test_a_profile_outside_the_acting_scope_is_refused(self, service, scope):
        acting = acting_caller(["Author"], scope=["lending"])

        with pytest.raises(PermissionError, match="not in your allowed scope"):
            index.handler(direct_event(acting=acting, version="mortgage"), None)
        assert service.calls == []

    def test_the_options_are_scoped_the_same_way(self, service, scope, monkeypatch):
        monkeypatch.setattr(index, "_document_config_version", lambda key: "mortgage")
        acting = acting_caller(["Author"], scope=["lending"])

        with pytest.raises(PermissionError, match="outside your allowed"):
            index.handler(direct_event(field=OPTIONS, acting=acting), None)
        assert service.calls == []


class TestTheField:
    def test_an_unknown_field_is_a_bad_request(self, service, scope):
        with pytest.raises(ValueError, match="unsupported field"):
            index.handler(direct_event(field="deleteDocument"), None)
        assert service.calls == []

    def test_an_event_that_names_no_field_is_a_reprocess(self, service, scope):
        event = direct_event()
        del event["info"]

        assert index.handler(event, None)["queued"] == 1
        assert len(service.reprocessed) == 1
