"""Offline tests for the Terraform migration test driver and its SRP sign-in.

`terraform_migration_test.py` deploys two real deployments, so the only parts that
can be checked without AWS are the ones that decide what a green run means:

  * the SRP client derives the same key a Cognito-side computation does, and its
    timestamp is the format Cognito signs (the day is not zero-padded);
  * the snapshot comparison fails on a lost or changed object or item, and allows
    exactly the one change the migration is meant to make (fix-config's two
    counters on Config# rows), nowhere else;
  * the test's own list of resources that must be kept is checked against the
    command's plan, so a plan that drops one cannot shrink the comparison;
  * the sweep only reaches names this run owns;
  * a failed phase still tears down, new deployment first; ``--stop-after`` and
    ``--keep`` do not; ``--resume`` skips what is done.
"""

import hashlib
import json
import secrets
import string
from datetime import datetime, timezone

import cognito_srp as srp
import pytest
import terraform_migration_test as tmt

pytestmark = pytest.mark.unit


# -- SRP ---------------------------------------------------------------------


def _server_key(pool_name, user_id, password, salt_hex, small_a):
    """Cognito's half of SRP-6a, written independently of the client code."""
    identity = hashlib.sha256(f"{pool_name}{user_id}:{password}".encode()).hexdigest()
    x = int(srp.hash_hex(srp.pad_hex(int(salt_hex, 16)) + identity), 16)
    v = pow(srp.G, x, srp.N)
    b = secrets.randbits(1024)
    big_b = (srp.multiplier() * v + pow(srp.G, b, srp.N)) % srp.N
    big_a = srp.large_a(small_a)
    u = srp.scrambler(big_a, big_b)
    s = pow(big_a * pow(v, u, srp.N), b, srp.N)
    key = srp.hkdf_key(bytes.fromhex(srp.pad_hex(s)), bytes.fromhex(srp.pad_hex(u)))
    return key, big_b


def test_client_key_matches_the_server_side_computation():
    small_a = srp.new_small_a()
    salt = secrets.token_hex(16)
    server, big_b = _server_key("ABCdef123", "user-1", "Pw!1aaaa", salt, small_a)
    client = srp.password_authentication_key(
        "ABCdef123", "user-1", "Pw!1aaaa", salt, big_b, small_a
    )
    assert client == server


def test_a_wrong_password_derives_a_different_key():
    small_a = srp.new_small_a()
    salt = secrets.token_hex(16)
    server, big_b = _server_key("ABCdef123", "user-1", "right", salt, small_a)
    assert (
        srp.password_authentication_key(
            "ABCdef123", "user-1", "wrong", salt, big_b, small_a
        )
        != server
    )


@pytest.mark.parametrize(
    "value,expected",
    [(0x14, "14"), (0xEC, "00ec"), (0x1, "01"), (0x8000, "008000"), (0x7F, "7f")],
)
def test_pad_hex_is_the_java_byte_array_encoding(value, expected):
    assert srp.pad_hex(value) == expected


def test_timestamp_does_not_zero_pad_the_day():
    stamp = srp.timestamp(datetime(2026, 11, 4, 9, 5, 3, tzinfo=timezone.utc))
    assert stamp == "Wed Nov 4 09:05:03 UTC 2026"


def test_a_zero_b_is_refused():
    with pytest.raises(ValueError):
        srp.password_authentication_key("p", "u", "pw", "ab", srp.N, 5)


