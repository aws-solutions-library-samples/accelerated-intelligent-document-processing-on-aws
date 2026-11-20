# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``client.fit_check.run``: input refusal, the active-profile guard and cleanup.

The AWS-facing steps are replaced by stubs, so what is under test is the order of
the steps and the decisions between them: which files are refused, that a changed
active profile fails the run (after the report is written), and that cleanup never
deletes a profile the caller passed in.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from idp_sdk import IDPClient
from idp_sdk.exceptions import IDPError, IDPValidationError
from idp_sdk.operations.fit_check import FitCheckOperation

OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64


def _samples(tmp_path: Path) -> Path:
    d = tmp_path / "samples"
    (d / "sub").mkdir(parents=True)
    (d / "a.png").write_bytes(b"\x89PNG not decoded")
    (d / "sub" / "a.png").write_bytes(b"\x89PNG same name")
    (d / "deck.pptx").write_bytes(b"PK\x03\x04")
    (d / "renamed.docx").write_bytes(OLE2)
    (d / ".hidden.pdf").write_bytes(b"%PDF")
    return d


@pytest.mark.unit
def test_refusals_are_decided_before_upload(tmp_path):
    from idp_sdk._core.fit_check.inputs import scan_samples

    found = {
        s.file + ("@sub" if "/sub/" in s.path else ""): s
        for s in scan_samples(str(_samples(tmp_path)))
    }
    assert set(found) == {"a.png", "a.png@sub", "deck.pptx", "renamed.docx"}
    assert found["a.png"].refused is None and found["a.png"].pages == 1
    assert found["a.png@sub"].refused.startswith(
        "another sample has the same file name"
    )
    assert found["deck.pptx"].refused_ref == "limitation:unsupported-file-format"
    assert found["renamed.docx"].refused_ref == "limitation:legacy-office-formats"


def _operation(tmp_path, snapshots, *, config_profile=None):
    client = MagicMock(spec=IDPClient)
    client._require_stack.return_value = "S"
    client._region = "us-west-2"
    client._get_stack_resources.return_value = {
        "InputBucket": "in",
        "OutputBucket": "out",
    }
    client.config = MagicMock()
    client.config.download.return_value.config = {
        "classes": [{"$id": "Form", "properties": {"a": {}}}]
    }
    op = FitCheckOperation(client)
    op._stack_version = MagicMock(return_value="0.6.16")
    op._snapshot = MagicMock(side_effect=snapshots)
    names = {"default": True, "p1": False}
    op._profile_names = MagicMock(return_value=names)
    op._generate_profile = MagicMock(return_value="discovery")
    op._submit = MagicMock(return_value=["fit-check-x/a.png"])
    op._wait = MagicMock(return_value={})
    op._collect = MagicMock(return_value=[])
    op._cleanup = MagicMock(return_value={"summary": "cleaned"})
    return op


SAME = {"active_profile": "default", "active_sha256": "a", "default_sha256": "d"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "after",
    [
        dict(SAME, active_profile="p1"),
        dict(SAME, active_sha256="changed"),
        dict(SAME, default_sha256="changed"),
    ],
)
def test_a_changed_active_configuration_fails_the_run_after_the_report(tmp_path, after):
    op = _operation(tmp_path, [SAME, after])
    out = tmp_path / "out"
    with pytest.raises(IDPError, match="active configuration changed"):
        op.run(str(_samples(tmp_path)), "S", output_dir=str(out))
    record = json.loads((out / "fit-run-record.json").read_text())
    assert record["guard"]["unchanged"] is False
    assert "**CHANGED**" in (out / "fit-report.md").read_text()


@pytest.mark.unit
def test_an_unchanged_configuration_passes_and_is_reported(tmp_path):
    op = _operation(tmp_path, [SAME, dict(SAME)])
    result = op.run(str(_samples(tmp_path)), "S", output_dir=str(tmp_path / "out"))
    assert result.active_profile_unchanged is True
    assert result.guard["before"] == result.guard["after"] == SAME
    assert op._generate_profile.call_args.args[1].startswith("fit-check-")


@pytest.mark.unit
def test_the_guard_runs_when_processing_fails(tmp_path):
    op = _operation(tmp_path, [SAME, dict(SAME)])
    op._wait.side_effect = RuntimeError("stack went away")
    with pytest.raises(RuntimeError):
        op.run(
            str(_samples(tmp_path)), "S", output_dir=str(tmp_path / "out"), cleanup=True
        )
    assert op._snapshot.call_count == 2
    assert op._cleanup.called  # the uploaded batch is still removed


