"""Unit tests for delete_apigw_test_vpc — the ENI-blocked test VPC teardown.

A `*-apigw-vpc` stack whose Lambda ENIs outlive it goes DELETE_FAILED, and the
reaper that re-attempts it runs at the head of every pipeline run. Two
properties therefore matter as much as eventual success:

* a stack already in DELETE_FAILED must not be given a plain delete first,
  because that re-attempts the same resources and spends a 15-minute waiter to
  reach the same state;
* when the ENI sweep cannot clear the blocker, the stack must still leave
  DELETE_FAILED via CloudFormation's RetainResources escape hatch, which needs
  no EC2 permission — otherwise the VPC stays stranded against a limit of five
  and the cost recurs on every run, which is what happened for eleven weeks.

These mock boto3 so they need no AWS.
"""

import pytest

pytestmark = pytest.mark.unit


class _FakeCfn:
    """CloudFormation double driven by a scripted sequence of stack statuses."""

    def __init__(self, statuses, failed_ids=(), outputs=None, delete_raises=False):
        # statuses is consumed one entry per describe_stacks call; the last
        # entry repeats, so a test only spells out the transitions it cares
        # about.
        self._statuses = list(statuses)
        self._failed_ids = list(failed_ids)
        self._outputs = outputs or {}
        self._delete_raises = delete_raises
        self.delete_calls = []
        self.waits = 0

    def describe_stacks(self, StackName):
        status = self._statuses[0]
        if len(self._statuses) > 1:
            self._statuses.pop(0)
        return {
            "Stacks": [
                {
                    "StackStatus": status,
                    "Outputs": [
                        {"OutputKey": k, "OutputValue": v}
                        for k, v in self._outputs.items()
                    ],
                }
            ]
        }

    def describe_stack_resources(self, StackName):
        return {
            "StackResources": [
                {"LogicalResourceId": lid, "ResourceStatus": "DELETE_FAILED"}
                for lid in self._failed_ids
            ]
        }

    def delete_stack(self, **kwargs):
        self.delete_calls.append(kwargs)
        if self._delete_raises:
            raise RuntimeError("delete rejected")

    def get_waiter(self, name):
        outer = self

        class _Waiter:
            def wait(self, **kwargs):
                outer.waits += 1
                if outer._delete_raises:
                    raise RuntimeError("Waiter StackDeleteComplete failed")

        return _Waiter()


def _install(cbd, monkeypatch, cfn, ec2=None):
    def _client(name, *a, **k):
        if name == "cloudformation":
            return cfn
        if name == "ec2":
            if ec2 is None:
                raise RuntimeError("no ec2 double installed")
            return ec2
        raise AssertionError(f"unexpected client {name}")

    monkeypatch.setattr(cbd.boto3, "client", _client)
    monkeypatch.setattr(cbd.time, "sleep", lambda *_: None)
    return cfn


class _DenyingEc2:
    """EC2 double that refuses DeleteNetworkInterface the way a missing grant does."""

    def __init__(self, eni_ids):
        self._eni_ids = list(eni_ids)

    def describe_network_interfaces(self, Filters):
        return {
            "NetworkInterfaces": [
                {"NetworkInterfaceId": i, "Status": "available"} for i in self._eni_ids
            ]
        }

    def delete_network_interface(self, NetworkInterfaceId):
        raise _ClientErrorish("UnauthorizedOperation")


