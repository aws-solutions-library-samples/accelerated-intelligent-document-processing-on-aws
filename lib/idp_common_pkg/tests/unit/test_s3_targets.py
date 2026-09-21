# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The shared rule for which S3 bucket and key an API resolver may act on.

Two controls with different jobs, and the tests are split the same way: the bucket
allow-list bounds *which* bucket, and applies to reads and writes alike; the
write-once key rule bounds *where in* a bucket a write may land, and applies to writes
only — reading a configuration revision body or a run manifest is a legitimate thing
for the UI to do.
"""

import pytest

from idp_common import s3_targets

pytestmark = pytest.mark.unit

WIRED = {
    "INPUT_BUCKET": "stack-input",
    "OUTPUT_BUCKET": "stack-output",
    "CONFIGURATION_BUCKET": "stack-config",
}


class TestResolvingTheAllowList:
    def test_only_wired_names_contribute(self):
        assert s3_targets.resolve_allowed_buckets(WIRED) == {
            "stack-input",
            "stack-output",
            "stack-config",
        }

    def test_an_empty_value_is_not_a_wired_bucket(self):
        """Otherwise the empty string joins the allow-list and matches a caller who
        supplies no bucket at all."""
        allowed = s3_targets.resolve_allowed_buckets({**WIRED, "TEST_SET_BUCKET": ""})

        assert "" not in allowed
        assert len(allowed) == 3

    def test_nothing_wired_gives_an_empty_set(self):
        assert s3_targets.resolve_allowed_buckets({}) == set()


class TestTheBucketAllowList:
    def test_a_wired_bucket_is_permitted(self):
        s3_targets.assert_bucket_allowed(
            "stack-input", s3_targets.resolve_allowed_buckets(WIRED)
        )

    def test_a_foreign_bucket_is_refused(self):
        with pytest.raises(PermissionError) as excinfo:
            s3_targets.assert_bucket_allowed(
                "someone-elses-bucket", s3_targets.resolve_allowed_buckets(WIRED)
            )

        # The dispatcher picks 403 from the exception CLASS NAME, falling back to an
        # anchored message prefix. Both have to hold or the refusal becomes a 500.
        assert type(excinfo.value).__name__ == "PermissionError"
        assert str(excinfo.value).startswith("Unauthorized")

    def test_the_refusal_discloses_neither_the_bucket_nor_the_allow_list(self):
        with pytest.raises(PermissionError) as excinfo:
            s3_targets.assert_bucket_allowed(
                "someone-elses-bucket", s3_targets.resolve_allowed_buckets(WIRED)
            )

        message = str(excinfo.value)
        assert "someone-elses-bucket" not in message
        for name in WIRED.values():
            assert name not in message

    def test_an_empty_allow_list_refuses_everything(self):
        """Fails CLOSED. The resolver code and the variables that configure it are one
        CloudFormation resource, so an empty set is a template fault — and reading a
        template fault as "allow every bucket this role can reach" is the one reading
        that turns the fault into the gadget the list exists to prevent."""
        with pytest.raises(PermissionError, match="not configured"):
            s3_targets.assert_bucket_allowed("stack-input", set())


class TestTheWriteOnceKeyRule:
    @pytest.mark.parametrize(
        "key",
        [
            "config_revisions/default/000001.json.gz",
            "config_revisions/Production/000412.json.gz",
            "mydoc.pdf/runs/20260101T000000Z-abc/manifest.json",
            "some/nested/doc.pdf/runs/20260101T000000Z-abc/manifest.json",
            "runs/20260101T000000Z-abc/manifest.json",
        ],
    )
    def test_a_write_once_key_is_refused(self, key):
        with pytest.raises(PermissionError) as excinfo:
            s3_targets.assert_key_writable(key)

        assert str(excinfo.value).startswith("Unauthorized")

    @pytest.mark.parametrize(
        "key",
        [
            "invoice.pdf",
            "lending/statement.pdf",
            "document/20260101_000000_scan.pdf",
            # Near-misses, so the patterns are not looser than intended.
            "my_config_revisions_notes.txt",
            "runs/manifest.json",
            "mydoc.pdf/runs/20260101T000000Z-abc/output.json",
            "config_revisions_backup/default/1.json.gz",
        ],
    )
    def test_an_ordinary_key_is_permitted(self, key):
        s3_targets.assert_key_writable(key)

    def test_every_rule_names_what_it_protects(self):
        """A pattern with no stated store is one nobody can check or retire."""
        for pattern, what in s3_targets.WRITE_ONCE_KEY_RULES:
            assert what and isinstance(what, str)
            assert pattern.pattern

    def test_the_reason_identifies_which_store(self):
        revision = s3_targets.write_once_reason(
            "config_revisions/default/000001.json.gz"
        )
        manifest = s3_targets.write_once_reason("d.pdf/runs/r/manifest.json")

        assert revision is not None and "revision" in revision
        assert manifest is not None and "manifest" in manifest
        assert s3_targets.write_once_reason("invoice.pdf") is None


class TestTheCombinedWriteCheck:
    def test_both_controls_are_applied(self):
        allowed = s3_targets.resolve_allowed_buckets(WIRED)

        # Wrong bucket, ordinary key.
        with pytest.raises(PermissionError):
            s3_targets.assert_write_target_allowed("elsewhere", "a.pdf", allowed)

        # Right bucket, write-once key.
        with pytest.raises(PermissionError):
            s3_targets.assert_write_target_allowed(
                "stack-config", "config_revisions/default/000001.json.gz", allowed
            )

    def test_a_legitimate_write_is_permitted(self):
        """The control that matters most: this is a live path the UI uses, so a rule
        that over-refuses breaks document upload."""
        s3_targets.assert_write_target_allowed(
            "stack-input",
            "lending/invoice.pdf",
            s3_targets.resolve_allowed_buckets(WIRED),
        )

    def test_the_bucket_is_checked_before_the_key(self):
        """A caller naming a foreign bucket learns nothing about our key layout."""
        with pytest.raises(PermissionError) as excinfo:
            s3_targets.assert_write_target_allowed(
                "elsewhere", "config_revisions/default/000001.json.gz", {"stack-input"}
            )

        assert "bucket" in str(excinfo.value)


class TestTheReadPathDoesNotGetTheKeyRule:
    def test_reading_a_write_once_object_is_not_refused_by_the_bucket_check(self):
        """The configuration UI reads revision bodies and the version viewer reads run
        manifests. Only writes consult the key rule."""
        s3_targets.assert_bucket_allowed(
            "stack-config", s3_targets.resolve_allowed_buckets(WIRED)
        )