def test_password_claim_signs_with_the_derived_key():
    small_a = srp.new_small_a()
    salt = secrets.token_hex(16)
    server, big_b = _server_key("ABCdef123", "uid", "pw", salt, small_a)
    secret_block = "c2VjcmV0LWJsb2Nr"
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    claim = srp.password_claim(
        "us-west-2_ABCdef123",
        "pw",
        {
            "SRP_B": format(big_b, "x"),
            "SALT": salt,
            "SECRET_BLOCK": secret_block,
            "USER_ID_FOR_SRP": "uid",
        },
        small_a,
        now,
    )
    import base64
    import hmac

    expected = base64.b64encode(
        hmac.new(
            server,
            b"ABCdef123uid"
            + base64.b64decode(secret_block)
            + srp.timestamp(now).encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert claim["PASSWORD_CLAIM_SIGNATURE"] == expected
    assert claim["USERNAME"] == "uid"
    assert claim["TIMESTAMP"] == "Fri Jan 2 03:04:05 UTC 2026"


# -- names and passwords --------------------------------------------------------


def test_names_that_overlap_are_refused():
    assert tmt.validate_names("gitf2", "gitfnew2") == []
    assert any("overlap" in p for p in tmt.validate_names("gitf", "gitf2new"))
    assert any("longer" in p for p in tmt.validate_names("gitf2", "x" * 26))
    assert any("lowercase" in p for p in tmt.validate_names("Gitf2", "gitfnew2"))


def test_password_has_every_character_class():
    for _ in range(50):
        pw = tmt.make_password()
        assert len(pw) == 24
        assert any(c in string.ascii_uppercase for c in pw)
        assert any(c in string.ascii_lowercase for c in pw)
        assert any(c in string.digits for c in pw)
        assert any(not c.isalnum() for c in pw)


def test_upstream_tfvars_names_the_prefix_region_and_data_features():
    text = tmt.upstream_tfvars("gitf2", "run1", "us-west-2")
    assert 'prefix      = "gitf2"' in text
    assert 'region      = "us-west-2"' in text
    for feature in (
        "create_knowledge_base",
        "enable_test_studio",
        "create_bda_project",
    ):
        assert (
            f"{feature}" in text and "true" in text.split(feature, 1)[1].split("\n")[0]
        )


# -- states and plans -----------------------------------------------------------


def _state():
    def res(tf_type, name, attrs, module="", key=None):
        inst = {"attributes": attrs}
        if key is not None:
            inst["index_key"] = key
        return {
            "mode": "managed",
            "type": tf_type,
            "name": name,
            "module": module,
            "instances": [inst],
        }

    m = "module.genai_idp_accelerator"
    return {
        "resources": [
            res("aws_s3_bucket", "input_bucket", {"bucket": "gitf2-input-x"}),
            res("aws_s3_bucket", "output_bucket", {"bucket": "gitf2-output-x"}),
            res("aws_s3_bucket", "working_bucket", {"bucket": "gitf2-working-x"}),
            res(
                "aws_s3_bucket",
                "evaluation_baseline_bucket",
                {"bucket": "gitf2-base-x"},
            ),
            res("aws_s3_bucket", "test_sets", {"bucket": "gitf2-ts"}, module=m, key=0),
            res(
                "aws_dynamodb_table",
                "tracking_table",
                {"name": "idp-tracking-x"},
                module=m,
            ),
            res(
                "aws_dynamodb_table",
                "configuration_table",
                {"name": "idp-config-x"},
                module=m,
            ),
            res("aws_dynamodb_table", "users", {"name": "gitf2-users"}, module=m),
            res(
                "aws_dynamodb_table",
                "concurrency_table",
                {"name": "idp-conc-x"},
                module=m,
            ),
            res("aws_kms_key", "encryption_key", {"key_id": "k-1"}),
            res("aws_cognito_user_pool", "user_pool", {"id": "us-west-2_P"}, module=m),
            res("aws_cloudwatch_log_group", "lg", {"name": "/aws/lambda/gitf2-a"}),
            res("aws_iam_role", "r", {"name": "gitf2-role"}),
            {"mode": "data", "type": "aws_region", "name": "x", "instances": [{}]},
        ]
    }


def _plan(state, override=None):
    resources = []
    for inst in tmt.state_instances(state):
        verdict = "adopt"
        if inst["name"] == "configuration_table":
            verdict = "adopt-after-fix"
        if inst["name"] in ("concurrency_table", "lg", "r"):
            verdict = "recreate"
        verdict = (override or {}).get(inst["name"], verdict)
        a = inst["attributes"]
        resources.append(
            {
                "old_address": inst["address"],
                "old_type": inst["type"],
                "kind": "configuration_table"
                if inst["name"] == "configuration_table"
                else inst["name"],
                "verdict": verdict,
                "physical_id": a.get("bucket") or a.get("name") or a.get("id"),
            }
        )
    return {"resources": resources, "configurationTable": "idp-config-x"}


def test_state_instances_flatten_with_module_and_index():
    addresses = [i["address"] for i in tmt.state_instances(_state())]
    assert "module.genai_idp_accelerator.aws_s3_bucket.test_sets[0]" in addresses
    assert "aws_s3_bucket.input_bucket" in addresses
    assert not any("aws_region" in a for a in addresses)


def test_physical_names_are_read_per_kind():
    names = tmt.physical_names(_state())
    assert "gitf2-input-x" in names["buckets"]
    assert "idp-tracking-x" in names["tables"]
    assert names["user_pools"] == ["us-west-2_P"]
    assert names["kms_keys"] == ["k-1"]
    assert names["log_groups"] == ["/aws/lambda/gitf2-a"]
    assert names["roles"] == ["gitf2-role"]


def test_a_plan_keeping_everything_required_passes():
    state = _state()
    assert tmt.required_kept_missing(state, _plan(state)) == []


def test_a_plan_recreating_a_required_resource_is_reported():
    state = _state()
    problems = tmt.required_kept_missing(state, _plan(state, {"users": "recreate"}))
    assert problems == [
        "module.genai_idp_accelerator.aws_dynamodb_table.users is not adopted "
        "(verdict 'recreate')"
    ]


def test_a_required_resource_missing_from_the_old_state_is_reported():
    state = _state()
    state["resources"] = [r for r in state["resources"] if r["name"] != "output_bucket"]
    problems = tmt.required_kept_missing(state, _plan(state))
    assert problems == ["the old state has no aws_s3_bucket.output_bucket"]


def test_kept_resources_lists_adopted_buckets_and_tables_only():
    state = _state()
    kept = tmt.kept_resources(_plan(state))
    assert "idp-conc-x" not in kept["tables"]
    assert "idp-config-x" in kept["tables"]
    assert "gitf2-ts" in kept["buckets"]


# -- snapshot comparison ------------------------------------------------------------


def test_objects_missing_or_changed_are_reported_and_additions_counted():
    before = {"a": {"etag": "1", "size": 1}, "b": {"etag": "2", "size": 2}}
    after = {
        "a": {"etag": "1", "size": 1},
        "b": {"etag": "X", "size": 2},
        "c": {"etag": "3", "size": 3},
    }
    assert tmt.compare_objects(before, after) == {
        "missing": [],
        "changed": ["b"],
        "added": 1,
        "before": 2,
    }
    assert tmt.compare_objects(before, {"a": before["a"]})["missing"] == ["b"]


def _config_row(**extra):
    row = {
        "Configuration": {"S": "Config#default"},
        "_compressed_config": {"B": "abc"},
        "LatestRevision": {"N": "1"},
        "PublishedRevision": {"N": "1"},
    }
    row.update(extra)
    return row


def test_fix_config_counter_removal_is_the_one_allowed_change():
    before = {"k": _config_row()}
    after = {
        "k": {k: v for k, v in _config_row().items() if k not in tmt.CONFIG_COUNTERS}
    }
    cmp = tmt.compare_items("configuration", before, after)
    assert cmp["changed"] == [] and cmp["allowedChanges"] == 1


def test_any_other_config_row_change_is_reported():
    before = {"k": _config_row()}
    after = {
        "k": {
            "Configuration": {"S": "Config#default"},
            "_compressed_config": {"B": "zzz"},
        }
    }
    cmp = tmt.compare_items("configuration", before, after)
    assert cmp["changed"][0]["attributes"] == [
        "LatestRevision",
        "PublishedRevision",
        "_compressed_config",
    ]


def test_counter_removal_outside_the_configuration_table_is_reported():
    before = {"k": _config_row()}
    after = {
        "k": {k: v for k, v in _config_row().items() if k not in tmt.CONFIG_COUNTERS}
    }
    assert tmt.compare_items("tracking_table", before, after)["changed"]


def test_counter_removal_on_a_non_config_row_is_reported():
    row = _config_row(Configuration={"S": "TerraformSeed#default"})
    after = {k: v for k, v in row.items() if k not in tmt.CONFIG_COUNTERS}
    assert tmt.compare_items("configuration", {"k": row}, {"k": after})["changed"]


def test_a_lost_item_is_reported():
    cmp = tmt.compare_items("users_table", {"k": {"PK": {"S": "1"}}}, {})
    assert cmp["missing"] == ["k"]


def test_table_kind_reads_the_plan():
    plan = _plan(_state())
    assert tmt.table_kind("idp-config-x", plan) == "configuration"
    assert tmt.table_kind("idp-tracking-x", plan) == "tracking_table"
    assert tmt.table_kind("nope", plan) == "unknown"


def test_profile_differences_compare_scope_not_formatting():
    before = {
        "email": "a@example.com",
        "persona": "Author",
        "allowedConfigVersions": ["default"],
        "createdAt": "x",
    }
    same = dict(before, createdAt="2026-01-01T00:00:00Z", status="active")
    assert tmt.profile_differences(before, same) == []
    widened = dict(before, allowedConfigVersions=None)
    assert tmt.profile_differences(before, widened) == [
        "allowedConfigVersions: ['default'] -> None"
    ]


# -- what the sweep may touch -------------------------------------------------------


@pytest.mark.parametrize(
    "name,owned",
    [
        ("gitf2-input-abc", True),
        ("/aws/lambda/gitf2-fn", True),
        ("gitfnew2-users", True),
        ("/gitfnew2/lambda/x", True),
        ("gitf20-input", False),
        ("mygitf2", False),
        ("tfidp2-users", False),
        ("IDP1-tracking", False),
        ("idp-tracking-table-x", False),
    ],
)
def test_owned_name_matches_a_delimited_token(name, owned):
    assert tmt.owned_name(name, "gitf2", "gitfnew2") is owned


def test_a_recorded_name_is_owned_whatever_it_is_called():
    assert tmt.owned_name(
        "idp-tracking-table-x", "gitf2", "gitfnew2", ["idp-tracking-table-x"]
    )


def test_merge_names_unions_per_kind():
    assert tmt.merge_names(
        {"buckets": ["a"]}, {"buckets": ["b", "a"], "tables": ["t"]}
    ) == {
        "buckets": ["a", "b"],
        "tables": ["t"],
    }


# -- the driver's control flow -------------------------------------------------------


class _Scripted(tmt.Run):
    """A Run whose phases are stubs: each records itself, one may fail."""

    def __init__(self, settings, fail=None, calls=None):
        super().__init__(settings)
        self.calls = calls if calls is not None else []
        self.fail = fail

    def __getattribute__(self, name):
        if name.startswith("phase_"):
            phase = name[len("phase_") :].replace("_", "-")
            calls, fail = (
                object.__getattribute__(self, "calls"),
                object.__getattribute__(self, "fail"),
            )

            def stub():
                calls.append(phase)
                if phase == fail:
                    raise tmt.PhaseFailed(f"{phase} broke")
                if phase == "preflight":
                    object.__setattr__(self, "preflight_passed", True)
                if phase == "upstream-deploy":
                    self.ctx["created"] = True
                return "ok"

            return stub
        return object.__getattribute__(self, name)

    def _new_state_nonempty(self):
        return False

    def _attempt(self, name, fn=None):
        try:
            self.run_phase(name)
        except Exception:  # noqa: BLE001
            pass


def _settings(tmp_path, **kw):
    base = dict(
        old_name="gitf2",
        new_name="gitfnew2",
        region="us-west-2",
        work_dir=tmp_path,
        terraform_bin="terraform",
        module_url="https://example.com/m.zip",
    )
    base.update(kw)
    return tmt.Settings(**base)


def test_a_clean_run_executes_every_phase_in_order(tmp_path):
    run = _Scripted(_settings(tmp_path))
    assert run.execute() == 0
    assert run.calls == list(tmt.PHASES)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "PASS"
    assert "PASS" in (tmp_path / "summary.txt").read_text()


def test_a_failed_phase_tears_down_new_first_then_old_then_sweeps(tmp_path):
    run = _Scripted(_settings(tmp_path), fail="verify")
    run.ctx["oldDeployStarted"] = True
    run.ctx["newApplyStarted"] = True
    run.ctx["created"] = True
    assert run.execute() == 1
    after = run.calls[run.calls.index("verify") + 1 :]
    assert after == ["new-destroy", "old-destroy-forced", "sweep", "leftover-check"]
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "FAIL"
    assert any("verify broke" in f for f in result["findings"])


def test_a_preflight_failure_runs_no_teardown_at_all(tmp_path):
    """A refusing preflight refuses exactly the cases where a sweep is
    catastrophic: overlapping names, a clash with existing resources, the wrong
    account. Measured on the earlier code: ['preflight', 'sweep', 'leftover-check']."""
    run = _Scripted(_settings(tmp_path), fail="preflight")
    assert run.execute() == 1
    assert run.calls == ["preflight"]


def test_a_failure_before_anything_was_created_runs_no_teardown(tmp_path):
    run = _Scripted(_settings(tmp_path), fail="upstream-clone")
    assert run.execute() == 1
    assert run.calls == ["preflight", "upstream-clone"]


def test_a_stored_created_marker_alone_does_not_permit_teardown(tmp_path):
    """A resumed run whose preflight fails (wrong account, say) tears nothing
    down, even though the work directory says it created something."""
    run = _Scripted(_settings(tmp_path), fail="preflight")
    run.ctx["created"] = True
    run.ctx["oldDeployStarted"] = True
    assert run.execute() == 1
    assert run.calls == ["preflight"]


def test_keep_skips_the_teardown(tmp_path):
    run = _Scripted(_settings(tmp_path, keep=True), fail="seed")
    run.ctx["oldDeployStarted"] = True
    assert run.execute() == 1
    assert run.calls[-1] == "seed"


def test_stop_after_stops_without_teardown_and_resume_continues(tmp_path):
    first = _Scripted(_settings(tmp_path, stop_after="seed"))
    assert first.execute() == 0
    assert first.calls[-1] == "seed"
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "STOPPED"
    second = _Scripted(_settings(tmp_path, resume=True))
    assert second.execute() == 0
    # preflight always reruns; nothing else that completed does.
    assert second.calls[0] == "preflight"
    assert second.calls[1] == tmt.PHASES[tmt.PHASES.index("seed") + 1]


def test_a_resumed_run_does_not_inherit_an_earlier_attempts_verdict(tmp_path):
    first = _Scripted(_settings(tmp_path, keep=True), fail="seed")
    assert first.execute() == 1
    second = _Scripted(_settings(tmp_path, resume=True))
    assert second.execute() == 0
    ctx = json.loads((tmp_path / "context.json").read_text())
    assert ctx["findings"] == [] and ctx["previousFindings"]


def test_the_result_never_holds_a_password(tmp_path):
    run = _Scripted(_settings(tmp_path))
    run.save_secrets(
        {"users": [{"username": "a@example.com", "password": "Sekret-Pw-1!"}]}
    )
    run.execute()
    assert "Sekret-Pw-1!" not in (tmp_path / "result.json").read_text()
    assert "Sekret-Pw-1!" not in (tmp_path / "context.json").read_text()
    assert oct((tmp_path / "secrets.json").stat().st_mode & 0o777) == "0o600"


def test_first_party_pythonpath_names_every_lib_root_absolutely():
    roots = tmt.first_party_pythonpath().split(":")
    assert all(r.startswith("/") for r in roots)
    names = {r.rsplit("/", 1)[1] for r in roots}
    assert {"idp_sdk", "idp_cli_pkg", "idp_common_pkg"} <= names


def test_api_helpers_unwrap_the_dispatcher_payload():
    assert tmt._find_value({"getMyProfile": {"email": "a"}}, None) == {"email": "a"}
    assert tmt._json_field(
        {"getConfigVersion": {"Custom": '{"notes": "x"}'}}, "Custom"
    ) == {"notes": "x"}
    assert tmt._success({"updateConfiguration": {"success": True}})
    assert not tmt._success({"updateConfiguration": {"success": False}})


def test_deployment_owned_config_rows_may_change():
    row = {"Configuration": {"S": "Schema"}, "Schema": {"S": "old"}}
    after = {"Configuration": {"S": "Schema"}, "Schema": {"S": "new"}}
    assert tmt.compare_items("configuration", {"k": row}, {"k": after})["changed"] == []


def test_a_restored_profile_row_may_change_and_no_other_profile_may():
    before = {"k": _config_row()}
    after = {"k": _config_row(_compressed_config={"B": "restored"})}
    assert (
        tmt.compare_items("configuration", before, after, ["default"])["changed"] == []
    )
    assert tmt.compare_items("configuration", before, after, ["bda"])["changed"]


def test_the_snapshot_waits_for_the_old_deployment_to_be_quiet(tmp_path, monkeypatch):
    run = tmt.Run(_settings(tmp_path))
    run.ctx["snapshotTargets"] = {"buckets": [], "tables": []}

    def snap(n):
        return {
            "buckets": {"b": {"k": {"etag": str(n), "size": 1}}},
            "tables": {},
            "users": {},
        }

    taken = iter([snap(1), snap(2), snap(2), snap(3)])
    monkeypatch.setattr(run, "snapshot", lambda label: next(taken))
    monkeypatch.setattr(tmt.time, "sleep", lambda s: None)
    assert "1 objects" in run.phase_snapshot_before()
    assert next(taken) == snap(3)  # stopped at the first two that agreed


def test_a_snapshot_that_never_settles_fails(tmp_path, monkeypatch):
    run = tmt.Run(_settings(tmp_path))
    run.ctx["snapshotTargets"] = {"buckets": [], "tables": []}
    counter = iter(range(100))
    monkeypatch.setattr(run, "snapshot", lambda label: {"n": next(counter)})
    monkeypatch.setattr(tmt.time, "sleep", lambda s: None)
    with pytest.raises(tmt.PhaseFailed, match="kept changing"):
        run.phase_snapshot_before()


def test_binary_attributes_compare_equal_after_a_round_trip_through_the_file():
    item = {"_compressed_config": {"B": b"\x1f\x8b binary"}}
    once = json.loads(json.dumps(item, default=tmt._json_default))
    assert json.loads(json.dumps(once)) == once
    assert once["_compressed_config"]["B"] != str(b"\x1f\x8b binary")


def test_a_resume_never_continues_past_a_failed_verify(tmp_path):
    ctx = {"phases": [{"phase": "verify", "status": "FAIL"}], "completed": ["verify"]}
    refusal = tmt.resume_refusal("migrate-detach", ctx)
    assert refusal is not None and "verify has not passed" in refusal
    # Tearing down is still allowed, and so is anything before the gate.
    assert tmt.resume_refusal("new-destroy", ctx) is None
    assert tmt.resume_refusal("migrate-apply", ctx) is None
    ctx["phases"][0]["status"] = "PASS"
    assert tmt.resume_refusal("migrate-detach", ctx) is None


def test_the_test_finds_a_kept_resource_in_a_destroy_plan_by_kind():
    targets = {"buckets": ["gitf2-x-api-test-sets"], "tables": ["idp-tracking-x"]}
    old = {"user_pool_id": "us-west-2_P"}

    def delete(tf_type, **before):
        return {
            "address": f"{tf_type}.x",
            "type": tf_type,
            "change": {"actions": ["delete"], "before": before},
        }

    plan = {
        "resource_changes": [
            # upstream's test-sets TABLE shares the kept bucket's name: not kept
            delete("aws_dynamodb_table", name="gitf2-x-api-test-sets"),
            delete("aws_cognito_user_pool_client", user_pool_id="us-west-2_P"),
        ]
    }
    assert tmt.kept_deletions(plan, targets, old) == []
    plan["resource_changes"] += [
        delete("aws_s3_bucket", bucket="gitf2-x-api-test-sets"),
        delete("aws_dynamodb_table", name="idp-tracking-x"),
        delete("aws_cognito_user_pool", id="us-west-2_P"),
    ]
    assert len(tmt.kept_deletions(plan, targets, old)) == 3


def test_the_ttl_disable_waits_out_dynamodbs_change_interval(tmp_path, monkeypatch):
    """Measured live: on a table upstream had created (and enabled TTL on) minutes
    before, the disable was refused as a second change within the interval."""
    from botocore.exceptions import ClientError

    class FakeDdb:
        def __init__(self):
            self.calls = 0

        def update_time_to_live(self, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ValidationException",
                            "Message": "Time to live has been modified multiple times within a fixed interval",
                        }
                    },
                    "UpdateTimeToLive",
                )

    FakeDdb.exceptions = type("exceptions", (), {"ClientError": ClientError})
    monkeypatch.setattr(tmt.time, "sleep", lambda s: None)
    ddb = FakeDdb()
    tmt.Run(_settings(tmp_path))._disable_ttl(ddb, "t", "expiresAfter")
    assert ddb.calls == 3


