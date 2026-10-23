"""Unit tests for cleanup_stale_idp_buckets — the S3-bucket-leak startup reaper.

Buckets leak independently of stacks: CloudFormation can't delete a non-empty
bucket, so an interrupted `idp-cli delete` leaves the bucket behind even after
the stack is gone (thousands accumulated this way). This reaper is destructive,
so these mock-boto3 tests pin the SAFETY logic: never delete a bucket whose run
still has ANY CloudFormation stack (protected), never delete one younger than
the age gate, always empty versions before deleting, and only touch idp- names.

They also pin the reaper's OUTCOME, which is a separate claim from its safety:
a sweep that could not enumerate the account considered nothing, and must not be
reportable as one that enumerated and found nothing to do (#1391 defect 2).

One thing deliberately NOT pinned, so a green run here is not over-read: whether
an already-set `failure_reason` in main() wins over the sweep's. The verdict flip
is pinned below; the precedence of the one-line reason is not, because asserting
it means either running main() end to end or pinning the polarity of a nested
guard in the AST, and the same unpinned pattern already carries the teardown
verification's reason. Inverting it would mis-attribute a deploy failure's
message, not change whether the run fails.
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: The SDLC pipeline template carrying the CodeBuild role's grants.
PIPELINE_TEMPLATE = Path(__file__).resolve().parents[1] / "cfn" / "codepipeline-s3.yml"


class _FakeStackPaginator:
    def __init__(self, stack_names):
        self._names = stack_names

    def paginate(self, **kwargs):
        return [{"StackSummaries": [{"StackName": n} for n in self._names]}]


class _FakeS3Client:
    def __init__(self, buckets, live_stacks):
        # buckets: list of (name, age_seconds)
        self._buckets = buckets
        self._live_stacks = live_stacks
        self.deleted = []

    # used by _live_idp_run_prefixes
    def get_paginator(self, name):
        assert name == "list_stacks"
        return _FakeStackPaginator(self._live_stacks)

    def list_buckets(self):
        now = datetime.now(tz=timezone.utc)
        return {
            "Buckets": [
                {"Name": n, "CreationDate": now - timedelta(seconds=age)}
                for n, age in self._buckets
            ]
        }

    def delete_bucket(self, Bucket):
        self.deleted.append(Bucket)


class _FakeBucket:
    def __init__(self, name, emptied):
        self._name = name
        self._emptied = emptied

    @property
    def object_versions(self):
        parent = self

        class _OV:
            def delete(self):
                parent._emptied.append(parent._name)

        return _OV()


class _FakeS3Resource:
    def __init__(self):
        self.emptied = []

    def Bucket(self, name):
        return _FakeBucket(name, self.emptied)


def _install(cbd, monkeypatch, buckets, live_stacks):
    client = _FakeS3Client(buckets, live_stacks)
    resource = _FakeS3Resource()

    def fake_client(name, *a, **k):
        # both cloudformation (for _live_idp_run_prefixes) and s3 route to the
        # same fake — get_paginator/list_buckets/delete_bucket don't collide.
        return client

    monkeypatch.setattr(cbd.boto3, "client", fake_client)
    monkeypatch.setattr(cbd.boto3, "resource", lambda name, *a, **k: resource)
    return client, resource


def test_reaps_old_bucket_with_no_live_stack(cbd, monkeypatch):
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, resource = _install(
        cbd,
        monkeypatch,
        buckets=[("idp-0709-160356-inputbucket-abc", old)],
        live_stacks=[],  # no stacks at all → nothing protected
    )
    cbd.cleanup_stale_idp_buckets()
    assert client.deleted == ["idp-0709-160356-inputbucket-abc"]
    # must empty versions BEFORE deleting
    assert resource.emptied == ["idp-0709-160356-inputbucket-abc"]


def test_protects_bucket_whose_run_has_a_live_stack(cbd, monkeypatch):
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[("idp-0716-165247-outputbucket-xyz", old)],
        # the run's primary stack still exists → protect ALL its buckets
        live_stacks=["idp-0716-165247", "idp-0716-165247-headless-iam"],
    )
    cbd.cleanup_stale_idp_buckets()
    assert client.deleted == []


def test_skips_young_bucket_even_without_stack(cbd, monkeypatch):
    young = cbd.IDP_BUCKET_STALE_AGE_SECONDS - 600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[("idp-0716-170000-inputbucket-new", young)],
        live_stacks=[],
    )
    cbd.cleanup_stale_idp_buckets()
    # age gate is the backstop for a brand-new bucket whose stack hasn't
    # registered yet — must not delete it.
    assert client.deleted == []


def test_ignores_non_idp_buckets(cbd, monkeypatch):
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[
            ("some-other-bucket", old),
            ("genaiic-sdlc-sourcecode-020432867916-us-east-1", old),
            ("idp-0709-160356-inputbucket-abc", old),
        ],
        live_stacks=[],
    )
    cbd.cleanup_stale_idp_buckets()
    assert client.deleted == ["idp-0709-160356-inputbucket-abc"]


def test_mixed_batch_only_deletes_safe_ones(cbd, monkeypatch):
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    young = cbd.IDP_BUCKET_STALE_AGE_SECONDS - 600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[
            ("idp-0709-160356-inputbucket-a", old),  # delete
            ("idp-0716-165247-inputbucket-b", old),  # protected (live stack)
            ("idp-0716-170000-inputbucket-c", young),  # too young
            ("idp-0705-120000-outputbucket-d", old),  # delete
        ],
        live_stacks=["idp-0716-165247"],
    )
    cbd.cleanup_stale_idp_buckets()
    assert set(client.deleted) == {
        "idp-0709-160356-inputbucket-a",
        "idp-0705-120000-outputbucket-d",
    }


def test_delete_error_does_not_abort_batch(cbd, monkeypatch):
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[
            ("idp-0709-100000-inputbucket-a", old),
            ("idp-0709-200000-inputbucket-b", old),
        ],
        live_stacks=[],
    )
    orig = client.delete_bucket

    def flaky(Bucket):
        if Bucket.endswith("-a"):
            raise RuntimeError("BucketNotEmpty")
        orig(Bucket)

    monkeypatch.setattr(client, "delete_bucket", flaky)
    cbd.cleanup_stale_idp_buckets()
    # the second bucket still gets deleted despite the first raising
    assert client.deleted == ["idp-0709-200000-inputbucket-b"]


def test_never_raises_on_api_error(cbd, monkeypatch):
    class _Boom:
        def get_paginator(self, name):
            raise RuntimeError("throttled")

        def list_buckets(self):
            raise RuntimeError("throttled")

    monkeypatch.setattr(cbd.boto3, "client", lambda name, *a, **k: _Boom())
    monkeypatch.setattr(cbd.boto3, "resource", lambda name, *a, **k: object())
    cbd.cleanup_stale_idp_buckets()  # must swallow, not raise


# ---------------------------------------------------------------------------
# An inert sweep must not be reportable as a clean one (#1391 defect 2)
#
# The reaper swallowed every failure into one `⚠️ Stale idp- bucket cleanup
# failed` line and returned, so a run whose FIRST call was denied — considering
# zero buckets, establishing nothing about the account — was indistinguishable
# from a run that enumerated the account and found nothing to reap. Both printed
# one line and left a green run.
#
# The two directions below are therefore asserted on the returned OUTCOME, not
# on what was printed: a printed string is the thing that was already there and
# that nobody read. Enumeration failing yields a finding; enumerating and
# finding nothing yields none. A per-bucket delete failure stays best effort and
# yields none either, because one stuck bucket must not cost a deploy.
#
# `_live_idp_run_prefixes` (ListStacks) is enumeration too, and it runs BEFORE
# `list_buckets`: without the protected-prefix set there is no safe selection to
# make, so it is covered in the same class rather than treated as a partial.
# ---------------------------------------------------------------------------


class _EnumerationFailure:
    """A client that answers neither enumeration call.

    `which` picks which of the two fails, so the test can show that both land in
    the same class — the reaper has two enumerations and either one failing
    leaves it with nothing to sweep.
    """

    def __init__(self, which):
        self._which = which
        self.deleted = []

    def get_paginator(self, name):
        if self._which == "stacks":
            raise RuntimeError("AccessDenied: ListStacks")
        return _FakeStackPaginator([])

    def list_buckets(self):
        if self._which == "buckets":
            raise RuntimeError("AccessDenied: ListBuckets")
        return {"Buckets": []}

    def delete_bucket(self, Bucket):  # pragma: no cover - must never be reached
        self.deleted.append(Bucket)


@pytest.mark.parametrize("which", ["buckets", "stacks"])
def test_an_enumeration_failure_is_a_finding_not_a_completed_sweep(
    cbd, monkeypatch, capsys, which
):
    client = _EnumerationFailure(which)
    monkeypatch.setattr(cbd.boto3, "client", lambda name, *a, **k: client)
    monkeypatch.setattr(cbd.boto3, "resource", lambda name, *a, **k: _FakeS3Resource())

    findings = cbd.cleanup_stale_idp_buckets()

    # The outcome, which is what the pipeline reads.
    assert findings, (
        "a sweep that could not enumerate its targets returned no finding, so "
        "the run cannot tell it apart from one that enumerated and found "
        "nothing — the defect itself"
    )
    assert all(f.startswith(cbd.STALE_BUCKET_SWEEP_FINDING) for f in findings)
    assert client.deleted == []
    # And it must not ALSO claim the clean outcome, which is the string a reader
    # skims for.
    assert "No stale idp- test buckets to reap" not in capsys.readouterr().out


def test_a_clean_sweep_is_still_the_quiet_path(cbd, monkeypatch, capsys):
    """Enumerated, nothing stale → no finding and the ordinary ✅ line.

    The direction a careless fix breaks: making every green deploy report a
    cleanup problem is worse than the silence, because it trains the reader to
    ignore the row that matters. "Nothing to reap" is a completed sweep.
    """
    client, _ = _install(cbd, monkeypatch, buckets=[], live_stacks=[])

    findings = cbd.cleanup_stale_idp_buckets()

    assert findings == []
    assert client.deleted == []
    out = capsys.readouterr().out
    assert "✅ No stale idp- test buckets to reap" in out
    assert cbd.STALE_BUCKET_SWEEP_FINDING not in out


def test_a_sweep_that_reaps_everything_it_found_reports_no_finding(cbd, monkeypatch):
    """The other clean direction: targets found, all deleted, nothing to report."""
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[("idp-0709-160356-inputbucket-abc", old)],
        live_stacks=[],
    )

    findings = cbd.cleanup_stale_idp_buckets()

    assert findings == []
    assert client.deleted == ["idp-0709-160356-inputbucket-abc"]


def test_a_per_bucket_delete_failure_stays_best_effort(cbd, monkeypatch, capsys):
    """One stuck bucket is reported and costs nothing else.

    The asymmetry this fix has to preserve. A BucketNotEmpty or a replication
    hold on one bucket must not fail a deploy: the loop names it, the other
    buckets are still reaped, and the next run tries again. So it is NOT a
    finding, and the only thing that distinguishes it from the enumeration
    failure above is which `except` catches it.
    """
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[
            ("idp-0709-100000-inputbucket-a", old),
            ("idp-0709-200000-inputbucket-b", old),
        ],
        live_stacks=[],
    )
    orig = client.delete_bucket

    def flaky(Bucket):
        if Bucket.endswith("-a"):
            raise RuntimeError("BucketNotEmpty")
        orig(Bucket)

    monkeypatch.setattr(client, "delete_bucket", flaky)

    findings = cbd.cleanup_stale_idp_buckets()

    assert findings == []
    assert client.deleted == ["idp-0709-200000-inputbucket-b"]
    out = capsys.readouterr().out
    assert "idp-0709-100000-inputbucket-a" in out  # the stuck one is named
    assert cbd.STALE_BUCKET_SWEEP_FINDING not in out


def test_a_sweep_abandoned_part_way_is_a_finding_too(cbd, monkeypatch):
    """Iteration giving out mid-selection leaves buckets unconsidered.

    Partial is not clean, so it reports for the same reason enumeration does:
    the buckets after the one that broke were never looked at, and the sweep
    cannot be read as one that enumerated and found nothing.

    The vehicle is a listing entry that is not a mapping, which is synthetic —
    the shape that used to arrive here was an undateable `CreationDate`, and
    `_listing_age_seconds` now answers None for it so that one is a single
    skipped bucket (see the test below) rather than an abandoned sweep. What is
    pinned here is the handler, not any particular way of reaching it, so a
    vehicle that still raises mid-selection keeps the claim intact.
    """
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[("idp-0709-160356-inputbucket-abc", old)],
        live_stacks=[],
    )
    now = datetime.now(tz=timezone.utc)
    monkeypatch.setattr(
        client,
        "list_buckets",
        lambda: {
            "Buckets": [
                {
                    "Name": "idp-0709-160356-inputbucket-abc",
                    "CreationDate": now - timedelta(seconds=old),
                },
                # Not a mapping, so selection raises before the delete pass and
                # the bucket above is never attempted.
                "idp-0709-160357-inputbucket-def",
            ]
        },
    )

    findings = cbd.cleanup_stale_idp_buckets()

    assert findings
    assert all(f.startswith(cbd.STALE_BUCKET_SWEEP_FINDING) for f in findings)
    assert client.deleted == []


def test_a_bucket_whose_age_cannot_be_established_is_skipped_not_reaped(
    cbd, monkeypatch, capsys
):
    """A naive `CreationDate` is one skipped bucket, not a reaped one (#1445).

    Both halves matter. The bucket is kept, because an age that cannot be
    established is not the same claim as being old enough to delete and this
    path empties the bucket before deleting it; and the sweep still completes,
    because every bucket the listing returned was considered.
    """
    client, _ = _install(cbd, monkeypatch, buckets=[], live_stacks=[])
    monkeypatch.setattr(
        client,
        "list_buckets",
        lambda: {
            "Buckets": [
                {
                    "Name": "idp-0709-160356-inputbucket-abc",
                    "CreationDate": datetime(2026, 1, 1),  # noqa: DTZ001
                }
            ]
        },
    )

    findings = cbd.cleanup_stale_idp_buckets()

    assert findings == []
    assert client.deleted == []
    out = capsys.readouterr().out
    assert "idp-0709-160356-inputbucket-abc" in out
    assert "age not establishable" in out
    # Not misreported as the routine case, which needs no attention.
    assert "younger than the age gate" not in out


# ---------------------------------------------------------------------------
# The pipeline has to surface it, or the outcome is as inert as the warning was
# ---------------------------------------------------------------------------


def _summary(cbd, findings):
    """The consolidated status table — printed, uploaded to S3, mailed on fail."""
    primary = {
        "stack_name": "idp-0101-000000",
        "success": True,
        "step_results": {"Step 3: Default config": {"status": "passed", "error": ""}},
        "teardown": {"retained": False, "findings": []},
    }
    # Keyed by sweep since #1434 made the table cover all six Step 0 sweeps; the
    # other five are absent here, which is their "not reported" state and renders
    # rows `_sweep_rows` does not match.
    return cbd.build_consolidated_summary(
        "idp-0101-000000", primary, [], True, {"cleanup_stale_idp_buckets": findings}
    )


def _sweep_rows(text):
    """The table's sweep row(s), so the ROW's own status can be asserted.

    The overall verdict is not enough on its own, in either direction. A row
    rendered `✅` beside a finding still flips OVERALL, so the verdict alone
    passes a table that ticks a sweep it is simultaneously failing the run for;
    and a row rendered `❌` on a clean sweep does not flip anything, so the
    verdict alone passes a table that reports a problem on every green deploy.
    Both were live survivors of the mutation sample.
    """
    return [line for line in text.splitlines() if "Stale idp- bucket sweep" in line]


def test_the_summary_fails_the_run_on_a_sweep_finding(cbd):
    text = _summary(cbd, [f"{cbd.STALE_BUCKET_SWEEP_FINDING} could not enumerate"])
    assert "OVERALL: FAIL" in text
    assert "could not enumerate" in text
    rows = _sweep_rows(text)
    assert rows and all(row.strip().startswith("❌") for row in rows), rows


def test_the_summary_passes_on_a_completed_sweep(cbd):
    text = _summary(cbd, [])
    assert "OVERALL: PASS" in text
    assert cbd.STALE_BUCKET_SWEEP_FINDING not in text
    # The row itself must read as a pass. A ❌ on every clean sweep is the
    # careless-fix direction: it trains the reader to ignore the row, which is
    # how the warning that preceded it came to be ignored.
    rows = _sweep_rows(text)
    assert len(rows) == 1 and rows[0].strip().startswith("✅"), rows


def test_the_summary_does_not_claim_a_sweep_nobody_reported(cbd):
    """`None` is "the caller does not have the result", not "clean".

    Collapsing those two would reproduce the defect one level up: a table that
    says the sweep was fine because nothing told it otherwise. So the row may
    not carry the pass tick, and the detail must say so in words.
    """
    text = _summary(cbd, None)
    assert "OVERALL: PASS" in text
    assert "enumerated, swept" not in text
    rows = _sweep_rows(text)
    assert len(rows) == 1, rows
    assert "not reported" in rows[0]
    assert not rows[0].strip().startswith("✅"), rows[0]


# ---------------------------------------------------------------------------
# main() must actually consult it
#
# A return value nothing reads is the same inert control one level up, and that
# is the recurring defect class here. Asserted over main's AST rather than its
# text: the question is whether the value is bound and then used to decide the
# run, which a substring search cannot answer.
# ---------------------------------------------------------------------------

HARNESS = Path(__file__).resolve().parents[1] / "codebuild_deployment.py"


def _main_function():
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    main = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        ),
        None,
    )
    assert main is not None, f"no main() in {HARNESS.name}"
    return main


def _sweep_result_name(main):
    """The local main() binds cleanup_stale_idp_buckets()'s return value to."""
    for node in ast.walk(main):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "cleanup_stale_idp_buckets"
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            return node.targets[0].id
    return None


def test_main_binds_the_sweep_result_instead_of_discarding_it():
    main = _main_function()
    name = _sweep_result_name(main)
    assert name, (
        "main() calls cleanup_stale_idp_buckets() as a bare statement, so its "
        "findings go nowhere and the sweep's outcome cannot reach the verdict "
        "— the shape of #1391 defect 2"
    )


def test_main_fails_the_run_on_a_sweep_finding():
    """The bound name must gate an assignment of `stack_success = False`."""
    main = _main_function()
    name = _sweep_result_name(main)
    assert name

    def _tests_on(node):
        return name in {
            inner.id for inner in ast.walk(node.test) if isinstance(inner, ast.Name)
        }

    def _assigns_failure(node):
        return any(
            isinstance(stmt, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "stack_success"
                for t in stmt.targets
            )
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is False
            for stmt in ast.walk(node)
        )

    guarded = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.If) and _tests_on(node) and _assigns_failure(node)
    ]
    assert guarded, (
        f"no `if {name}:` in main() sets stack_success = False, so a sweep that "
        f"considered zero buckets still leaves the run green — which is the "
        f"defect, not the fix"
    )

    # And it must carry a reason, so the 💥 line and the failure email say what
    # went wrong rather than just that something did. Whether an already-set
    # reason wins is NOT pinned here — see the module docstring note.
    assert any(
        isinstance(stmt, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "failure_reason" for t in stmt.targets
        )
        for node in guarded
        for stmt in ast.walk(node)
    ), (
        f"the `if {name}:` block in main() sets no failure_reason, so the run "
        f"goes red with nothing naming the sweep as the cause"
    )