@pytest.mark.unit
def test_cleanup_never_deletes_a_profile_the_caller_passed(tmp_path):
    op = _operation(tmp_path, [SAME, dict(SAME)])
    op.run(
        str(_samples(tmp_path)),
        "S",
        config_profile="p1",
        cleanup=True,
        output_dir=str(tmp_path / "o"),
    )
    assert op._cleanup.call_args.args[4] is None
    op._generate_profile.assert_not_called()


@pytest.mark.unit
def test_cleanup_deletes_the_generated_profile(tmp_path):
    op = _operation(tmp_path, [SAME, dict(SAME)])
    op._profile_names.side_effect = lambda name: {"default": True}
    # The generated profile exists by the time cleanup runs.
    calls = {"n": 0}

    def names(_):
        calls["n"] += 1
        return (
            {"default": True}
            if calls["n"] == 1
            else {"default": True, op._generate_profile.call_args.args[1]: False}
        )

    op._profile_names.side_effect = names
    op.run(str(_samples(tmp_path)), "S", cleanup=True, output_dir=str(tmp_path / "o"))
    assert op._cleanup.call_args.args[4].startswith("fit-check-")


@pytest.mark.unit
def test_cleanup_is_off_by_default(tmp_path):
    op = _operation(tmp_path, [SAME, dict(SAME)])
    op.run(str(_samples(tmp_path)), "S", output_dir=str(tmp_path / "o"))
    op._cleanup.assert_not_called()


@pytest.mark.unit
def test_too_many_documents_is_refused_before_anything_runs(tmp_path):
    op = _operation(tmp_path, [SAME, dict(SAME)])
    with pytest.raises(IDPValidationError, match="more than --max-documents"):
        op.run(str(_samples(tmp_path)), "S", max_documents=0)
    op._snapshot.assert_not_called()
    op._submit.assert_not_called()


@pytest.mark.unit
def test_an_unknown_config_profile_is_refused(tmp_path):
    op = _operation(tmp_path, [SAME, dict(SAME)])
    with pytest.raises(IDPValidationError, match="not found"):
        op.run(
            str(_samples(tmp_path)),
            "S",
            config_profile="nope",
            output_dir=str(tmp_path / "o"),
        )
    op._submit.assert_not_called()
    assert op._snapshot.call_count == 2  # checked again on the way out


@pytest.mark.unit
def test_a_failed_cleanup_still_checks_the_guard_and_writes_the_report(tmp_path):
    from idp_sdk.exceptions import IDPProcessingError

    op = _operation(tmp_path, [SAME, dict(SAME)])
    op._cleanup.side_effect = RuntimeError("scan hit an unreadable record")
    out = tmp_path / "out"
    with pytest.raises(IDPProcessingError, match="cleanup did not finish"):
        op.run(str(_samples(tmp_path)), "S", cleanup=True, output_dir=str(out))
    assert op._snapshot.call_count == 2
    record = json.loads((out / "fit-run-record.json").read_text())
    assert record["guard"]["unchanged"] is True
    assert "Cleanup failed" in record["cleanup"]["summary"]


@pytest.mark.unit
def test_cleanup_deletes_each_uploaded_key_and_reads_back(tmp_path, monkeypatch):
    client = MagicMock(spec=IDPClient)
    client._region = "us-west-2"
    client.document = MagicMock()
    client.document.delete.return_value.success = True
    client.evaluation = MagicMock()
    client.config = MagicMock()
    client.config.delete.return_value.success = True
    op = FitCheckOperation(client)
    op._profile_names = MagicMock(return_value={"default": True})
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"KeyCount": 0}
    monkeypatch.setattr("boto3.client", lambda *a, **k: s3)
    keys = ["fit-check-x/a.pdf", "fit-check-x/b.png"]
    summary = op._cleanup(
        "S",
        {"InputBucket": "in", "OutputBucket": "out"},
        keys,
        [],
        "fit-check-x",
        print,
    )
    assert [c.args[0] for c in client.document.delete.call_args_list] == keys
    client.config.delete.assert_called_once_with(
        stack_name="S", config_profile="fit-check-x"
    )
    assert summary["documents_deleted"] == 2
    assert summary["objects_remaining"] == {"InputBucket": 0, "OutputBucket": 0}
    assert summary["profile_remaining"] is False and "error" not in summary
    # Anything left behind is an error, not a silent success.
    s3.list_objects_v2.return_value = {"KeyCount": 3}
    assert (
        "remain"
        in op._cleanup("S", {"InputBucket": "in"}, keys, [], None, print)["error"]
    )