def test_any_other_ttl_error_is_raised(tmp_path, monkeypatch):
    from botocore.exceptions import ClientError

    class FakeDdb:
        def update_time_to_live(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "no"}},
                "UpdateTimeToLive",
            )

    FakeDdb.exceptions = type("exceptions", (), {"ClientError": ClientError})
    monkeypatch.setattr(tmt.time, "sleep", lambda s: None)
    with pytest.raises(ClientError):
        tmt.Run(_settings(tmp_path))._disable_ttl(FakeDdb(), "t", "expiresAfter")


class _Pages:
    def __init__(self, key, items):
        self.key, self.items = key, items

    def paginate(self, **kwargs):
        return [{self.key: self.items}]


class _FakeClients:
    """Just enough of each AWS client for a sweep that finds one owned bucket."""

    def __init__(self):
        self.deleted = []

    def __call__(self, service):
        return self

    # s3
    def list_buckets(self):
        return {"Buckets": [{"Name": "gitf2-input-x"}, {"Name": "someone-else"}]}

    def get_paginator(self, name):
        return {
            "list_tables": _Pages("TableNames", []),
            "describe_log_groups": _Pages("logGroups", []),
            "list_user_pools": _Pages("UserPools", []),
            "list_roles": _Pages("Roles", []),
            "list_object_versions": _Pages("Versions", []),
        }[name]

    def describe_log_groups(self, **kwargs):
        return {"logGroups": []}

    def delete_bucket(self, Bucket):
        self.deleted.append(Bucket)

    def describe_stacks(self, StackName):
        raise RuntimeError("no such stack")