def _names_carrying(main, name):
    """`name` plus any local main() assigns a value that mentions it.

    One hop, because since #1434 the six Step 0 sweeps reach the table through a
    mapping keyed by sweep rather than one argument each. The claim asserted
    below is unchanged — the sweep's outcome has to arrive at the table — and
    only the route is wider. That the mapping actually carries THIS sweep under
    THIS key is pinned in test_startup_sweep_outcomes.py, over every sweep in the
    registry rather than this one.
    """
    carried = {name}
    for node in ast.walk(main):
        if not isinstance(node, ast.Assign):
            continue
        mentioned = {
            inner.id for inner in ast.walk(node.value) if isinstance(inner, ast.Name)
        }
        if carried & mentioned:
            carried |= {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
    return carried


def test_main_passes_the_sweep_result_to_the_consolidated_summary():
    """The row in the published table, as well as the exit code."""
    main = _main_function()
    name = _sweep_result_name(main)
    assert name
    carried = _names_carrying(main, name)

    passed_to = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_consolidated_summary"
        and carried
        & {
            inner.id
            for arg in [*node.args, *(kw.value for kw in node.keywords)]
            for inner in ast.walk(arg)
            if isinstance(inner, ast.Name)
        }
    ]
    assert passed_to, (
        f"no build_consolidated_summary(...) call in main() is given {name}, so "
        f"the status table — the artifact a reader opens, and the one mailed on "
        f"failure — cannot show whether the sweep ran"
    )


