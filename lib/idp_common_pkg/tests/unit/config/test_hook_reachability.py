# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#982: a pipeline hook registered where its own processing mode can't reach it.

`postOcr`, `postClassification` and `postExtraction` are states on the Pipeline
branch of the unified state machine only — BDA performs OCR, classification and
extraction inside one Bedrock Data Automation invocation, so there is no separate
step to hook after. A configuration with `use_bda: true` that registers a hook at
one of them describes a hook that is never invoked: nothing runs, no `onError:
fail` policy gates, and the execution history is empty because the dispatcher was
never called.

The config-side write paths react here. `validate_config` — behind `idp-cli
config-validate` and `config-upload` — turns a gating registration into an ERROR
and an advisory one into a warning. `ConfigurationManager._reject_inert_gating_hooks`,
on the `updateConfiguration` mutation the Configuration UI uses, raises on the
gating case, scoped to what the incoming delta actually writes. The two
feature-install paths are covered by the feature-platform suites
(`test_register_feature_hooks.py`, `test_apply_feature_config_preset.py`).

None of this is an `IDPConfig` validator, on purpose: a record already stored in
this shape must still LOAD, or upgrading would break every Lambda that reads the
configuration.
"""

import pytest

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.hook_reachability import (
    InertGatingHookError,
    delta_touches_hook_registration,
    reject_inert_gating_hooks,
    unreachable_hook_registrations,
)
from idp_common.config.merge_utils import _validate_pipeline_hook_reachability
from idp_common.config.models import IDPConfig

pytestmark = pytest.mark.unit

_ARN = "arn:aws:lambda:us-east-1:123456789012:function:redact"


def _config(use_bda, point_section="ocr", on_error="fail", **extra):
    return {
        "use_bda": use_bda,
        point_section: {
            "postHook": [
                {
                    "featureId": "pii-redactor",
                    "arn": _ARN,
                    "onError": on_error,
                    "enabled": True,
                }
            ]
        },
        **extra,
    }


def _findings(cfg):
    return unreachable_hook_registrations(cfg)


def test_gating_hook_at_a_bda_unreachable_point_is_a_finding():
    findings = _findings(_config(True))
    assert len(findings) == 1
    assert findings[0]["point"] == "postOcr"
    assert findings[0]["gating"] is True
    assert findings[0]["processingMode"] == "bda"
    # The message must name the remedy, not just the problem.
    assert "preprocessing" in findings[0]["message"]


def test_advisory_hook_at_a_bda_unreachable_point_is_a_non_gating_finding():
    findings = _findings(_config(True, on_error="continue"))
    assert len(findings) == 1
    assert findings[0]["gating"] is False


def test_the_same_hook_in_pipeline_mode_is_not_a_finding():
    """The other direction — a check that flagged everything would look as green."""
    assert _findings(_config(False)) == []


@pytest.mark.parametrize(
    "section", ["rule_validation", "summarization", "preprocessing", "postprocessing"]
)
def test_points_both_modes_reach_are_never_findings(section):
    if section in ("preprocessing", "postprocessing"):
        cfg = {
            "use_bda": True,
            section: {
                "enabled": True,
                "featureId": "pii-anonymizer",
                "arn": _ARN,
                "onError": "fail",
            },
        }
    else:
        cfg = _config(True, point_section=section)
    assert _findings(cfg) == []


def test_stringified_use_bda_is_understood():
    """Config values are written to DynamoDB stringified, so `"true"` is normal."""
    assert len(_findings(_config("true"))) == 1
    assert _findings(_config("false")) == []


def test_an_unrecognisable_mode_yields_nothing():
    """Guessing would name the wrong hooks as inert."""
    assert _findings(_config(None)) == []
    assert _findings(_config("maybe")) == []


def test_disabled_and_arnless_registrations_are_not_findings():
    """The dispatcher skips both, so neither can be an inert gate."""
    cfg = {
        "use_bda": True,
        "ocr": {
            "postHook": [
                {"featureId": "a", "arn": _ARN, "onError": "fail", "enabled": False},
                {"featureId": "b", "onError": "fail", "enabled": True},
            ]
        },
    }
    assert _findings(cfg) == []


def test_validate_config_makes_a_gating_registration_an_error():
    result = {"valid": True, "errors": [], "warnings": []}
    _validate_pipeline_hook_reachability(_config(True), result)
    assert result["valid"] is False
    assert len(result["errors"]) == 1
    assert "postOcr" in result["errors"][0]
    assert result["warnings"] == []


def test_validate_config_makes_an_advisory_registration_a_warning():
    result = {"valid": True, "errors": [], "warnings": []}
    _validate_pipeline_hook_reachability(_config(True, on_error="continue"), result)
    assert result["valid"] is True
    assert result["errors"] == []
    assert len(result["warnings"]) == 1


def test_update_mutation_refuses_to_save_a_gating_registration():
    """The UI/API save path. Raised from the manager, so the resolver's error
    response carries the message and the row is not written."""
    config = IDPConfig(**_config(True))
    with pytest.raises(InertGatingHookError) as excinfo:
        ConfigurationManager._reject_inert_gating_hooks(config)
    assert "postOcr" in str(excinfo.value)
    assert "onError=fail" in str(excinfo.value)
    # A ValueError subclass, so callers catching that still work; a distinct type so
    # the API resolver can report a deliberate refusal as a validation error rather
    # than as an unexpected server fault.
    assert isinstance(excinfo.value, ValueError)


def test_update_mutation_saves_an_advisory_registration():
    ConfigurationManager._reject_inert_gating_hooks(
        IDPConfig(**_config(True, on_error="continue"))
    )


def test_update_mutation_saves_a_reachable_gating_registration():
    ConfigurationManager._reject_inert_gating_hooks(IDPConfig(**_config(False)))


# --------------------------------------------------------------------------
# The refusal is scoped to what the incoming write actually changes.
#
# `handle_update_custom_configuration` merges a DELTA onto the stored config, and
# three of its five callers are BDA blueprint↔class synchronisation
# (`idp_common.bda.bda_blueprint_service`), which sends `{"classes": [...]}`, runs
# only in BDA mode, and swallows exceptions. An unscoped check would refuse those
# over a hook they never mentioned and the classes would silently not sync — the
# same fail-open shape one layer up. It would also refuse an operator's unrelated
# save because of a hook that was already stored.
# --------------------------------------------------------------------------


_CLASSES_DELTA = {"classes": [{"$id": "Invoice"}]}
_HOOK_DELTA = {"ocr": {"postHook": [{"featureId": "x", "arn": _ARN}]}}


def test_a_classes_only_delta_does_not_refuse_a_pre_existing_hook():
    """The blueprint-sync payload, verbatim."""
    ConfigurationManager._reject_inert_gating_hooks(
        IDPConfig(**_config(True)), _CLASSES_DELTA
    )


def test_a_delta_that_writes_the_hook_is_refused():
    with pytest.raises(InertGatingHookError):
        ConfigurationManager._reject_inert_gating_hooks(
            IDPConfig(**_config(True)), _HOOK_DELTA
        )


def test_a_delta_that_flips_the_mode_is_refused():
    """Turning BDA mode on is what makes an already-stored hook inert, so the
    write that does it is exactly the one that must be told."""
    with pytest.raises(InertGatingHookError):
        ConfigurationManager._reject_inert_gating_hooks(
            IDPConfig(**_config(True)), {"use_bda": True}
        )


def test_an_unrelated_edit_to_the_same_section_is_not_refused():
    """Editing `ocr.image.dpi` is not a statement about `ocr.postHook`."""
    ConfigurationManager._reject_inert_gating_hooks(
        IDPConfig(**_config(True)), {"ocr": {"image": {"target_width": 1024}}}
    )


@pytest.mark.parametrize(
    "delta,point,expected",
    [
        ({"use_bda": True}, "postOcr", True),
        ({"ocr": {"postHook": []}}, "postOcr", True),
        ({"ocr": {"image": {}}}, "postOcr", False),
        ({"classes": []}, "postOcr", False),
        ({"preprocessing": {"arn": _ARN}}, "preprocessing", True),
        # A preset that ships only `args` has not named a hook yet — the ARN is
        # filled in at install — so it is not a registration.
        ({"preprocessing": {"args": []}}, "preprocessing", False),
        ({}, "postOcr", False),
    ],
)
def test_delta_touches_hook_registration(delta, point, expected):
    assert delta_touches_hook_registration(delta, point) is expected


def test_reject_returns_advisory_findings_instead_of_raising():
    advisory = reject_inert_gating_hooks(_config(True, on_error="continue"))
    assert len(advisory) == 1
    assert advisory[0]["gating"] is False


def test_reset_to_default_is_deliberately_not_checked():
    """Resetting a version to `default` is exempt, and that is on purpose.

    Reset is the escape hatch from a bad version; if the `default` row carries an
    inert gating hook, refusing the reset wedges the admin with no in-UI way out,
    and the copy introduces nothing that was not already stored. The runtime audit
    still reports the hook on every document. Asserted against the source so the
    exemption cannot be removed silently.
    """
    import inspect

    source = inspect.getsource(ConfigurationManager.handle_update_custom_configuration)
    reset_branch = source.split("if reset_to_default:", 1)[1].split(
        "if save_as_default:", 1
    )[0]
    assert "_reject_inert_gating_hooks" not in reset_branch
    assert "Deliberately NOT subject to _reject_inert_gating_hooks" in source


def test_a_stored_config_in_this_shape_still_loads():
    """Deliberately NOT an IDPConfig validator.

    A record written by an earlier release, or by `register_feature_hooks` writing
    to DynamoDB directly, can already hold this combination. Refusing to
    deserialize it would turn an inert hook into a stack-wide outage on upgrade —
    strictly worse than the fail-open being fixed. The write boundary is where it
    is rejected; the dispatcher reports the already-stored ones at runtime.
    """
    config = IDPConfig(**_config(True))
    assert config.use_bda is True
    assert config.ocr.postHook[0].onError == "fail"
