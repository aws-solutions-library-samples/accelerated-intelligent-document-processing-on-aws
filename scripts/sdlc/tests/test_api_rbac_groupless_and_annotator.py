# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The live matrix must drive every role the deployment has, plus no role at all.

The defect: `scripts/test_api_rbac.py` reports a verdict per (operation, role) cell
and the roles it reported over were not the roles the system has. It drove four of
the five Cognito groups and created no user outside a group, so two things the
authorization model rests on had no live assertion anywhere
([#1065](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1065)):

* a caller in **no group** — the case `ANY_GROUP` exists to refuse, which
  self-service sign-up and a half-finished onboarding both produce;
* the **`Annotator`** role, the only one scoped per object (`allowedTestSets`), so
  the seven test-set operations that admit it had no cell at all.

This is the same family as #1256, #1304 and #1365: declared reach exceeding actual
reach, where the report reads complete.

**Every assertion here is two-sided, and that is the point of the module.** A new
cell that cannot fail is worse than no cell, because it reports a role as covered.
So for each one, the shape that must pass is asserted *beside* the shape that must
fail — a group floor that admits the groupless caller, a floor that refuses an
admitted Annotator, a per-object control that admits an unassigned object, an
unattributed refusal, a 5xx — and a cell whose verdict does not move between them
would be caught by one direction or the other.

No AWS and no network: the matrix is driven with a fake `call`, and the Cognito and
DynamoDB helpers are replaced with recorders.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_HARNESS = _REPO / "scripts" / "test_api_rbac.py"
_EXPECTATIONS = _REPO / "scripts" / "api_rbac_expectations.yaml"
_GENERATOR = _REPO / "scripts" / "sdlc" / "generate_api_rbac_manifest.py"
_REVIEW_LAMBDA = _REPO / "src" / "lambda" / "complete_section_review" / "index.py"

API = "https://abc.execute-api.us-west-2.amazonaws.com/api"
CTX = {"api_base": API, "circuit_breaker": False}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


h = _load("_rbac_harness_groupless", _HARNESS)
gen = _load("_rbac_manifest_generator_groupless", _GENERATOR)

TOKENS = {p: f"tok-{p}" for p in h.MATRIX_PRINCIPALS}


@pytest.fixture(scope="module")
def expectations() -> dict:
    return yaml.safe_load(_EXPECTATIONS.read_text())["operations"]


@pytest.fixture(scope="module")
def deployment_groups() -> list:
    """The Cognito groups `template.yaml` creates, read with the generator's own
    function — the same one the dispatcher's manifest is built from, so there is no
    second reading of this fact to drift."""
    names = sorted(gen.cognito_group_names((_REPO / "template.yaml").read_text()))
    assert names, "no AWS::Cognito::UserPoolGroup in template.yaml"
    return names


# ===========================================================================
# the population: the matrix's principals are the deployment's groups, plus none
# ===========================================================================
def test_the_matrix_drives_every_group_the_deployment_creates(deployment_groups):
    """The property the issue is about, stated as an identity rather than a count.

    A figure here would go stale the first time a group is added, and the equality
    is the thing worth having: a group the template creates and `ROLES` does not
    name is a row of the policy nothing drives, and a name in `ROLES` that is not a
    group is a row whose user cannot be created.
    """
    assert sorted(h.ROLES) == deployment_groups, (
        "the roles the live matrix drives are not the groups this deployment has.\n"
        f"  ROLES:    {sorted(h.ROLES)}\n"
        f"  template: {deployment_groups}"
    )


def test_the_groupless_principal_is_not_one_of_those_groups(deployment_groups):
    """Its whole value is holding no group, so a name that collided with a real
    group would make every one of its cells assert the opposite thing — and the
    collision would be invisible, since the matrix would still print a row."""
    assert h.NO_GROUP not in deployment_groups
    assert h.NO_GROUP not in h.ROLES
    assert h.NO_GROUP in h.MATRIX_PRINCIPALS


def test_the_annotator_is_driven_and_is_a_real_group(deployment_groups):
    assert h.ANNOTATOR_ROLE in deployment_groups
    assert h.ANNOTATOR_ROLE in h.ROLES


# ===========================================================================
# driving the matrix: every principal gets a cell on every operation
# ===========================================================================
OPS = {
    # One operation per policy shape. `ANY_GROUP` arrives here already resolved to
    # the group names (load_expectations does that before the matrix runs), which is
    # why it is spelled as a list.
    "getDocument": {"groups": None, "args": {}},  # filled in below
    "listUsers": {"groups": ["Admin"], "args": {}},
    "getMyProfile": {"groups": "ANY", "args": {}},
    "updateAgentJobStatus": {"groups": "IAM_ONLY", "args": {}},
}


@pytest.fixture
def ops(deployment_groups):
    """A fresh copy per test — `run_group_matrix` reads `o['groups']` and the
    object_scoped tests mutate their own copy."""
    built = {field: dict(cfg) for field, cfg in OPS.items()}
    built["getDocument"]["groups"] = list(deployment_groups)  # ANY_GROUP, resolved
    return built


def _responder(**per_principal):
    """A fake `call` answering per principal, defaulting to a floor refusal.

    `default` answers every principal not named. Returning an `ApiResult` keeps the
    harness's own tuple unpacking and `authz_control` reading in the path under test
    rather than stubbed out.
    """
    default = per_principal.pop("default", None)

    def _call(api_base, field, args, token):
        if token is None:
            return h.ApiResult(401, None, None, "rid")
        principal = token.replace("tok-", "")
        answer = per_principal.get(principal, default)
        if answer is None:
            return h.ApiResult(
                403, "Unauthorized", None, "rid", control=h.CONTROL_GROUP_FLOOR
            )
        return answer

    return _call


def _rows(results, principal):
    return [r for r in results if r["principal"] == principal]


def _drive(monkeypatch, capsys, ops, call):
    monkeypatch.setattr(h, "call", call)
    results = []
    h.run_group_matrix(ops, CTX, TOKENS, results)
    capsys.readouterr()
    return results


def test_every_principal_gets_a_cell_on_every_operation(monkeypatch, capsys, ops):
    """The structural half: adding a principal to the list and not to the loop would
    leave the report naming a role it never called. That is the shape the fix for
    #1065 could most easily take while looking done."""
    results = _drive(
        monkeypatch,
        capsys,
        ops,
        _responder(
            **{p: h.ApiResult(200, None, None, "rid") for p in h.MATRIX_PRINCIPALS}
        ),
    )
    for principal in h.MATRIX_PRINCIPALS:
        driven = {r["op"] for r in _rows(results, principal)}
        assert driven == set(ops), (
            f"{principal} was not driven over every operation: missing "
            f"{sorted(set(ops) - driven)}"
        )


# ===========================================================================
# the groupless caller
# ===========================================================================
def test_a_groupless_caller_refused_by_the_group_floor_passes(monkeypatch, capsys, ops):
    """The positive arm: a correctly behaving deployment.

    A floor-labelled 403 on every operation whose policy is not `ANY` — the group
    list, the resolved `ANY_GROUP` and `IAM_ONLY` alike — and a 200 on the one that
    consults authentication only. Every groupless cell passes, which is what makes
    the failing shapes below meaningful rather than "this principal always fails".
    """
    ok = h.ApiResult(200, None, None, "rid")
    floor = h.ApiResult(403, "Unauthorized", None, "rid", control=h.CONTROL_GROUP_FLOOR)

    def _call(api_base, field, args, token):
        """Answer exactly as the policy says, for every principal."""
        if token is None:
            return h.ApiResult(401, None, None, "rid")
        principal = token.replace("tok-", "")
        policy = ops[field]["groups"]
        if policy == "ANY":
            return ok
        if policy == "IAM_ONLY":
            return floor
        return ok if principal in policy else floor

    results = _drive(monkeypatch, capsys, ops, _call)
    groupless = {r["op"]: r for r in _rows(results, h.NO_GROUP)}
    assert len(groupless) == len(ops)
    for field, row in groupless.items():
        assert row["outcome"] == "PASS", f"{field}: {row['detail']}"
    assert not h.hard_failures(results)


def test_a_group_floor_that_admits_a_groupless_caller_fails_the_gate(
    monkeypatch, capsys, ops
):
    """⭐ The non-vacuity control for the cell above, and the reason it is worth
    having at all.

    `ANY_GROUP` means "an administrator onboarded this person". If the floor lets a
    caller in no group through — the manifest widened, the sentinel left unexpanded,
    the claim comparison inverted — the cell must go red. Before this principal
    existed, every one of those changes was invisible to the live gate: nothing
    called the API without a group.
    """
    results = _drive(
        monkeypatch,
        capsys,
        ops,
        _responder(**{p: h.ApiResult(200, None, None, "rid") for p in TOKENS}),
    )
    groupless = {r["op"]: r for r in _rows(results, h.NO_GROUP)}
    assert groupless["getDocument"]["outcome"] == "FAIL", (
        "a groupless caller admitted to an ANY_GROUP operation was not reported as "
        "a failure"
    )
    assert groupless["listUsers"]["outcome"] == "FAIL"
    assert groupless["updateAgentJobStatus"]["outcome"] == "FAIL"
    assert h.hard_failures(results), "the gate stayed green"


def test_a_groupless_refusal_by_a_resource_control_is_a_failure(
    monkeypatch, capsys, ops
):
    """A `resource` label on this cell is a finding, not a pass.

    The floor runs before routing, so a caller it must refuse cannot reach a
    resolver. A per-object control catching them means the floor did not — defence
    in depth working over a hole in the layer this cell tests. Asserted here
    because the groupless principal is the caller for whom the per-object
    narrowings most often stand down (no UsersTable row means unrestricted on the
    config axis, and a caller in no group is not a Reviewer), so leaning on them is
    exactly the wrong reading.
    """
    results = _drive(
        monkeypatch,
        capsys,
        ops,
        _responder(
            **{p: h.ApiResult(200, None, None, "rid") for p in h.ROLES},
            **{
                h.NO_GROUP: h.ApiResult(
                    403, "Unauthorized", None, "rid", control=h.CONTROL_RESOURCE
                )
            },
        ),
    )
    groupless = {r["op"]: r for r in _rows(results, h.NO_GROUP)}
    assert groupless["getDocument"]["outcome"] == "FAIL"
    assert "WRONG CONTROL" in groupless["getDocument"]["detail"].upper()


def test_a_groupless_caller_must_not_be_refused_an_ANY_operation(
    monkeypatch, capsys, ops
):
    """The other polarity, and the one that keeps this principal honest.

    The 8 `ANY` operations consult authentication only, so refusing a groupless
    caller there is a regression in the opposite direction — a self-registered user
    locked out of their own profile. A principal whose every cell expected a
    refusal would pass by being refused everywhere, which is why this direction is
    asserted too.
    """
    results = _drive(
        monkeypatch,
        capsys,
        ops,
        _responder(
            **{p: h.ApiResult(200, None, None, "rid") for p in h.ROLES},
            **{h.NO_GROUP: None},  # refused everywhere, including the ANY op
        ),
    )
    profile = [r for r in _rows(results, h.NO_GROUP) if r["op"] == "getMyProfile"]
    assert profile and profile[0]["outcome"] == "FAIL", (
        "a groupless caller refused an ANY operation was not reported"
    )


@pytest.mark.parametrize(
    "answer,why",
    [
        (h.ApiResult(0, None, None, "<timeout>"), "a request that never completed"),
        (h.ApiResult(500, "InternalError", None, "rid"), "a server error"),
    ],
)
def test_an_inconclusive_groupless_cell_is_not_a_pass(
    monkeypatch, capsys, ops, answer, why
):
    """A refusal and an inconclusive result are different outcomes, and this cell
    asserts a refusal — so the cheapest way to fake it is a deployment that answers
    nothing. Neither shape may read as "correctly refused"."""
    results = _drive(
        monkeypatch, capsys, ops, _responder(**{h.NO_GROUP: answer}, default=answer)
    )
    groupless = _rows(results, h.NO_GROUP)
    assert groupless
    assert not [r for r in groupless if r["outcome"] == "PASS"], (
        f"{why} was reported as a pass for the groupless caller"
    )
    assert all(r["inconclusive"] for r in groupless)


# ===========================================================================
# the groupless user is created with no group, and destroyed
# ===========================================================================
class _Recorder:
    """Stands in for the Cognito and DynamoDB helpers the harness calls."""

    def __init__(self, groups_by_email=None):
        self.created = []
        self.deleted = []
        self.put_items = []
        self.deleted_keys = []
        self.delete_tables = []
        self.queries = []
        self.seeded_uids = {}
        self._groups = groups_by_email or {}

    # --- cognito -----------------------------------------------------------
    def create_cognito_user(self, ctx, email, group, password):
        self.created.append((email, group))
        self._groups.setdefault(email, [] if not group else [group])

    def delete_cognito_user(self, ctx, email):
        self.deleted.append(email)

    def cognito_groups_for_user(self, ctx, email):
        return sorted(self._groups.get(email, []))

    # --- the aws() shim ----------------------------------------------------
    def aws(self, *args, region=None):
        if args[:2] == ("dynamodb", "put-item"):
            self.put_items.append(args)
            # Remember the userId a seeding wrote for an address, so a later query
            # for that address answers with the SAME key the seeding recorded in
            # ctx. Without that the two sources of keys never overlap in this stub
            # and the de-duplication below would have nothing to de-duplicate.
            item = json.loads(args[args.index("--item") + 1])
            email = (item.get("email") or {}).get("S")
            uid = (item.get("userId") or {}).get("S")
            if email and uid:
                self.seeded_uids[email] = uid
        elif args[:2] == ("dynamodb", "query"):
            self.queries.append(args)
            # A row shaped as the recovery path reads it, so the KEYS it builds can
            # be asserted. Answering with no items would leave the key construction
            # unmeasured, and a wrong key deletes nothing while teardown reports a
            # delete — which is how the row stays in a live stack's table.
            #
            # Keyed on the address queried for, so "which addresses did teardown
            # sweep?" is answerable from the keys it deleted rather than only from
            # the queries it issued.
            values = json.loads(args[args.index("--expression-attribute-values") + 1])
            email = values[":e"]["S"]
            uid = self.seeded_uids.get(email, f"uid-{email.split('@')[0]}")
            return {
                "Items": [
                    {
                        "userId": {"S": uid},
                        "cognitoSub": {"S": f"sub-{email.split('@')[0]}"},
                    }
                ]
            }
        elif args[:2] == ("cognito-idp", "admin-get-user"):
            return {"UserAttributes": [{"Name": "sub", "Value": "sub-xyz"}]}
        return {}


@pytest.fixture
def seeded(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(h, "create_cognito_user", rec.create_cognito_user)
    monkeypatch.setattr(h, "delete_cognito_user", rec.delete_cognito_user)
    monkeypatch.setattr(h, "cognito_groups_for_user", rec.cognito_groups_for_user)
    monkeypatch.setattr(h, "aws", rec.aws)
    monkeypatch.setattr(h, "enable_admin_auth", lambda ctx: None)
    monkeypatch.setattr(h, "subprocess", _NoSubprocess(rec))
    return rec


class _NoSubprocess:
    """`teardown_users` deletes UsersTable items through `subprocess.run` directly."""

    def __init__(self, rec):
        self._rec = rec

    def run(self, cmd, **kwargs):
        if "delete-item" in cmd:
            self._rec.deleted_keys.append(cmd[cmd.index("--key") + 1])
            self._rec.delete_tables.append(cmd[cmd.index("--table-name") + 1])

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Done()


def _stack_ctx():
    return {
        "user_pool": "pool",
        "region": "us-west-2",
        "users_table": "UsersTable",
        "client": "client",
    }


def test_setup_creates_the_groupless_user_in_no_group(seeded, expectations, capsys):
    h.setup_users(_stack_ctx(), expectations)
    capsys.readouterr()
    for_groupless = [
        group for email, group in seeded.created if email == h.test_email(h.NO_GROUP)
    ]
    assert for_groupless == [None], (
        "the groupless user was not created, or was created with a group: "
        f"{for_groupless}"
    )


def test_a_groupless_user_that_holds_a_group_stops_the_run(monkeypatch, capsys):
    """⭐ The attack the verification exists for.

    Hand the groupless principal a group — a leftover account from an interrupted
    run, a copy-paste in the setup — and every `ANY_GROUP` cell reads as correctly
    admitted, nothing is refused that should be, and the run is green while
    asserting the opposite of what the report says. It must not get as far as the
    first request.
    """
    rec = _Recorder({h.test_email(h.NO_GROUP): ["Viewer"]})
    monkeypatch.setattr(h, "create_cognito_user", lambda *a, **k: None)
    monkeypatch.setattr(h, "delete_cognito_user", rec.delete_cognito_user)
    monkeypatch.setattr(h, "cognito_groups_for_user", rec.cognito_groups_for_user)
    monkeypatch.setattr(h, "aws", rec.aws)
    monkeypatch.setattr(h, "enable_admin_auth", lambda ctx: None)
    for role in h.ROLES:
        rec._groups[h.test_email(role)] = [role]
    with pytest.raises(RuntimeError, match="group membership"):
        h.setup_users(_stack_ctx(), {})
    out = capsys.readouterr().out
    assert h.NO_GROUP in out and "expected no group at all" in out


def test_a_role_user_in_the_wrong_group_stops_the_run(monkeypatch, capsys):
    """The same check in the other direction: a row labelled `Viewer` whose user
    holds `Admin` reports Admin's verdicts under Viewer's name, which is worse than
    not running."""
    rec = _Recorder({h.test_email("Viewer"): ["Admin"]})
    for role in h.ROLES:
        rec._groups.setdefault(h.test_email(role), [role])
    rec._groups[h.test_email(h.NO_GROUP)] = []
    monkeypatch.setattr(h, "create_cognito_user", lambda *a, **k: None)
    monkeypatch.setattr(h, "cognito_groups_for_user", rec.cognito_groups_for_user)
    monkeypatch.setattr(h, "aws", rec.aws)
    monkeypatch.setattr(h, "enable_admin_auth", lambda ctx: None)
    with pytest.raises(RuntimeError, match="group membership"):
        h.setup_users(_stack_ctx(), {})
    assert "Viewer user holds ['Admin']" in capsys.readouterr().out


def test_teardown_deletes_the_groupless_user(seeded, capsys):
    """⭐ Create nothing you do not delete. A groupless account left in a shared
    user pool is a standing credential that every `ANY` operation admits, and the
    teardown list was written out by hand — so a principal added to the matrix and
    not to that list leaks one per run."""
    h.teardown_users(_stack_ctx())
    capsys.readouterr()
    assert h.test_email(h.NO_GROUP) in seeded.deleted


def test_teardown_deletes_every_principal_the_matrix_drives(seeded, capsys):
    """Derived rather than spelled out, so the next principal is covered too."""
    h.teardown_users(_stack_ctx())
    capsys.readouterr()
    missing = [
        h.test_email(p)
        for p in h.MATRIX_PRINCIPALS
        if h.test_email(p) not in seeded.deleted
    ]
    assert not missing, f"teardown left these users behind: {missing}"


def test_teardown_deletes_the_seeded_annotator_row(seeded, capsys):
    """The row is DynamoDB litter in a live stack's own UsersTable, and it carries a
    scope, so leaving it behind would also pre-authorize whatever annotator account
    next uses that address."""
    ctx = _stack_ctx()
    h.setup_users(ctx, yaml.safe_load(_EXPECTATIONS.read_text())["operations"])
    assert ctx.get("_annotator_scope_keys"), "the seeding recorded no key to delete"
    recorded = ctx["_annotator_scope_keys"]
    h.teardown_users(ctx)
    capsys.readouterr()
    import json as _json

    deleted = [_json.loads(k) for k in seeded.deleted_keys]
    for key in recorded:
        assert key in deleted, f"{key} was seeded and not deleted"


def test_teardown_only_recovers_the_annotator_row_from_the_table(seeded, capsys):
    """`--teardown-only`, and a rerun after a killed process, gets a fresh ctx that
    recorded nothing. The row is still findable by the one thing that does not change
    between runs — the Annotator user's address — so the recovery query has to be
    issued for it, not only for the scoped user's.

    The recovered KEY is asserted too, not just the query. A query that found the row
    and then built the wrong key deletes nothing, while teardown prints a count and
    reports success — the same end state as never looking.
    """
    import json as _json

    h.teardown_users(_stack_ctx())
    capsys.readouterr()
    queried = [arg for call in seeded.queries for arg in call if isinstance(arg, str)]
    assert any(h.test_email(h.ANNOTATOR_ROLE) in arg for arg in queried), (
        "teardown never looked for the Annotator's seeded row, so a run that died "
        "before teardown leaves it in the table forever"
    )
    deleted = [_json.loads(k) for k in seeded.deleted_keys]
    # No `setup_users` here: this is the --teardown-only shape, where nothing
    # recorded a key, so the stub answers with its address-derived id.
    local = h.test_email(h.ANNOTATOR_ROLE).split("@")[0]
    row_key = {"PK": {"S": f"USER#uid-{local}"}, "SK": {"S": f"USER#uid-{local}"}}
    assert row_key in deleted, (
        f"the recovered row key was not the one deleted: {deleted}"
    )


def test_teardown_sweeps_a_users_table_row_for_every_address_it_owns(seeded, capsys):
    """⭐ Not only the two rows the harness SEEDS — the matrix writes the rest.

    `listUsers[Admin]` runs the resolver's `sync_cognito_users_to_dynamodb`, which
    creates a UsersTable row for every Cognito user in the pool, and
    `createUser[Admin]` writes one directly. Deleting the Cognito user leaves those
    rows behind, so a teardown that swept only the seeded addresses left one row per
    principal per run in a shared account — measured on a live stack, including a set
    dated nearly a month before the run that found them.

    The groupless principal is what made this worth fixing rather than noting: it is
    a new address, so it would have been a new orphaned row every run.
    """
    import json as _json

    h.teardown_users(_stack_ctx())
    capsys.readouterr()
    deleted = {_json.dumps(_json.loads(k), sort_keys=True) for k in seeded.deleted_keys}
    missing = []
    for role in [*h.MATRIX_PRINCIPALS, h.SCOPED, h.USER_B, h.CREATED]:
        local = h.test_email(role).split("@")[0]
        key = {"PK": {"S": f"USER#uid-{local}"}, "SK": {"S": f"USER#uid-{local}"}}
        if _json.dumps(key, sort_keys=True) not in deleted:
            missing.append(role)
    assert not missing, (
        f"teardown swept no UsersTable row for these addresses it owns: {missing}"
    )


def test_teardown_deletes_each_distinct_item_once(seeded, capsys):
    """The recorded seeded keys and the swept keys overlap for the two seeded
    addresses, so the count teardown prints would double-report them. De-duplicated,
    so the number a reader sees is items aimed at rather than calls made."""
    import json as _json

    ctx = _stack_ctx()
    h.setup_users(ctx, yaml.safe_load(_EXPECTATIONS.read_text())["operations"])
    h.teardown_users(ctx)
    out = capsys.readouterr().out
    keys = [_json.dumps(_json.loads(k), sort_keys=True) for k in seeded.deleted_keys]
    assert len(keys) == len(set(keys)), f"an item was deleted twice: {keys}"
    assert f"{len(keys)} UsersTable item(s)" in out


# ===========================================================================
# the Annotator's seeded test-set scope
# ===========================================================================
def test_the_seeded_scope_covers_every_test_set_id_the_matrix_sends(expectations):
    """Derived from the arguments, so the two cannot drift.

    `assert_can_access_test_set` requires an Annotator to name the test set
    explicitly, so the seeded scope has to be exactly the set of ids the requests
    carry. Seeded from a written-down list instead, changing an operation's
    `testSetId` in the YAML would silently turn seven cells from "the group floor
    admitted this caller" into "a per-object control refused the object", with no
    diff anywhere near the seeding.
    """
    derived = h.test_set_ids_in_play(expectations)
    assert derived, "no testSetId found in the expectations file"
    # Independently recomputed here, including the nested `input:` maps, because a
    # bug in the harness's walk would otherwise be compared against itself.
    expected = set()
    for cfg in expectations.values():
        args = cfg.get("args") or {}
        for container in (args, args.get("input") or {}):
            value = container.get("testSetId") if isinstance(container, dict) else None
            if isinstance(value, str) and value:
                expected.add(value)
    assert set(derived) == expected, (
        f"the seeded Annotator scope would be {sorted(derived)} while the matrix "
        f"sends {sorted(expected)}"
    )


def test_the_id_walk_reaches_a_nested_input_map():
    """Four of the seven Annotator operations carry their id inside `input:`.

    On today's arguments a walk that read only the top level would derive the same
    *set*, because both ids the file uses also appear at the top level of some other
    operation — so the comparison above cannot see the difference and would pass with
    the nested leg removed. A synthetic entry whose id appears **only** nested is
    what makes the leg's absence visible, rather than waiting for the first
    operation that needs it.
    """
    assert h.test_set_ids_in_play(
        {"someOp": {"args": {"input": {"testSetId": "nested-only"}}}}
    ) == ["nested-only"]


def test_the_seeded_scope_is_what_gets_written(seeded, expectations, capsys):
    import json as _json

    ctx = _stack_ctx()
    h.setup_users(ctx, expectations)
    capsys.readouterr()
    rows = [_json.loads(call[call.index("--item") + 1]) for call in seeded.put_items]
    annotator = [
        row
        for row in rows
        if (row.get("email") or {}).get("S") == h.test_email(h.ANNOTATOR_ROLE)
    ]
    assert len(annotator) == 1, f"expected one Annotator row, wrote {len(annotator)}"
    written = [v["S"] for v in annotator[0]["allowedTestSets"]["L"]]
    assert written == h.test_set_ids_in_play(expectations)


def test_the_recorded_teardown_key_is_the_key_of_the_row_that_was_written(
    seeded, expectations, capsys
):
    """The key teardown deletes and the item the seeding wrote must be the same item.

    Teardown compares the two only through its own recorded value, so a seeding that
    recorded one key and wrote a row under another would delete nothing and report a
    delete — the row stays in a live stack's UsersTable with a scope on it, and the
    count teardown prints says it was removed. Read off the `put-item` payload rather
    than off the recorded key, so the two literals are compared to each other.
    """
    import json as _json

    ctx = _stack_ctx()
    h.setup_users(ctx, expectations)
    capsys.readouterr()
    rows = [_json.loads(call[call.index("--item") + 1]) for call in seeded.put_items]
    annotator = [
        row
        for row in rows
        if (row.get("email") or {}).get("S") == h.test_email(h.ANNOTATOR_ROLE)
    ]
    assert len(annotator) == 1
    written = annotator[0]
    recorded = ctx["_annotator_scope_keys"]
    assert recorded == [{"PK": written["PK"], "SK": written["SK"]}], (
        f"the key recorded for teardown is not the written row's key: {recorded} vs "
        f"{ {'PK': written['PK'], 'SK': written['SK']} }"
    )


def test_the_object_scoped_validator_passes_on_the_committed_file(expectations):
    """Non-vacuity in the passing direction, which the refusal tests cannot give.

    A validator that rejected every declaration — including a correct one — would
    satisfy both refusal tests below by exiting on anything, and would make the live
    harness unrunnable. Driven over the real `operations:` map, post-resolution not
    needed: the three declarations here name a role their own `groups:` list
    carries literally.
    """
    h._check_object_scoped_roles(expectations)


def test_a_non_list_object_scoped_roles_is_refused():
    """A scalar is the shape a hand edit produces (`object_scoped_roles: Annotator`),
    and iterating a string would name one principal per CHARACTER — none of which is
    a principal, so the key would be silently inert."""
    with pytest.raises(SystemExit) as exited:
        h._check_object_scoped_roles(
            {
                "claimReview": {
                    "groups": ["Admin", h.ANNOTATOR_ROLE],
                    "object_scoped_roles": h.ANNOTATOR_ROLE,
                }
            }
        )
    assert exited.value.code == 2


def test_the_seeded_row_is_written_to_the_users_table_and_records_the_sub(
    seeded, expectations, capsys
):
    """Two details of the write that no verdict depends on and the stack does.

    The row has to land in the table the stack resolved — a put at a different
    table, or with the flag mis-spelled, is an error the harness would report as a
    WARN-free success and the Annotator's cells would then all measure the
    per-object control. And it records `cognitoSub` the way `user_management`
    writes one, so `resolve_allowed_config_versions` reading this row by address
    finds the sub it expects rather than an absent one.
    """
    import json as _json

    h.setup_users(_stack_ctx(), expectations)
    capsys.readouterr()
    for call in seeded.put_items:
        assert "--table-name" in call
        assert call[call.index("--table-name") + 1] == "UsersTable"
    rows = [_json.loads(call[call.index("--item") + 1]) for call in seeded.put_items]
    annotator = [
        row
        for row in rows
        if (row.get("email") or {}).get("S") == h.test_email(h.ANNOTATOR_ROLE)
    ]
    assert annotator and annotator[0].get("cognitoSub") == {"S": "sub-xyz"}, (
        f"the seeded Annotator row does not record the pool's own sub: {annotator}"
    )


def test_teardown_deletes_from_the_users_table(seeded, capsys):
    """Same point on the way out: a delete aimed elsewhere removes nothing while
    teardown prints a count saying it did."""
    h.teardown_users(_stack_ctx())
    capsys.readouterr()
    assert seeded.delete_tables, "teardown issued no delete-item at all"
    assert set(seeded.delete_tables) == {"UsersTable"}


def test_the_seeded_annotator_row_carries_no_config_version_scope(
    seeded, expectations, capsys
):
    """Two scope axes, and only one of them is being set up here.

    `allowedConfigVersions` on this row would restrict the Annotator on the
    configuration axis as well, turning its `getConfigVersion` cell from "the group
    floor admits an Annotator" into a config-scope assertion — a different property,
    reported under the first one's name. Absence is what every ordinary user has.
    """
    import json as _json

    h.setup_users(_stack_ctx(), expectations)
    capsys.readouterr()
    rows = [_json.loads(call[call.index("--item") + 1]) for call in seeded.put_items]
    for row in rows:
        if (row.get("email") or {}).get("S") == h.test_email(h.ANNOTATOR_ROLE):
            assert "allowedConfigVersions" not in row


def test_an_unscoped_annotator_really_is_refused_by_the_per_object_control():
    """The premise the seeding rests on, computed rather than asserted in prose.

    If an Annotator with no `allowedTestSets` were simply unrestricted, the seeding
    would be pointless ceremony and the seven cells would measure the group floor
    either way. The library's actual rule is the opposite — an explicit scope is
    required and its absence denies — which is why the seeding exists. Driven
    through the real `idp_common.testset_scope`, with the scope lookup answering
    from an in-memory table.
    """
    sys.path.insert(0, str(_REPO / "lib" / "idp_common_pkg"))
    from idp_common import testset_scope

    class _Table:
        def __init__(self, scope):
            self._scope = scope

        def query(self, **kwargs):
            return {"Items": [{"allowedTestSets": self._scope}] if self._scope else []}

    event = {
        "identity": {
            "claims": {
                "cognito:groups": ["Annotator"],
                "email": "annot@example.invalid",
            }
        }
    }
    target = h.test_set_ids_in_play(
        yaml.safe_load(_EXPECTATIONS.read_text())["operations"]
    )[0]

    testset_scope.clear_scope_cache()
    with pytest.raises(testset_scope.TestSetAccessDenied):
        testset_scope.assert_can_access_test_set(event, target, _Table(None))

    # ...and admitted once the scope the harness seeds is in place, or the seeding
    # would not fix anything.
    testset_scope.clear_scope_cache()
    testset_scope.assert_can_access_test_set(event, target, _Table([target]))
    testset_scope.clear_scope_cache()


# ===========================================================================
# object_scoped_roles: the declared polarity, and its two-sided assertion
# ===========================================================================
REVIEW_OPERATIONS = ("completeSectionReview", "claimReview", "releaseReview")


def test_the_three_review_mutations_declare_the_annotator_object_scoped(expectations):
    for field in REVIEW_OPERATIONS:
        declared = expectations[field].get("object_scoped_roles")
        assert declared == [h.ANNOTATOR_ROLE], (
            f"{field} no longer declares the Annotator as object-scoped, so its "
            "cell now asserts that the group floor refuses a role the policy "
            f"admits: {declared!r}"
        )
        assert expectations[field].get("note"), (
            f"{field} declares object_scoped_roles with no note saying why — one "
            "operation's worth of reason is the rule here"
        )


def test_the_declaration_is_only_on_operations_that_admit_the_role(expectations):
    """Universe closure over the key. On a role the policy EXCLUDES, the cell would
    assert that the group floor is not what refuses a caller the floor exists to
    refuse — the exact inversion, scored as a pass."""
    for field, cfg in expectations.items():
        for role in cfg.get("object_scoped_roles") or []:
            groups = cfg.get("groups")
            assert isinstance(groups, list) and role in groups, (
                f"{field} declares '{role}' object-scoped while its policy is "
                f"{groups!r}"
            )


def test_the_harness_refuses_a_declaration_on_an_excluded_role():
    """⭐ And the check is armed, not merely written: it exits non-zero."""
    with pytest.raises(SystemExit) as exited:
        h._check_object_scoped_roles(
            {"claimReview": {"groups": ["Admin"], "object_scoped_roles": ["Annotator"]}}
        )
    assert exited.value.code == 2


def test_the_harness_refuses_an_unknown_operation_key():
    """The typo that would make `object_scoped_roles` vanish rather than misfire.

    Nothing validated the keys of an `operations:` entry, and two of them invert a
    cell's expected polarity, so `object_scoped_role:` would have left the matrix
    asserting the ordinary thing with nothing printed. The scanner already enforces
    this for the Function-URL section for the same reason.
    """
    with pytest.raises(SystemExit) as exited:
        h._reject_unknown_operation_keys(
            {"claimReview": {"groups": ["Admin"], "object_scoped_role": ["Annotator"]}}
        )
    assert exited.value.code == 2
    assert "object_scoped_roles" in sorted(h.OPERATION_ENTRY_KEYS)


def test_every_key_the_real_file_uses_is_a_known_key(expectations):
    """Non-vacuity for the check above: it must pass on the committed file, or it
    would be a gate nobody can leave green."""
    h._reject_unknown_operation_keys(expectations)


@pytest.fixture
def object_scoped_ops():
    return {
        "claimReview": {
            "groups": ["Admin", "Reviewer", h.ANNOTATOR_ROLE],
            "args": {},
            "object_scoped_roles": [h.ANNOTATOR_ROLE],
        }
    }


def _object_scoped_row(monkeypatch, capsys, ops, annotator_answer):
    call = _responder(
        **{
            p: h.ApiResult(200, None, None, "rid")
            for p in h.MATRIX_PRINCIPALS
            if p != h.ANNOTATOR_ROLE
        },
        **{h.ANNOTATOR_ROLE: annotator_answer},
    )
    results = _drive(monkeypatch, capsys, ops, call)
    rows = _rows(results, h.ANNOTATOR_ROLE)
    assert len(rows) == 1
    return rows[0]


def test_an_object_scoped_cell_passes_on_an_attributed_resolver_refusal(
    monkeypatch, capsys, object_scoped_ops
):
    """The positive arm: the floor admitted the role and a per-object control
    refused the object, which is the correct answer for this argument."""
    row = _object_scoped_row(
        monkeypatch,
        capsys,
        object_scoped_ops,
        h.ApiResult(403, "Unauthorized", None, "rid", control=h.CONTROL_RESOLVER),
    )
    assert row["outcome"] == "PASS", row["detail"]


def test_an_object_scoped_cell_fails_when_nothing_refused(
    monkeypatch, capsys, object_scoped_ops
):
    """⭐ The escalation this cell is worth having for: an annotator assigned to no
    test set containing this document reached a review mutation on it."""
    row = _object_scoped_row(
        monkeypatch, capsys, object_scoped_ops, h.ApiResult(200, None, None, "rid")
    )
    assert row["outcome"] == "FAIL"
    assert "NOT REFUSED" in row["detail"]


@pytest.mark.parametrize(
    "answer",
    [
        h.ApiResult(404, "ResourceNotFound", None, "rid"),
        h.ApiResult(400, "BadRequest", None, "rid"),
    ],
)
def test_an_object_scoped_cell_fails_on_a_four_xx_that_is_not_a_refusal(
    monkeypatch, capsys, object_scoped_ops, answer
):
    """⭐ "Any 4xx is a refusal" is the wrong reading, and it is the LIVE shape of
    the regression this cell exists to catch.

    The matrix names a document that does not exist. If the per-object control
    stopped firing — the scope call deleted from the branch, the role added to the
    set the check exempts — the operation would go on to look the document up and
    answer a plain not-found. That is `404`/`ResourceNotFound`, a 4xx, and a cell
    scoring any 4xx as "refused" would report the control as working while it had
    been removed. Only `_denied` — 401/403, an `Unauthorized` errorType, or the
    in-band 200 denial — counts.
    """
    row = _object_scoped_row(monkeypatch, capsys, object_scoped_ops, answer)
    assert row["outcome"] == "FAIL", (
        f"a {answer[0]} that is not a refusal was accepted as one: {row['detail']}"
    )
    assert "NOT REFUSED" in row["detail"]


def test_an_object_scoped_cell_fails_when_the_group_floor_refuses(
    monkeypatch, capsys, object_scoped_ops
):
    """⭐ The other direction: the field-level floor refusing a role the policy
    admits means the manifest stopped carrying it, and a cell that accepted any 403
    would read that as the expected per-object refusal."""
    row = _object_scoped_row(
        monkeypatch,
        capsys,
        object_scoped_ops,
        h.ApiResult(403, "Unauthorized", None, "rid", control=h.CONTROL_GROUP_FLOOR),
    )
    assert row["outcome"] == "FAIL"
    assert "GROUP FLOOR" in row["detail"]


def test_an_object_scoped_cell_cannot_be_run_on_an_unattributed_refusal(
    monkeypatch, capsys, object_scoped_ops
):
    """An absent label cannot tell the two cases apart, so it cannot pass.

    Recorded as a check that could not be run rather than as a failure, which is
    what is true and is no weaker: `hard_failures` counts ERROR with FAIL, so the
    gate still goes red. The distinction matters against a stack deployed before
    `authzControl` existed, where EVERY 403 arrives unlabelled — a report whose
    Control column is empty throughout is one where this whole distinction was
    inert, and calling that a broken group check would be as wrong as calling it a
    pass.
    """
    row = _object_scoped_row(
        monkeypatch,
        capsys,
        object_scoped_ops,
        h.ApiResult(403, "Unauthorized", None, "rid", control=h.CONTROL_UNLABELLED),
    )
    assert row["outcome"] == "ERROR"
    assert row["inconclusive"] is True
    assert row["outcome"] != "PASS"
    assert h.CONTROL_FIELD in row["detail"]
    assert h.hard_failures([row]), "an unevaluable cell left the gate green"


@pytest.mark.parametrize(
    "answer,outcome",
    [
        # A 5xx is registered against GAP-SEC-INCONCLUSIVE-5XX, so it is a WARN —
        # visible, counted separately, carrying `inconclusive`, and never a pass. A
        # timeout is registered against nothing and blocks.
        (h.ApiResult(500, "InternalError", None, "rid"), "WARN"),
        (h.ApiResult(0, None, None, "<timeout>"), "ERROR"),
    ],
)
def test_an_object_scoped_cell_is_not_a_pass_on_an_inconclusive_answer(
    monkeypatch, capsys, object_scoped_ops, answer, outcome
):
    """⭐ "Any non-2xx is a refusal" is the cheapest wrong reading available to a
    cell that asserts a refusal, and it is how 43 cells of a published snapshot read
    `500 ✅`. These answers establish nothing here, exactly as everywhere else."""
    row = _object_scoped_row(monkeypatch, capsys, object_scoped_ops, answer)
    assert row["outcome"] == outcome
    assert row["outcome"] != "PASS"
    assert row["inconclusive"] is True


def test_the_other_roles_on_an_object_scoped_operation_keep_the_ordinary_verdict(
    monkeypatch, capsys, object_scoped_ops
):
    """The key is per (operation, role). Declaring it must not relax the operation
    for Admin and Reviewer, whose cells still assert that an admitted caller is not
    refused at all."""
    call = _responder(
        **{
            p: h.ApiResult(403, "Unauthorized", None, "rid", control=h.CONTROL_RESOLVER)
            for p in h.MATRIX_PRINCIPALS
        }
    )
    results = _drive(monkeypatch, capsys, object_scoped_ops, call)
    for principal in ("Admin", "Reviewer"):
        row = _rows(results, principal)[0]
        assert row["outcome"] == "FAIL", (
            f"{principal} was refused an operation its policy admits and the cell "
            f"did not report it: {row['detail']}"
        )
    assert _rows(results, h.ANNOTATOR_ROLE)[0]["outcome"] == "PASS"


def test_the_declared_premise_matches_the_resolver_source():
    """The declaration says the per-object control runs on each of these routes.

    Read out of the Lambda's source with `ast`, not grepped: a mention of
    `_assert_annotator_scope` in a comment or a docstring satisfies a substring
    search, and this file has several. If a route stops calling it, the refusal the
    live cell expects stops happening and the cell becomes a leak report — so the
    premise is checked here rather than trusted from the YAML note.
    """
    tree = ast.parse(_REVIEW_LAMBDA.read_text())
    handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "handler"
    )
    calls = [
        node.func.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert calls.count("_assert_annotator_scope") >= 3, (
        "complete_section_review's handler no longer calls _assert_annotator_scope "
        "on three routes, so the refusal the object_scoped_roles cells expect may "
        f"not happen: {calls}"
    )


# ===========================================================================
# the Cognito helper itself, which the stubs above never execute
# ===========================================================================
def _rbac_common():
    """`scripts/rbac_common.py`, loaded by path like the harness loads it."""
    return _load("_rbac_common_groupless", _REPO / "scripts" / "rbac_common.py")


class _CliRecorder:
    def __init__(self, groups=()):
        self.calls = []
        self._groups = list(groups)

    def aws(self, *args, region=None):
        self.calls.append(args)
        if args[:2] == ("cognito-idp", "admin-list-groups-for-user"):
            return {"Groups": [{"GroupName": g} for g in self._groups]}
        return {}

    def run(self, cmd, **kwargs):
        self.calls.append(tuple(cmd))

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Done()


def _cli_ctx():
    return {"user_pool": "pool", "region": "us-west-2"}


@pytest.mark.parametrize("group", [None, ""])
def test_create_cognito_user_adds_no_group_when_none_is_asked_for(monkeypatch, group):
    """The one line that makes a groupless principal possible.

    Asserted on the call NOT being made, because `admin-add-user-to-group` is what
    would make this user a role user — and the harness's own tests stub this helper
    out entirely, so without this the branch has no offline coverage at all. Both
    falsy spellings, since a caller reading the group from a mapping gets `""`.
    """
    common = _rbac_common()
    rec = _CliRecorder()
    monkeypatch.setattr(common, "aws", rec.aws)
    monkeypatch.setattr(common.subprocess, "run", rec.run)
    common.create_cognito_user(_cli_ctx(), "nobody@example.invalid", group, "pw")
    assert not [c for c in rec.calls if "admin-add-user-to-group" in c], (
        f"a group was added for a groupless user: {rec.calls}"
    )
    # Non-vacuity: it still created the user and set the password, so "no calls at
    # all" cannot satisfy the assertion above.
    assert [c for c in rec.calls if "admin-create-user" in c]
    assert [c for c in rec.calls if "admin-set-user-password" in c]


def test_create_cognito_user_still_adds_a_named_group(monkeypatch):
    """The control for the test above: skipping the add for EVERY caller would turn
    all five role users groupless, which is the same defect wearing the opposite
    sign."""
    common = _rbac_common()
    rec = _CliRecorder()
    monkeypatch.setattr(common, "aws", rec.aws)
    monkeypatch.setattr(common.subprocess, "run", rec.run)
    common.create_cognito_user(_cli_ctx(), "someone@example.invalid", "Viewer", "pw")
    added = [c for c in rec.calls if "admin-add-user-to-group" in c]
    assert added and "Viewer" in added[0]


def test_the_group_read_back_returns_the_names_the_pool_reports(monkeypatch):
    common = _rbac_common()
    rec = _CliRecorder(groups=["Viewer", "Admin"])
    monkeypatch.setattr(common, "aws", rec.aws)
    assert common.cognito_groups_for_user(_cli_ctx(), "x@example.invalid") == [
        "Admin",
        "Viewer",
    ]


def test_the_group_read_back_raises_rather_than_answering_no_groups(monkeypatch):
    """⭐ "No groups" is the exact answer the groupless check wants to see, so a
    failed lookup must not be able to produce it. Returning `[]` on an error would
    make every membership verification pass for a pool nobody could read."""
    common = _rbac_common()

    def _boom(*args, **kwargs):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(common, "aws", _boom)
    with pytest.raises(RuntimeError):
        common.cognito_groups_for_user(_cli_ctx(), "x@example.invalid")