# ---------------------------------------------------------------------------
# The permission the reaper cannot work without
#
# ⚠️ `cleanup_stale_idp_buckets` calls `list_buckets` FIRST, and if that is
# denied the reaper logs one line and returns having reaped nothing. Every test
# above mocks boto3, so all of them pass against a role that cannot make the
# call at all — which is exactly what happened: the grant was missing, the
# reaper did nothing on every run for months, and the only symptom was `idp-`
# buckets accumulating (14 of them, the oldest three months old, when this was
# finally measured).
#
# `s3:ListAllMyBuckets` is an account-level operation with no resource to scope
# to, so it needs `Resource: '*'`. The role's other S3 grants are bucket-scoped
# — including an `s3:*` — and a bucket-scoped wildcard cannot cover it. Pinning
# the grant is the only offline check that the reaper is able to run.
# ---------------------------------------------------------------------------

#: The role the CodeBuild project runs as, and so the only one whose grants
#: decide whether the reaper can make its call. That is a premise about
#: `ServiceRole` on the project, not about this constant, so the test asserts it
#: rather than assuming it -- repointing `ServiceRole` at the supplied-role
#: parameter denies the reaper again while every grant named here is untouched.
REAPER_ROLE = "CodeBuildRole"
REAPER_ACTION = "s3:ListAllMyBuckets"

