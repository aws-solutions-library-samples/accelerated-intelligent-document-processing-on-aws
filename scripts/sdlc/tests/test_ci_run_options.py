# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ci_run_options.json: the one GitLab fact CodeBuild needs and cannot see.

integration_test_deployment.py writes which pipeline source and ref started the
run at the root of the deployment zip, and codebuild_deployment.py updates the
long-lived UX review stack in place only when that says schedule plus develop.
What is pinned here is the writer's side: the documented shape, that a stale copy
from a previous job on a reused workspace can never be what ships, that the write
happens before the zip is built, and that the zip's excludes let the file through.
A correctness guard rather than a security control, as the module says: an
MR-button run executes the MR's own harness.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _in_tmp_cwd(tmp_path, monkeypatch):
    """Run in a scratch directory, because the file is written relative to it."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def itd():
    import integration_test_deployment as module

    return module


def _run_main(itd, monkeypatch, *, seen=None) -> object:
    def package():
        if seen is not None:
            seen.append(("package", Path(itd.CI_RUN_OPTIONS_FILE).exists()))

    monkeypatch.setattr(itd, "create_deployment_package", package)
    monkeypatch.setattr(itd, "upload_to_s3", lambda bucket: "version-1")
    monkeypatch.setattr(itd, "monitor_pipeline", lambda *a, **k: True)
    with pytest.raises(SystemExit) as excinfo:
        itd.main()
    return excinfo.value.code


@pytest.mark.unit
def test_the_options_are_written_in_the_documented_shape(itd):
    environ = {
        "CI_PIPELINE_SOURCE": "schedule",
        "CI_COMMIT_REF_NAME": "develop",
        "CI_PIPELINE_ID": "123456",
        "CI_JOB_ID": "991",
        "GITLAB_USER_EMAIL": "jdoe@example.invalid",
    }

    returned = itd.write_ci_run_options(environ=environ)

    text = Path("ci_run_options.json").read_text(encoding="utf-8")
    assert text == (
        "{\n"
        '  "version": 1,\n'
        '  "pipeline_source": "schedule",\n'
        '  "ref": "develop",\n'
        '  "pipeline_id": "123456",\n'
        '  "variant_probes": false\n'
        "}\n"
    )
    assert json.loads(text) == returned
    assert "991" not in text and "jdoe" not in text


@pytest.mark.unit
def test_a_variable_gitlab_did_not_set_is_written_empty(itd):
    returned = itd.write_ci_run_options("elsewhere.json", environ={})

    assert returned == {
        "version": 1,
        "pipeline_source": "",
        "ref": "",
        "pipeline_id": "",
        "variant_probes": False,
    }
    assert json.loads(Path("elsewhere.json").read_text(encoding="utf-8")) == returned


@pytest.mark.unit
def test_the_options_come_from_the_job_environment_by_default(itd, monkeypatch):
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "merge_request_event")
    monkeypatch.setenv("CI_COMMIT_REF_NAME", "feature/rotate")
    monkeypatch.setenv("CI_PIPELINE_ID", "777")

    itd.write_ci_run_options()

    written = json.loads(Path(itd.CI_RUN_OPTIONS_FILE).read_text(encoding="utf-8"))
    assert (written["pipeline_source"], written["ref"], written["pipeline_id"]) == (
        "merge_request_event",
        "feature/rotate",
        "777",
    )


@pytest.mark.unit
def test_a_write_that_fails_does_not_stop_the_run(itd, capsys):
    """The update is advisory, so its options must never cost the integration run."""
    returned = itd.write_ci_run_options(
        "no-such-directory/ci_run_options.json", environ={}
    )

    assert returned["version"] == 1
    assert not Path("no-such-directory").exists()
    assert "Could not write" in capsys.readouterr().out


@pytest.mark.unit
def test_main_writes_this_runs_options_and_never_ships_a_stale_copy(itd, monkeypatch):
    """A runner can reuse a workspace, so the last job's file may still be there.

    It is removed before anything else runs, so the copy that ships is always this
    run's, or none at all if this run's could not be written.
    """
    Path(itd.CI_RUN_OPTIONS_FILE).write_text(
        '{"version": 1, "pipeline_source": "schedule", "ref": "develop"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "merge_request_event")
    monkeypatch.setenv("CI_COMMIT_REF_NAME", "feature/rotate")
    monkeypatch.setenv("CI_PIPELINE_ID", "888")
    seen: list[tuple[str, bool]] = []
    real_write = itd.write_ci_run_options

    def write(*args, **kwargs):
        seen.append(("write", Path(itd.CI_RUN_OPTIONS_FILE).exists()))
        return real_write(*args, **kwargs)

    monkeypatch.setattr(itd, "write_ci_run_options", write)

    assert _run_main(itd, monkeypatch, seen=seen) == 0

    assert seen == [("write", False), ("package", True)]
    written = json.loads(Path(itd.CI_RUN_OPTIONS_FILE).read_text(encoding="utf-8"))
    assert written["pipeline_source"] == "merge_request_event"
    assert written["pipeline_id"] == "888"


@pytest.mark.unit
def test_a_stale_copy_is_gone_even_when_this_runs_write_fails(itd, monkeypatch):
    Path(itd.CI_RUN_OPTIONS_FILE).write_text("left over\n", encoding="utf-8")
    monkeypatch.setattr(itd, "write_ci_run_options", lambda *a, **k: {})

    assert _run_main(itd, monkeypatch) == 0

    assert not Path(itd.CI_RUN_OPTIONS_FILE).exists()


def _zip_excludes(itd, monkeypatch) -> list[str]:
    commands: list[str] = []
    monkeypatch.setattr(itd, "run_command", lambda cmd, **k: commands.append(cmd))
    itd.create_deployment_package()
    assert commands, "create_deployment_package ran no command"
    assert commands[0].startswith("zip -r ./dist/code.zip ./ ")
    return re.findall(r'-x "([^"]*)"', commands[0])


@pytest.mark.unit
def test_no_zip_exclude_keeps_the_options_file_out_of_the_package(itd, monkeypatch):
    """CodeBuild unpacks the zip at its working directory, where the reader looks."""
    excludes = _zip_excludes(itd, monkeypatch)

    assert excludes
    assert any(
        fnmatch.fnmatch(f"./{itd.AFTER_SCRIPT_CREDS_FILE}", pattern)
        for pattern in excludes
    ), "the pattern match below would be vacuous: it finds no known exclude either"
    for name in (f"./{itd.CI_RUN_OPTIONS_FILE}", itd.CI_RUN_OPTIONS_FILE):
        matched = [p for p in excludes if fnmatch.fnmatch(name, p)]
        assert matched == [], f"{name} is dropped from the zip by {matched}"
