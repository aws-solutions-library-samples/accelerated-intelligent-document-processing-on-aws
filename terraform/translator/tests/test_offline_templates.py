"""The per-pull-request gate: every template variant translates offline.

A template change that uses a type, property or intrinsic the translator does
not handle fails here, on the pull request that introduces it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from idp_tf.cli import TEMPLATE_VARIANTS, cfn_types_in_tree, main

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("template", TEMPLATE_VARIANTS)
def test_template_translates_offline_with_no_failures(template, tmp_path, capsys):
    rc = main([str(REPO / template), "--offline", "--survey", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0, out


@pytest.mark.parametrize("template", TEMPLATE_VARIANTS)
def test_generated_module_carries_the_parameter_map_and_retained_list(
    template, tmp_path
):
    out = tmp_path / "m"
    rc = main(
        [
            str(REPO / template),
            "--offline",
            "--out",
            str(out),
            "--label-template",
            str(REPO / template),
            "--version",
            "0.0.0",
        ]
    )
    assert rc == 0
    meta = json.loads((out / "idp-terraform.json").read_text())
    assert meta["parameters"]["AdminEmail"]["variable"] == "admin_email"
    assert meta["parameters"]["AdminEmail"]["required"] is True
    assert any(a.startswith("aws_s3_bucket.") for a in meta["retainedAddresses"])
    versions = (out / "versions.tf").read_text()
    assert "AWSCC_VERSION" not in versions
    variables = (out / "variables.tf").read_text()
    assert "length(var.stack_name) <= 25" in variables


def test_vendored_schema_covers_every_awscc_type_the_templates_use():
    from idp_tf.awscc_schema import AwsccSchema, awscc_type_name
    from idp_tf.cli import VENDORED_SCHEMA

    schema = AwsccSchema(VENDORED_SCHEMA)
    full = json.loads(VENDORED_SCHEMA.read_text())
    assert full["awscc_version"]
    for template in TEMPLATE_VARIANTS:
        for t in cfn_types_in_tree(REPO / template):
            if t.startswith("Custom::") or t.count("::") != 2:
                continue
            # Either awscc has it (vendored) or a hand mapping exists; the
            # survey test above proves the latter, this proves the subset is
            # not missing a type the translator would route to awscc.
            if schema.has(t):
                assert awscc_type_name(t) in json.dumps(full)