#: Accepted in place of the specific action: a consolidated `s3:*` still grants
#: it. Without this, narrowing *or widening* the statement both fail, and the
#: widening case would report "no longer granted s3:ListAllMyBuckets" about a
#: policy that grants it — sending the reader after the wrong thing.
REAPER_ACTION_EQUIVALENTS = frozenset({REAPER_ACTION, "s3:*"})


def _as_list(value):
    """CloudFormation accepts a scalar wherever it accepts a list of them."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _statements_for_role(template: dict, role: str) -> list[dict]:
    """Every IAM statement attached to `role`, by either of the two shapes.

    A grant can reach a role inline (`AWS::IAM::Role.Properties.Policies`) or by
    a separate `AWS::IAM::Policy` naming it in `Roles`, and this template uses
    both for the same role. Collecting the two together is the whole point: the
    question is what the role can do, not where somebody wrote it down.
    """
    resources = template.get("Resources") or {}
    statements: list[dict] = []

    role_body = resources.get(role) or {}
    for policy in _as_list((role_body.get("Properties") or {}).get("Policies")):
        statements.extend(
            _as_list((policy.get("PolicyDocument") or {}).get("Statement"))
        )

    for body in resources.values():
        if body.get("Type") != "AWS::IAM::Policy":
            continue
        props = body.get("Properties") or {}
        # `Roles: [!Ref CodeBuildRole]` parses to [{'Ref': 'CodeBuildRole'}].
        attached = {
            entry.get("Ref")
            for entry in _as_list(props.get("Roles"))
            if isinstance(entry, dict)
        }
        if role in attached:
            statements.extend(
                _as_list((props.get("PolicyDocument") or {}).get("Statement"))
            )

    return statements


#: Routes to a role this collector deliberately does not model:
#:
#:   * `Roles: [!Sub '${CodeBuildRole}']` rather than `!Ref` (same value),
#:   * a statement or policy wrapped in `Fn::If`,
#:   * an `AWS::IAM::ManagedPolicy` attached to the role,
#:   * `AWS::IAM::RolePolicy`.
#:
#: Each makes the grant *invisible* here, so the test fails rather than passing
#: -- the safe direction, loud and wrong-way-correct. Written down because the
#: reach of a collector is the thing a reader cannot infer from reading it, and
#: a false failure that names nothing costs an afternoon.
_UNMODELLED_ATTACHMENT_ROUTES = (
    "Fn::If-wrapped policies, !Sub role references, "
    "AWS::IAM::ManagedPolicy, AWS::IAM::RolePolicy"
)


def _referenced_logical_ids(node) -> set[str]:
    """Every logical id a value refers to, through `Ref` or `Fn::GetAtt`.

    ⚠️ **Exact ids, not a substring search.** `ServiceRole` here is an `Fn::If`
    choosing between `!GetAtt CodeBuildRole.Arn` and `!Ref CodeBuildRoleArn`, and
    `CodeBuildRole` is a *prefix* of `CodeBuildRoleArn` -- so asking whether the
    role's name appears anywhere in the stringified value answers yes even when
    the project has been repointed at the supplied-role parameter, which is
    precisely the case this exists to detect. Walking the structure and taking
    the ids whole is the only way to tell those two apart.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                found.add(value)
            elif key == "Fn::GetAtt":
                parts = value if isinstance(value, list) else str(value).split(".")
                if parts:
                    found.add(str(parts[0]))
            else:
                found |= _referenced_logical_ids(value)
    elif isinstance(node, list):
        for item in node:
            found |= _referenced_logical_ids(item)
    return found