def test_a_sweep_runs_and_deletes_only_what_the_run_owns(tmp_path, monkeypatch):
    run = tmt.Run(_settings(tmp_path))
    fake = _FakeClients()
    monkeypatch.setattr(run, "boto", fake)
    deleted, errors = tmt.Sweeper(run, {}).sweep()
    assert errors == []
    assert fake.deleted == ["gitf2-input-x"]
    assert deleted == ["bucket gitf2-input-x"]


def _run_row(**extra):
    row = {
        "PK": {"S": "doc#seed/a.pdf"},
        "SK": {"S": "run#1"},
        "RecordType": {"S": "run"},
    }
    row.update(extra)
    return row


def test_the_gsi_backfill_adding_its_attributes_is_allowed():
    """Measured live: the module's backfill added ItemType and ConfidenceAlertCount
    to upstream's run# rows after the adoption."""
    before = {"k": _run_row()}
    after = {
        "k": _run_row(ItemType={"S": "document"}, ConfidenceAlertCount={"N": "23"})
    }
    cmp = tmt.compare_items("tracking_table", before, after)
    assert cmp["changed"] == [] and cmp["allowedChanges"] == 1


def test_a_changed_or_removed_tracking_attribute_is_still_reported():
    before = {"k": _run_row(CompletionTime={"S": "01:45"})}
    changed = {"k": _run_row(CompletionTime={"S": "01:47"}, ItemType={"S": "document"})}
    assert tmt.compare_items("tracking_table", before, changed)["changed"]
    removed = {"k": _run_row()}
    assert tmt.compare_items("tracking_table", before, removed)["changed"]
    # An existing backfill attribute overwritten is a change, not an addition.
    before = {"k": _run_row(ConfidenceAlertCount={"N": "2"})}
    after = {"k": _run_row(ConfidenceAlertCount={"N": "0"})}
    assert tmt.compare_items("tracking_table", before, after)["changed"]