class _ClientErrorish(Exception):
    """Minimal stand-in carrying botocore's `response` shape."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def test_already_failed_stack_skips_the_plain_delete(cbd, monkeypatch):
    # The whole per-run cost of the stuck stack was this waiter. A stack found
    # in DELETE_FAILED must go straight to the retain path.
    cfn = _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], failed_ids=["LambdaSecurityGroup"]),
        ec2=_DenyingEc2(["eni-1"]),
    )
    cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc")
    # Exactly one delete, and it carries RetainResources — no bare attempt.
    assert len(cfn.delete_calls) == 1
    assert cfn.delete_calls[0]["RetainResources"] == ["LambdaSecurityGroup"]


def test_retain_path_clears_a_stack_the_eni_sweep_cannot(cbd, monkeypatch):
    # The real failure: sweep refused for lack of ec2:DeleteNetworkInterface.
    # RetainResources needs no EC2 permission, so the stack still goes away.
    cfn = _install(
        cbd,
        monkeypatch,
        _FakeCfn(
            ["DELETE_FAILED"],
            failed_ids=["LambdaSecurityGroup", "PrivateSubnet1", "PrivateSubnet2"],
            outputs={"VpcId": "vpc-1"},
        ),
        ec2=_DenyingEc2(["eni-1", "eni-2"]),
    )
    cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc")
    assert cfn.delete_calls[-1]["RetainResources"] == [
        "LambdaSecurityGroup",
        "PrivateSubnet1",
        "PrivateSubnet2",
    ]


def test_a_refused_sweep_is_reported_as_a_permission_gap(cbd, monkeypatch, capsys):
    # "swept 0" alone is indistinguishable from having nothing to sweep, which
    # is how this stayed invisible. The refusal must name itself, and it must
    # name the policy to change: the grant reads as present in the template
    # under a condition this account does not meet, so a reader sent to
    # CodeBuildRole's conditional policy finds it there and concludes the
    # deployed stack is merely stale. The message has to point at the
    # unconditional policy instead or it sends the next reader the wrong way.
    _install(
        cbd,
        monkeypatch,
        _FakeCfn(
            ["DELETE_FAILED"],
            failed_ids=["LambdaSecurityGroup"],
            outputs={"VpcId": "vpc-1"},
        ),
        ec2=_DenyingEc2(["eni-1"]),
    )
    cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc")
    out = capsys.readouterr().out
    assert "HARNESS PERMISSION GAP" in out
    assert "CodeBuildEC2VPCPolicy" in out
    assert "codepipeline-s3.yml" in out
    assert "DeployInVPC" in out


def test_healthy_stack_still_gets_a_plain_delete(cbd, monkeypatch):
    # The ordinary path must not regress: a stack that is not DELETE_FAILED is
    # deleted plainly, with no RetainResources.
    cfn = _install(cbd, monkeypatch, _FakeCfn(["CREATE_COMPLETE", "DELETE_COMPLETE"]))
    cbd.delete_apigw_test_vpc("idp-0920-120000-apigw-vpc")
    assert cfn.delete_calls == [{"StackName": "idp-0920-120000-apigw-vpc"}]


def test_no_retain_attempt_while_a_delete_is_still_in_progress(cbd, monkeypatch):
    # RetainResources is only legal for DELETE_FAILED. If the first delete timed
    # out but the stack is still deleting, asking to retain would be rejected —
    # leave it for the next run.
    cfn = _install(
        cbd,
        monkeypatch,
        _FakeCfn(["CREATE_COMPLETE", "DELETE_IN_PROGRESS"], delete_raises=True),
        ec2=_DenyingEc2([]),
    )
    cbd.delete_apigw_test_vpc("idp-0920-120000-apigw-vpc")
    assert all("RetainResources" not in c for c in cfn.delete_calls)


def test_sweep_reports_denied_separately_from_swept_nothing(cbd, monkeypatch):
    _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}),
        ec2=_DenyingEc2(["eni-1"]),
    )
    deleted, denied = cbd._force_delete_vpc_stack_enis("idp-0719-012130-apigw-vpc")
    assert (deleted, denied) == (0, True)


def test_sweep_with_nothing_to_do_is_not_denied(cbd, monkeypatch):
    _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}),
        ec2=_DenyingEc2([]),
    )
    assert cbd._force_delete_vpc_stack_enis("idp-0719-012130-apigw-vpc") == (0, False)


def test_reaper_never_raises_when_cloudformation_errors(cbd, monkeypatch):
    class _Boom:
        def describe_stacks(self, StackName):
            raise RuntimeError("throttled")

        def delete_stack(self, **kwargs):
            raise RuntimeError("throttled")

        def describe_stack_resources(self, StackName):
            raise RuntimeError("throttled")

        def get_waiter(self, name):
            raise RuntimeError("throttled")

    _install(cbd, monkeypatch, _Boom(), ec2=_DenyingEc2([]))
    # Best effort: must swallow, not propagate.
    cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc")