def _runs_as_role(template: dict, role: str) -> bool:
    """Whether any CodeBuild project in `template` runs as `role`.

    The premise behind pinning one role's grants. `Fn::If` is not evaluated --
    a project that runs as this role on *either* branch counts, because the
    question is whether the role is still in play at all.
    """
    return any(
        role
        in _referenced_logical_ids((body.get("Properties") or {}).get("ServiceRole"))
        for body in (template.get("Resources") or {}).values()
        if body.get("Type") == "AWS::CodeBuild::Project"
    )


def test_the_codebuild_role_can_list_the_accounts_buckets():
    """The reaper's first call must be permitted *to its own role*, on `Resource: '*'`.

    Parsed rather than matched against the template text, and both halves of
    that matter. A text match cannot tell which role carries a statement, so it
    reports success with the grant sitting on a different role while the reaper's
    role has lost it -- which would make the only protection for this fix a gate
    with exactly the defect the fix exists to remove. And a text match pins YAML
    *formatting*: reordering `Action` and `Resource`, or adding a second action
    to the statement, changes no permission and must not fail.

    `load_template` comes from the sibling module in this directory, which
    `conftest.py` puts on `sys.path` for this purpose; it converts the CFN
    short-form tags, so `!Ref`/`!Sub` arrive as plain dicts.
    """
    from test_iam_trust_policy_partitions import load_template

    template = load_template(PIPELINE_TEMPLATE)

    # The premise first: pinning this role's grants says nothing unless the
    # CodeBuild project still runs as it. Repointing `ServiceRole` at the
    # supplied-role parameter denies the reaper while leaving every grant below
    # exactly as it is, so without this the test passes and the reaper is broken.
    assert REAPER_ROLE in (template.get("Resources") or {}), (
        f"{PIPELINE_TEMPLATE.name} declares no {REAPER_ROLE} resource. Everything "
        f"below is keyed on that logical id, and the grants would still be found "
        f"through a dangling `!Ref` in a policy, so this is checked first."
    )
    assert _runs_as_role(template, REAPER_ROLE), (
        f"no AWS::CodeBuild::Project in {PIPELINE_TEMPLATE.name} runs as "
        f"{REAPER_ROLE}, so pinning that role's grants says nothing about whether "
        f"the reaper can list the account"
    )

    statements = _statements_for_role(template, REAPER_ROLE)
    assert statements, (
        f"no IAM statements found for {REAPER_ROLE} in {PIPELINE_TEMPLATE.name} — "
        f"the role was renamed or the template restructured, and every assertion "
        f"below would pass vacuously"
    )

    granting = [
        statement
        for statement in statements
        if statement.get("Effect") == "Allow"
        and REAPER_ACTION_EQUIVALENTS & set(_as_list(statement.get("Action")))
    ]
    assert granting, (
        f"{REAPER_ROLE} is no longer granted {REAPER_ACTION} (or `s3:*`) in "
        f"{PIPELINE_TEMPLATE.name}. cleanup_stale_idp_buckets calls list_buckets "
        f"as its first action and swallows the AccessDenied, so removing this "
        f"makes the bucket reaper a no-op that still reports success. If the "
        f"grant IS there, check how it reaches the role: this collector reads "
        f"inline `Policies` and an `AWS::IAM::Policy` naming the role via `!Ref`, "
        f"and does not model {_UNMODELLED_ATTACHMENT_ROUTES}."
    )

    assert any("*" in _as_list(statement.get("Resource")) for statement in granting), (
        f"{REAPER_ACTION} is granted to {REAPER_ROLE} but not on `Resource: '*'`. "
        f"It is an account-level operation with no resource to scope to, so any "
        f"ARN — even an `s3:*` on a bucket — evaluates to implicitDeny."
    )