def test_an_unknown_attribute_added_is_reported():
    before = {"k": _run_row()}
    after = {"k": _run_row(Surprise={"S": "x"})}
    assert tmt.compare_items("tracking_table", before, after)["changed"]


def test_a_state_still_listing_resources_counts_as_leftovers_but_data_sources_do_not():
    listed = [
        "aws_lambda_function.x",
        "module.m.aws_iam_role.r",
        "data.aws_region.current",
        "module.m.data.aws_caller_identity.me",
    ]
    assert tmt.managed_addresses(listed) == [
        "aws_lambda_function.x",
        "module.m.aws_iam_role.r",
    ]


def test_the_accepted_changes_are_exactly_the_ones_the_guide_prescribes():
    """The ratchet on GUIDE_ACCEPTED_CHANGES (registered in
    scripts/tests/gate_exemptions.json): a stale entry the guide no longer names
    fails, and so does a change the guide tells every migration to accept that the
    test does not pass, which would leave the test unable to follow the guide."""
    import re
    from pathlib import Path

    guide = (Path(tmt.REPO_ROOT) / "docs" / "terraform-migration.md").read_text()
    prescribed = set(
        re.findall(r"--accept-change\s+`?\n?\s*([\w.\[\]\"-]+:[\w-]+)", guide)
    )
    assert set(tmt.GUIDE_ACCEPTED_CHANGES) == prescribed, (
        f"stale in the driver: {set(tmt.GUIDE_ACCEPTED_CHANGES) - prescribed}; "
        f"prescribed by the guide but not passed: {prescribed - set(tmt.GUIDE_ACCEPTED_CHANGES)}"
    )


# -- review fixes: names, ownership, targets, budget, files -------------------------


def test_a_resume_with_other_names_is_refused_before_anything_runs(tmp_path):
    first = _Scripted(_settings(tmp_path, stop_after="seed"))
    first.execute()
    second = _Scripted(
        _settings(tmp_path, resume=True, old_name="idp1", new_name="tfidp1")
    )
    assert second.execute() == 2
    assert second.calls == []
    ctx = json.loads((tmp_path / "context.json").read_text())
    assert (ctx["oldName"], ctx["newName"]) == ("gitf2", "gitfnew2")


def test_teardown_only_refuses_other_names_overlapping_names_and_another_account(
    tmp_path, monkeypatch
):
    run = _Scripted(_settings(tmp_path, stop_after="seed"))
    run.execute()

    class Sts:
        def __init__(self, account):
            self.account = account

        def get_caller_identity(self):
            return {"Account": self.account}

    for kw, account in (
        (dict(old_name="idp1", new_name="tfidp1"), "123456789012"),
        (dict(), "999999999999"),
    ):
        other = tmt.Run(_settings(tmp_path, resume=True, **kw))
        other.ctx["account"] = "123456789012"
        monkeypatch.setattr(other, "boto", lambda service, a=account: Sts(a))
        monkeypatch.setattr(
            other, "emergency_teardown", lambda: pytest.fail("tore down")
        )
        assert other.teardown_only() == 2


def test_names_overlapping_or_too_long_for_upstreams_iam_names_are_refused():
    assert tmt.validate_names("idp1", "tfidp1")  # overlap
    assert any(
        "longer than 13" in p
        for p in tmt.validate_names("idp-9911-235959", "idp-1111-235959")
    )
    assert tmt.validate_names("idpo11112359", "idp-1111-235959") == []


@pytest.mark.parametrize(
    "name",
    [
        "idp1-tracking",
        "tfidp1-users",
        "IDP1-x",
        "IDP1",
        "/aws/lambda/idp1-fn",
        "abc-gitf2-x",
        "gitf20-x",
        "mygitf2",
    ],
)
def test_another_deployments_resources_are_never_selected(name):
    assert tmt.owned_name(name, "gitf2", "gitfnew2") is False


@pytest.mark.parametrize(
    "name",
    [
        "gitf2-input-x",
        "/aws/lambda/gitf2-fn",
        "gitfnew2-users",
        "/gitfnew2/lambda/x",
        "gitfnew2",
    ],
)
def test_the_runs_own_prefixes_are_selected(name):
    assert tmt.owned_name(name, "gitf2", "gitfnew2") is True


def test_the_ci_pre_stack_and_its_role_are_never_swept(tmp_path):
    run = tmt.Run(
        _settings(tmp_path, new_name="idp-1111-235959", old_name="idpo11112359")
    )
    sweeper = tmt.Sweeper(run, {})
    assert not sweeper.owned("idp-1111-235959-iam-TFDeployRole", "roles")
    assert sweeper.owned("idp-1111-235959-SomeFunctionRole", "roles")


def test_preflight_inventories_exactly_what_the_sweep_would_delete(
    tmp_path, monkeypatch
):
    run = tmt.Run(_settings(tmp_path))
    seen = []
    monkeypatch.setattr(
        tmt.Sweeper, "inventory", lambda self: seen.append(self) or ["role gitf2-x"]
    )
    assert run.existing_matches() == ["role gitf2-x"]
    assert seen and seen[0].recorded == {}


def test_the_snapshot_covers_everything_the_plan_keeps(tmp_path, monkeypatch):
    run = tmt.Run(_settings(tmp_path))
    monkeypatch.setattr(
        run, "required_targets", lambda: {"buckets": ["b1"], "tables": ["t1"]}
    )
    plan_dir = run.new_dir / "genai-idp-terraform-migration"
    plan_dir.mkdir(parents=True)
    state = _state()
    (plan_dir / "migration-plan.json").write_text(json.dumps(_plan(state)))
    targets = run.snapshot_targets()
    assert "b1" in targets["buckets"] and "t1" in targets["tables"]
    # the agent-jobs-like extras the plan adopts are snapshotted too
    assert "gitf2-users" in targets["tables"] and "gitf2-ts" in targets["buckets"]


def test_the_deadline_stops_forward_phases_and_tears_down(tmp_path):
    run = _Scripted(_settings(tmp_path, deadline_seconds=0))
    assert run.execute() == 1
    # preflight is the first forward phase; it is refused, so nothing ran at all
    assert run.calls == []


