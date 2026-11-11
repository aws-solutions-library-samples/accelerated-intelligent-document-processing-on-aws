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
    row = _config_row(Configuration={"S": "Schema"})
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
    assert run.execute() == 1
    after = run.calls[run.calls.index("verify") + 1 :]
    assert after == ["new-destroy", "old-destroy-forced", "sweep", "leftover-check"]
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "FAIL"
    assert any("verify broke" in f for f in result["findings"])


def test_a_failure_before_anything_is_deployed_only_sweeps(tmp_path):
    run = _Scripted(_settings(tmp_path), fail="upstream-clone")
    assert run.execute() == 1
    assert run.calls[run.calls.index("upstream-clone") + 1 :] == [
        "sweep",
        "leftover-check",
    ]


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
