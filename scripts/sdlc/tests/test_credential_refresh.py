"""Unit tests for _CredentialRefresher — the monitor's mid-run cred refresh.

The GitLab monitor's creds are a role-chained assume of idp-sdlc-GitLab, which
AWS hard-caps at 1h. To watch a >1h pipeline, the monitor re-assumes the role
(a fresh 1h session) before expiry and rebuilds its boto3 clients. These tests
mock STS/boto3 so no AWS is touched, and pin: role-ARN resolution from the
account id, a successful refresh swaps in the new session creds, build_clients
uses the current creds, and a refresh failure is swallowed (→ graceful handoff
fallback) rather than raising.
"""

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _in_tmp_cwd(tmp_path, monkeypatch):
    """Run every test in a scratch directory.

    refresh() now writes AFTER_SCRIPT_CREDS_FILE, and main()'s handoff path
    writes MONITOR_HANDOFF_MARKER, both relative to the CWD. Without this the
    suite would litter the repository root with files named after live
    credentials whenever it is run from there.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def itd():
    import integration_test_deployment as module

    return module


class _FakeSTS:
    def __init__(self, account="020432867916", fail=False):
        self._account = account
        self._fail = fail
        self.assume_calls = []

    def get_caller_identity(self):
        return {"Account": self._account}

    def assume_role(self, **kwargs):
        self.assume_calls.append(kwargs)
        if self._fail:
            raise RuntimeError("AccessDenied: not authorized to assume")
        return {
            "Credentials": {
                "AccessKeyId": "AKIA-NEW",
                "SecretAccessKey": "secret-new",
                "SessionToken": "token-new",
            }
        }


def _patch_boto3(itd, monkeypatch, sts, client_recorder=None):
    def fake_client(name, *a, **k):
        if name == "sts":
            return sts
        if client_recorder is not None:
            client_recorder.append((name, k))
        return object()

    monkeypatch.setattr(itd.boto3, "client", fake_client)


def test_resolves_role_arn_from_account(itd, monkeypatch):
    _patch_boto3(itd, monkeypatch, _FakeSTS(account="111122223333"))
    r = itd._CredentialRefresher("us-east-1")
    assert r._role_arn == "arn:aws:iam::111122223333:role/idp-sdlc-GitLab"


def test_refresh_success_swaps_in_new_creds(itd, monkeypatch):
    sts = _FakeSTS()
    _patch_boto3(itd, monkeypatch, sts)
    r = itd._CredentialRefresher("us-east-1")
    assert r.refresh() is True
    # subsequent STS/client construction uses the new session creds
    assert r._session_kwargs["aws_access_key_id"] == "AKIA-NEW"
    assert r._session_kwargs["aws_session_token"] == "token-new"
    # requested a chained-max 1h session
    assert sts.assume_calls[-1]["DurationSeconds"] == 3600
    assert sts.assume_calls[-1]["RoleArn"].endswith(":role/idp-sdlc-GitLab")


def test_build_clients_uses_current_creds(itd, monkeypatch):
    recorder = []
    sts = _FakeSTS()
    _patch_boto3(itd, monkeypatch, sts, client_recorder=recorder)
    r = itd._CredentialRefresher("us-west-2")
    r.refresh()
    cp, logs = r.build_clients()
    # both clients built with region + the refreshed creds
    names = [n for n, _ in recorder]
    assert "codepipeline" in names and "logs" in names
    for _, kw in recorder:
        assert kw.get("region_name") == "us-west-2"
        assert kw.get("aws_access_key_id") == "AKIA-NEW"


def test_refresh_failure_is_swallowed(itd, monkeypatch):
    sts = _FakeSTS(fail=True)
    _patch_boto3(itd, monkeypatch, sts)
    r = itd._CredentialRefresher("us-east-1")
    # must return False (→ caller keeps old clients, hands off later), not raise
    assert r.refresh() is False
    # creds unchanged (still ambient)
    assert r._session_kwargs == {}


def test_refresh_noop_when_role_arn_unresolved(itd, monkeypatch):
    # If get_caller_identity fails at construction, role_arn is None and
    # refresh() is a safe no-op returning False.
    class _BoomSTS:
        def get_caller_identity(self):
            raise RuntimeError("no creds")

    monkeypatch.setattr(itd.boto3, "client", lambda name, *a, **k: _BoomSTS())
    r = itd._CredentialRefresher("us-east-1")
    assert r._role_arn is None
    assert r.refresh() is False


def test_handoff_constants_allow_watching_past_one_hour(itd):
    # The whole point: handoff deadline exceeds the 1h chained-cred cap, and we
    # refresh before that cap.
    assert itd.MONITOR_HANDOFF_SECONDS > 3600
    assert itd.CREDENTIAL_REFRESH_SECONDS < 3600


class _ExpiredTokenException(Exception):
    """Mimic botocore's ExpiredTokenException by name (that's what we match)."""


def _drive_monitor(itd, monkeypatch, verdict):
    """Run monitor_pipeline_execution with a codepipeline that raises
    ExpiredTokenException on every poll, and a stubbed S3 verdict."""
    # A refresher whose refresh() always fails (the real-world bug: TagSession
    # denied) and whose clients raise expired-token on every call.
    class _ExpiredCP:
        def get_pipeline_execution(self, **k):
            raise _ExpiredTokenException("The security token ... is expired")

    class _FakeRefresher:
        def __init__(self, region):
            pass

        def refresh(self):
            return False

        def build_clients(self):
            return _ExpiredCP(), object()

    monkeypatch.setattr(itd, "_CredentialRefresher", _FakeRefresher)
    monkeypatch.setattr(itd, "resolve_codebuild_log_stream", lambda *a, **k: "stream-x")
    monkeypatch.setattr(itd, "fetch_summary_verdict", lambda *a, **k: verdict)
    monkeypatch.setattr(itd.time, "sleep", lambda *_a, **_k: None)
    # Force the handoff/refresh windows past so we exercise the expired branch
    # immediately on the first poll.
    return itd.monitor_pipeline_execution(
        "pipe", "exec-1", max_wait=120, handoff_after=999999
    )


def test_expired_creds_hand_off_neutral_when_no_fail_verdict(itd, monkeypatch):
    # A HEALTHY run whose creds expire mid-flight must NOT be failed: with no
    # OVERALL: FAIL in the summary, the monitor exits neutral (None), not False.
    assert _drive_monitor(itd, monkeypatch, verdict=None) is None
    assert _drive_monitor(itd, monkeypatch, verdict=True) is None


def test_expired_creds_still_fail_on_overall_fail_verdict(itd, monkeypatch):
    # But if the authoritative S3 summary already says OVERALL: FAIL, the
    # expired-creds path must still fail the job.
    assert _drive_monitor(itd, monkeypatch, verdict=False) is False


# ---------------------------------------------------------------------------
# Persisting a session for after_script.
#
# after_script's shell `aws` calls run on the JOB's credentials, which the
# runner's vendor mints pre-job and AWS caps at 1h for a chained assume. This
# job runs 58-63 minutes, so whether the log capture had working credentials
# was decided by how long the deploy took: the 2026-10-11 nightly (job
# 30872567, pipeline terminal at 59m, 62.6m wall clock) burned all 12
# CodeBuild-id attempts against an expired token and reported "no logs were
# fetched" for a run that passed 12/12, while the three nights before it
# (58.4-58.5m) resolved on the first attempt. after_script cannot re-assume the
# role itself — that needs live credentials — so the monitor hands its own
# freshly-minted session over through a file.
# ---------------------------------------------------------------------------


def _creds_file(itd):
    import pathlib

    return pathlib.Path(itd.AFTER_SCRIPT_CREDS_FILE)


def test_refresh_persists_the_session_for_after_script(itd, monkeypatch):
    _patch_boto3(itd, monkeypatch, _FakeSTS())
    r = itd._CredentialRefresher("us-east-1")
    assert r.refresh() is True

    path = _creds_file(itd)
    assert path.is_file(), "after_script has nothing to source"
    body = path.read_text()
    # Sourceable by POSIX sh: `. ./aws_after_script_creds.env`.
    assert "export AWS_ACCESS_KEY_ID=AKIA-NEW\n" in body
    assert "export AWS_SECRET_ACCESS_KEY=secret-new\n" in body
    assert "export AWS_SESSION_TOKEN=token-new\n" in body


def test_persisted_session_is_not_world_readable(itd, monkeypatch):
    _patch_boto3(itd, monkeypatch, _FakeSTS())
    itd._CredentialRefresher("us-east-1").refresh()
    mode = _creds_file(itd).stat().st_mode & 0o777
    assert mode == 0o600, f"live credentials left at mode {mode:o}"


def test_each_refresh_rewrites_the_file(itd, monkeypatch):
    # The file's freshness is the whole guarantee: it must never be older than
    # CREDENTIAL_REFRESH_SECONDS, so every successful refresh has to overwrite
    # it rather than only the first one creating it.
    sts = _FakeSTS()
    _patch_boto3(itd, monkeypatch, sts)
    r = itd._CredentialRefresher("us-east-1")
    assert r.refresh() is True
    _creds_file(itd).write_text("stale\n")
    assert r.refresh() is True
    assert "AKIA-NEW" in _creds_file(itd).read_text()


def test_a_write_failure_does_not_break_the_refresh(itd, monkeypatch):
    # Losing the log artifacts on a long run beats failing a deploy that
    # worked, so a persistence failure is reported and swallowed.
    _creds_file(itd).mkdir()  # write_text now raises IsADirectoryError (OSError)
    _patch_boto3(itd, monkeypatch, _FakeSTS())
    r = itd._CredentialRefresher("us-east-1")
    assert r.refresh() is True
    assert r._session_kwargs["aws_access_key_id"] == "AKIA-NEW"


def test_a_failed_refresh_persists_nothing(itd, monkeypatch):
    # No file is better than a file holding credentials that were never vended.
    _patch_boto3(itd, monkeypatch, _FakeSTS(fail=True))
    assert itd._CredentialRefresher("us-east-1").refresh() is False
    assert not _creds_file(itd).exists()


def test_the_monitor_mints_a_session_before_it_starts_polling(itd, monkeypatch):
    # A run that finishes just over the hour never reaches the 50-min refresh
    # with enough credential life left, so the up-front mint is what covers it.
    # Drive one poll that reports Succeeded and assert the file is already there.
    minted = []

    class _OneShotCP:
        def get_pipeline_execution(self, **_k):
            return {"pipelineExecution": {"status": "Succeeded"}}

    class _Refresher:
        def __init__(self, region):
            pass

        def refresh(self):
            minted.append(True)
            return True

        def build_clients(self):
            return _OneShotCP(), object()

    monkeypatch.setattr(itd, "_CredentialRefresher", _Refresher)
    monkeypatch.setattr(itd, "resolve_codebuild_log_stream", lambda *a, **k: None)
    monkeypatch.setattr(itd.time, "sleep", lambda *_a, **_k: None)

    assert itd.monitor_pipeline_execution("pipe", "exec-1", max_wait=60) is True
    assert minted, "no session was minted before polling began"


def test_the_deployment_zip_excludes_the_persisted_session(itd, monkeypatch):
    # The zip is uploaded to S3 and unpacked by CodeBuild, and a runner can
    # reuse a workspace between jobs — so a file left by a PREVIOUS run would
    # be shipped unless the packager excludes it.
    captured = []
    monkeypatch.setattr(itd, "run_command", lambda cmd, **k: captured.append(cmd))
    itd.create_deployment_package()
    assert captured, "create_deployment_package ran no command"
    assert itd.AFTER_SCRIPT_CREDS_FILE in captured[0]
    assert f'-x "*{itd.AFTER_SCRIPT_CREDS_FILE}"' in captured[0]


# ---------------------------------------------------------------------------
# The handoff marker.
#
# main() exits 0 on the handoff path as well as on a real Succeeded, so a green
# job is two different claims. Slack read only the exit status and printed
# "✅ PASSED" for both.
# ---------------------------------------------------------------------------


def _run_main(itd, monkeypatch, result):
    monkeypatch.setattr(itd, "create_deployment_package", lambda: None)
    monkeypatch.setattr(itd, "upload_to_s3", lambda bucket: "version-1")
    monkeypatch.setattr(itd, "monitor_pipeline", lambda *a, **k: result)
    with pytest.raises(SystemExit) as excinfo:
        itd.main()
    return excinfo.value.code


def _marker(itd):
    import pathlib

    return pathlib.Path(itd.MONITOR_HANDOFF_MARKER)


def test_handoff_exits_zero_but_leaves_the_marker(itd, monkeypatch):
    assert _run_main(itd, monkeypatch, None) == 0
    assert _marker(itd).is_file()
    # The text is what Slack prepends to the summary, so it has to say plainly
    # that the green status is not a pass.
    assert "does NOT mean the suite" in _marker(itd).read_text()


def test_a_real_pass_leaves_no_marker(itd, monkeypatch):
    assert _run_main(itd, monkeypatch, True) == 0
    assert not _marker(itd).exists()


def test_a_failure_leaves_no_marker(itd, monkeypatch):
    assert _run_main(itd, monkeypatch, False) == 1
    assert not _marker(itd).exists()


def test_stale_run_state_is_cleared_before_the_run(itd, monkeypatch):
    # A runner reuses workspaces. A marker left by the PREVIOUS job would make
    # Slack call this run UNVERIFIED, and a stale pipeline_execution_id.txt
    # would make after_script publish the previous run's CodeBuild logs as this
    # one's. Both are confidently wrong, which beats being absent only in
    # looking plausible.
    import pathlib

    _marker(itd).write_text("left over from the last job\n")
    pathlib.Path("pipeline_execution_id.txt").write_text("stale-execution-id\n")

    assert _run_main(itd, monkeypatch, True) == 0
    assert not _marker(itd).exists()
    assert not pathlib.Path("pipeline_execution_id.txt").exists()