def test_a_failing_terraform_command_reports_its_log_tail(tmp_path):
    log = tmp_path / "x.log"
    log.write_text(
        "".join(f"line {i}\n" for i in range(100))
        + "\x1b[31mError: AccessDenied\x1b[0m\n"
    )
    tail = tmt.log_tail(log)
    assert (
        tail.endswith("Error: AccessDenied")
        and "line 71" in tail
        and "line 70" not in tail
    )


def test_private_files_are_owner_only_even_when_they_existed(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("old")
    path.chmod(0o644)
    tmt.write_private(path, "new")
    assert oct(path.stat().st_mode & 0o777) == "0o600" and path.read_text() == "new"


def test_a_retried_run_reports_its_earlier_findings(tmp_path):
    first = _Scripted(_settings(tmp_path, keep=True), fail="seed")
    first.execute()
    second = _Scripted(_settings(tmp_path, resume=True))
    second.execute()
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["previousFindings"] and "seed broke" in result["previousFindings"][0]


def test_restored_profiles_are_parsed_from_the_commands_wording_or_refused():
    changes = [
        "restore revision 1 of default (its content before the migration), replacing "
        "the module preset the first deploy published"
    ]
    assert tmt.restored_profiles(changes) == ["default"]
    with pytest.raises(ValueError):
        tmt.restored_profiles(["put back default"])


def test_config_list_is_matched_by_whole_profile_name():
    listing = "│ default-copy │ active │\n│ bda │ │"
    assert not tmt.profile_listed(listing, "default")
    assert tmt.profile_listed(listing, "bda")
    assert tmt.profile_listed("│ default │", "default")


# -- upstream IAM under the run's path and boundary ------------------------------

PATH_ = "/idp-1111-235959-upstream/"
BOUNDARY = "arn:aws:iam::123456789012:policy/idp-1111-235959-PermissionsBoundary"


def _upstream_tree(root):
    files = {
        "main.tf": 'resource "aws_iam_role" "root_role" {\n  name = "x"\n}\n',
        "modules/api/iam.tf": (
            'resource "aws_iam_role" "resolver" {\n  name = "ConfigurationResolverRole"\n}\n'
            'resource "aws_iam_policy" "s3" {\n  name = "UploadResolverS3Policy"\n}\n'
            'resource "aws_iam_role_policy_attachment" "a" {}\n'
        ),
        "modules/api/tests/fixture.tf": 'resource "aws_iam_role" "fixture" {}\n',
        f"{tmt.UPSTREAM_EXAMPLE}/knowledge-base.tf": 'resource "aws_iam_role" "kb" {}\n',
        "examples/bda-processor/main.tf": 'resource "aws_iam_role" "other" {}\n',
        "sources/template.tf": 'resource "aws_iam_role" "accelerator" {}\n',
        "modules/api/.terraform/x.tf": 'resource "aws_iam_role" "cached" {}\n',
        "modules/api/mine_override.tf": 'resource "aws_iam_role" "resolver" {}\n',
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_every_loaded_iam_role_and_policy_gets_the_path_and_roles_the_boundary(
    tmp_path,
):
    _upstream_tree(tmp_path)
    files = tmt.upstream_iam_overrides(tmp_path, PATH_, BOUNDARY)
    by_dir = {p.parent.relative_to(tmp_path).as_posix(): t for p, t in files.items()}
    assert set(by_dir) == {".", "modules/api", tmt.UPSTREAM_EXAMPLE}
    assert all(p.name == tmt.IAM_OVERRIDE_FILE for p in files)
    api = by_dir["modules/api"]
    assert 'resource "aws_iam_role" "resolver" {' in api
    assert 'resource "aws_iam_policy" "s3" {' in api
    assert api.count(f'path = "{PATH_}"') == 2
    # a policy takes a path, never a boundary
    assert api.count("permissions_boundary") == 1
    assert f'permissions_boundary = "{BOUNDARY}"' in api
    assert "aws_iam_role_policy_attachment" not in api


def test_files_the_example_does_not_load_are_not_overridden(tmp_path):
    """Other examples, the accelerator submodule, test fixtures, provider caches
    and existing override files are not part of the deployed configuration; an
    override naming one of their blocks would fail `terraform init`."""
    _upstream_tree(tmp_path)
    text = "".join(tmt.upstream_iam_overrides(tmp_path, PATH_, BOUNDARY).values())
    for absent in ("fixture", "other", "accelerator", "cached"):
        assert f'"{absent}"' not in text
    assert text.count('"resolver"') == 1


@pytest.mark.parametrize("path", ["idp-x/", "/idp-x", "/idp x/", ""])
def test_a_malformed_iam_path_is_refused(tmp_path, path):
    with pytest.raises(ValueError):
        tmt.upstream_iam_overrides(tmp_path, path, BOUNDARY)


def _args(*extra):
    return [
        "--old-name",
        "idpo11112359",
        "--new-name",
        "idp-1111-235959",
        "--work-dir",
        "/tmp/w",
        "--terraform-bin",
        "/bin/sh",
        "--terraform-module-url",
        "https://m.zip",
        *extra,
    ]


@pytest.mark.parametrize(
    "extra",
    [
        ("--upstream-iam-path", PATH_),
        ("--upstream-permissions-boundary-arn", BOUNDARY),
        (
            "--upstream-iam-path",
            "no-slashes",
            "--upstream-permissions-boundary-arn",
            BOUNDARY,
        ),
    ],
    ids=["path-only", "boundary-only", "malformed"],
)
def test_the_path_and_boundary_are_given_together_and_well_formed(extra, capsys):
    with pytest.raises(SystemExit):
        tmt.parse_args(_args(*extra))
    err = capsys.readouterr().err
    assert "go together" in err or "is not an IAM path" in err, err


def test_both_reach_the_settings():
    s = tmt.parse_args(
        _args(
            "--upstream-iam-path",
            PATH_,
            "--upstream-permissions-boundary-arn",
            BOUNDARY,
        )
    )
    assert (s.upstream_iam_path, s.upstream_permissions_boundary_arn) == (
        PATH_,
        BOUNDARY,
    )


def test_a_resume_under_another_iam_path_is_refused():
    """The path is part of every upstream role's identity: resuming under another
    would plan all of them for replacement, so it is refused like a changed name."""
    ctx = {"oldName": "o", "newName": "n", "upstreamIamPath": PATH_}
    assert tmt.stored_name_problems(ctx, "o", "n", PATH_) == []
    assert tmt.stored_name_problems(ctx, "o", "n", "/idp-other-upstream/")
    assert tmt.stored_name_problems(ctx, "o", "n", None)
    # a context written before the path existed carries no claim about it
    assert tmt.stored_name_problems({"oldName": "o"}, "o", "n", PATH_) == []


# -- the state mirror ---------------------------------------------------------------

MIRROR = "s3://ci-bucket/terraform-migration-runs/gitfnew2/"
MIRROR_PREFIX = "terraform-migration-runs/gitfnew2/"


class _FakeS3:
    """Records puts and deletes; serves gets from `objects`."""

    def __init__(self, objects=None, fail_put=False):
        self.objects = dict(objects or {})
        self.fail_put = fail_put
        self.puts = {}
        self.deleted = []

    def put_object(self, Bucket, Key, Body, **kw):  # noqa: N803 - boto3 casing
        if self.fail_put:
            raise RuntimeError("AccessDenied: PutObject")
        assert kw.get("ServerSideEncryption") == "AES256"
        self.puts[Key] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise RuntimeError("NoSuchKey")
        body = self.objects[Key]

        class _B:
            def read(self):
                return body

        return {"Body": _B()}

    def get_paginator(self, api):
        fake = self

        class _P:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                keys = [k for k in fake.objects if k.startswith(Prefix)]
                if api == "list_object_versions":
                    yield {
                        "Versions": [{"Key": k, "VersionId": "v1"} for k in keys],
                        "DeleteMarkers": [],
                    }
                else:
                    yield {"Contents": [{"Key": k} for k in keys]}

        return _P()

    def delete_object(self, Bucket, Key, VersionId):  # noqa: N803
        self.deleted.append(Key)


class _FakeSts:
    def get_caller_identity(self):
        return {"Account": "123456789012"}


def _mirrored(tmp_path, s3, **kw):
    run = tmt.Run(_settings(tmp_path, state_mirror=MIRROR, **kw))
    run._boto_cache.update(s3=s3, sts=_FakeSts())
    return run


def test_both_roots_get_an_s3_backend_under_the_mirror_prefix(tmp_path, monkeypatch):
    run = _mirrored(tmp_path, _FakeS3())
    run.old_dir.mkdir(parents=True)
    run.prepare_old_root()
    old = (run.old_dir / tmt.OLD_BACKEND_FILE).read_text()
    assert f'key          = "{MIRROR_PREFIX}old/terraform.tfstate"' in old
    assert 'bucket       = "ci-bucket"' in old and "use_lockfile = true" in old
    assert "encrypt      = true" in old and 'region       = "us-west-2"' in old

    seen = {}

    class _Stop(Exception):
        pass

    def migrate(command, *args):
        backend = run.new_dir / tmt.NEW_BACKEND_FILE
        seen["backend"] = backend.read_text() if backend.is_file() else None
        raise _Stop()

    monkeypatch.setattr(run, "terraform_capture", lambda *a: "{}")
    monkeypatch.setattr(run, "secrets", lambda: {"users": [{"username": "a@b"}]})
    monkeypatch.setattr(run, "migrate", migrate)
    with pytest.raises(_Stop):
        run.phase_migrate_plan()
    # written before `migrate plan`, whose own `init` takes the backend
    assert f'"{MIRROR_PREFIX}new/terraform.tfstate"' in (seen["backend"] or "")


def test_without_a_mirror_no_backend_is_written(tmp_path):
    run = tmt.Run(_settings(tmp_path))
    run.old_dir.mkdir(parents=True)
    run.prepare_old_root()
    assert not (run.old_dir / tmt.OLD_BACKEND_FILE).exists()


def test_save_mirrors_the_context_and_new_root_but_never_secrets_or_state(tmp_path):
    s3 = _FakeS3()
    run = _mirrored(tmp_path, s3)
    run.new_dir.mkdir(parents=True)
    for name in (
        "main.tf",
        "terraform.tfvars",
        "backend.tf",
        "adopt.tf",
        ".terraform.lock.hcl",
        "terraform.tfstate",
        "terraform.tfstate.backup",
        "apply.tfplan",
    ):
        (run.new_dir / name).write_text("x")
    run.save_secrets({"users": [{"password": "hunter2"}]})
    run.ctx["oldName"] = "gitf2"
    run.save()
    keys = set(s3.puts)
    assert MIRROR_PREFIX + tmt.MIRROR_CONTEXT in keys
    assert {
        k[len(MIRROR_PREFIX + tmt.MIRROR_NEW_ROOT) :] for k in keys if "new-root/" in k
    } == {
        "main.tf",
        "terraform.tfvars",
        "backend.tf",
        "adopt.tf",
        ".terraform.lock.hcl",
    }
    assert not any("secret" in k or "tfstate" in k or "tfplan" in k for k in keys)
    assert all(b"hunter2" not in body for body in s3.puts.values())


def test_a_failed_mirror_write_only_warns_in_save(tmp_path, capsys):
    run = _mirrored(tmp_path, _FakeS3(fail_put=True))
    run.save()
    assert "could not update the state mirror" in capsys.readouterr().out
    assert run.context_path.is_file()


def test_preflight_fails_when_the_mirror_cannot_be_written(tmp_path, monkeypatch):
    """A run that could not be torn down by a later build creates nothing."""
    run = _mirrored(tmp_path, _FakeS3(fail_put=True))

    class _Iam:
        def get_account_summary(self):
            return {"SummaryMap": {"RolesQuota": 5000, "Roles": 10}}

    run._boto_cache["iam"] = _Iam()
    monkeypatch.setattr(run, "identity_problems", lambda: [])
    run._account = "123456789012"
    monkeypatch.setattr(
        run, "terraform_capture", lambda *a: '{"terraform_version": "1.17.1"}'
    )
    monkeypatch.setattr(run, "existing_matches", lambda: [])
    with pytest.raises(tmt.PhaseFailed, match="state mirror"):
        run.phase_preflight()
    assert run.preflight_passed is False
    assert not run.ctx.get("created")


def _mirrored_context(**kw):
    ctx = {
        "oldName": "gitf2",
        "newName": "gitfnew2",
        "runId": "20261111T000000Z",
        "account": "123456789012",
        "created": True,
        "oldDeployStarted": True,
        "completed": [],
        "phases": [],
        "findings": [],
    }
    ctx.update(kw)
    return json.dumps(ctx).encode()


def test_teardown_only_reads_the_context_from_the_mirror_and_refuses_other_names(
    tmp_path, monkeypatch
):
    s3 = _FakeS3(
        {MIRROR_PREFIX + tmt.MIRROR_CONTEXT: _mirrored_context(oldName="idp1")}
    )
    run = _mirrored(tmp_path, s3, resume=True, teardown_only=True)
    calls = []
    monkeypatch.setattr(run, "restore_for_teardown", lambda: calls.append("restore"))
    monkeypatch.setattr(run, "emergency_teardown", lambda: calls.append("teardown"))
    assert run.teardown_only() == 2
    assert calls == [] and s3.deleted == []
    assert run.ctx["oldName"] == "idp1"  # read from the mirror, then refused


def _teardown(tmp_path, monkeypatch, leftovers):
    s3 = _FakeS3(
        {
            MIRROR_PREFIX + tmt.MIRROR_CONTEXT: _mirrored_context(),
            MIRROR_PREFIX + "old/terraform.tfstate": b"{}",
        }
    )
    run = _mirrored(tmp_path, s3, resume=True, teardown_only=True)
    calls = []
    monkeypatch.setattr(
        run, "restore_for_teardown", lambda: calls.append("restore") or []
    )

    def teardown():
        calls.append("teardown")
        run.ctx["leftovers"] = leftovers

    monkeypatch.setattr(run, "emergency_teardown", teardown)
    rc = run.teardown_only()
    return rc, calls, s3


def test_teardown_only_restores_before_tearing_down_and_clears_a_clean_mirror(
    tmp_path, monkeypatch
):
    rc, calls, s3 = _teardown(tmp_path, monkeypatch, [])
    assert (rc, calls) == (0, ["restore", "teardown"])
    assert sorted(s3.deleted) == sorted(
        [MIRROR_PREFIX + tmt.MIRROR_CONTEXT, MIRROR_PREFIX + "old/terraform.tfstate"]
    )


def test_a_teardown_that_left_something_keeps_the_mirror(tmp_path, monkeypatch):
    rc, _, s3 = _teardown(tmp_path, monkeypatch, ["role x"])
    assert rc == 1 and s3.deleted == []


class _Mirrored(_Scripted):
    """A scripted run whose leftover check records `leftovers`."""

    leftovers: object = []

    def __getattribute__(self, name):
        if name == "phase_leftover_check":

            def stub():
                object.__getattribute__(self, "calls").append("leftover-check")
                self.ctx["leftovers"] = type(self).leftovers
                return "ok"

            return stub
        return super().__getattribute__(name)


@pytest.mark.parametrize(
    ("kw", "leftovers", "cleared"),
    [
        ({}, [], True),
        ({}, ["bucket b"], False),
        ({"keep": True}, [], False),
        ({"stop_after": "seed"}, [], False),
    ],
    ids=["clean", "leftovers", "keep", "stopped"],
)
def test_a_run_clears_its_mirror_only_after_a_clean_leftover_check(
    tmp_path, kw, leftovers, cleared
):
    s3 = _FakeS3({MIRROR_PREFIX + "new/terraform.tfstate": b"{}"})
    _Mirrored.leftovers = leftovers
    run = _Mirrored(_settings(tmp_path, state_mirror=MIRROR, **kw))
    run._boto_cache.update(s3=s3, sts=_FakeSts())
    run.execute()
    assert bool(s3.deleted) is cleared


def test_a_stale_clean_verdict_from_an_earlier_attempt_does_not_clear(tmp_path):
    """`leftovers` is this invocation's, not a resumed context's."""
    s3 = _FakeS3({MIRROR_PREFIX + "new/terraform.tfstate": b"{}"})
    _Mirrored.leftovers = []
    run = _Mirrored(
        _settings(tmp_path, state_mirror=MIRROR, stop_after="seed"), fail=None
    )
    run._boto_cache.update(s3=s3, sts=_FakeSts())
    run.ctx["leftovers"] = []
    run.execute()
    assert s3.deleted == []


@pytest.mark.parametrize(
    "url",
    ["s3://ci-bucket/prefix", "ci-bucket/prefix/", "s3://Bad_Bucket/p/", "s3://b/"],
)
def test_a_malformed_state_mirror_is_refused(url, capsys):
    with pytest.raises(SystemExit):
        tmt.parse_args(_args("--state-mirror", url))
    assert "is not s3://" in capsys.readouterr().err


def test_restore_rebuilds_the_old_root_and_fetches_the_new_one(tmp_path, monkeypatch):
    s3 = _FakeS3(
        {
            MIRROR_PREFIX + tmt.MIRROR_NEW_ROOT + "main.tf": b"module {}",
            MIRROR_PREFIX + tmt.MIRROR_NEW_ROOT + "backend.tf": b"terraform {}",
            MIRROR_PREFIX + tmt.MIRROR_NEW_ROOT + "terraform.tfstate": b"{}",
        }
    )
    run = _mirrored(tmp_path, s3)
    run.ctx.update(oldDeployStarted=True, runId="20261111T000000Z")
    order = []

    def clone():
        (run.work / "upstream" / ".git").mkdir(parents=True)
        run.old_dir.mkdir(parents=True)
        order.append("clone")
        return "cloned"

    monkeypatch.setattr(run, "phase_upstream_clone", clone)
    monkeypatch.setattr(
        run, "terraform", lambda d, *a, **k: order.append(f"init {d.name}")
    )
    assert run.restore_for_teardown() == ["old root", "new root"]
    assert order == [
        "clone",
        f"init {run.old_dir.name}",
        f"init {run.new_dir.name}",
    ]
    assert (run.old_dir / tmt.OLD_BACKEND_FILE).is_file()
    assert (run.old_dir / "terraform.tfvars").is_file()
    assert sorted(p.name for p in run.new_dir.iterdir()) == ["backend.tf", "main.tf"]


@pytest.mark.parametrize("fail", ["preflight", "upstream-clone"])
def test_a_run_refused_before_it_created_anything_clears_its_mirror(tmp_path, fail):
    """Nothing to tear down, and the run's role goes with the build's `-iam`
    cleanup, so a mirror left behind would only send every later build's reaper
    to assume a role that no longer exists."""
    s3 = _FakeS3({MIRROR_PREFIX + tmt.MIRROR_CONTEXT: b"{}"})
    run = _Mirrored(_settings(tmp_path, state_mirror=MIRROR), fail=fail)
    run._boto_cache.update(s3=s3, sts=_FakeSts())
    run.execute()
    assert not run.ctx.get("created")
    assert s3.deleted


def test_a_run_that_created_something_and_failed_keeps_its_mirror(tmp_path):
    s3 = _FakeS3({MIRROR_PREFIX + "old/terraform.tfstate": b"{}"})
    _Mirrored.leftovers = ["bucket b"]
    run = _Mirrored(_settings(tmp_path, state_mirror=MIRROR), fail="seed")
    run._boto_cache.update(s3=s3, sts=_FakeSts())
    run.execute()
    assert run.ctx.get("created") and s3.deleted == []


@pytest.mark.parametrize("boundary", [BOUNDARY, None])
def test_the_new_deployment_gets_the_runs_boundary(tmp_path, monkeypatch, boundary):
    """The deploy role creates only roles carrying the run's boundary, so the new
    deployment, whose module attaches PermissionsBoundaryArn to every role, is
    planned with it. A run given no boundary (by hand, as an administrator)
    passes none."""
    kw = (
        dict(upstream_iam_path=PATH_, upstream_permissions_boundary_arn=boundary)
        if boundary
        else {}
    )
    run = tmt.Run(_settings(tmp_path, **kw))
    seen = {}

    class _Stop(Exception):
        pass

    def migrate(command, *args):
        seen["args"] = list(args)
        raise _Stop()

    monkeypatch.setattr(run, "terraform_capture", lambda *a: "{}")
    monkeypatch.setattr(run, "secrets", lambda: {"users": [{"username": "a@b"}]})
    monkeypatch.setattr(run, "migrate", migrate)
    with pytest.raises(_Stop):
        run.phase_migrate_plan()
    args = seen["args"]
    if boundary:
        assert (
            args[args.index("--parameters") + 1] == f"PermissionsBoundaryArn={boundary}"
        )
    else:
        assert "--parameters" not in args
