"""The per-pull-request gate: every template variant translates offline.

A template change that uses a type, property or intrinsic the translator does
not handle fails here, on the pull request that introduces it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from idp_tf.cli import TEMPLATE_VARIANTS, main

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(("template", "variant"), TEMPLATE_VARIANTS)
def test_template_translates_offline_with_no_failures(
    template, variant, tmp_path, capsys
):
    args = [str(REPO / template), "--offline", "--survey", "--out", str(tmp_path)]
    if variant:
        args += ["--variant", variant]
    rc = main(args)
    out = capsys.readouterr().out
    assert rc == 0, out


@pytest.mark.parametrize("template", ["template.yaml"])
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


def test_every_directory_reader_is_in_the_standard_template():
    """TERRAFORM_DIRECTORY_READERS names real functions; a rename fails here."""
    from idp_tf.translate import TERRAFORM_DIRECTORY_READERS

    types_by_lid = {}
    from idp_tf.expand import expand, load_template, offline_placeholders

    template = expand(offline_placeholders(load_template(REPO / "template.yaml")))
    for lid, res in template["Resources"].items():
        types_by_lid[lid] = res["Type"]
    for lid in TERRAFORM_DIRECTORY_READERS:
        assert types_by_lid.get(lid) == "AWS::Lambda::Function", lid


# ------------------------------------------------------------------ helpers
RESOURCE_BLOCK = re.compile(r'^(resource|data) "([a-z0-9_]+)" "([a-z0-9_]+)" \{', re.M)
MODULE_BLOCK = re.compile(r'^module "([a-z0-9_]+)" \{', re.M)
DEPENDS_ON = re.compile(r"^\s*depends_on = \[(.*?)\]", re.M | re.S)


def _translate(out: Path, variant: str | None = None) -> Path:
    args = [
        str(REPO / "template.yaml"),
        "--offline",
        "--out",
        str(out),
        "--label-template",
        str(REPO / "template.yaml"),
        "--version",
        "0.0.0",
    ]
    if variant:
        args += ["--variant", variant]
    assert main(args) == 0
    return out


def _modules(root: Path) -> list[Path]:
    """The root module and every nested module directory under it."""
    return [root, *sorted(p for p in (root / "modules").glob("*") if p.is_dir())]


def _module_text(module: Path) -> str:
    return "\n".join(p.read_text() for p in sorted(module.glob("*.tf")))


@pytest.fixture(scope="module")
def standard_module(tmp_path_factory) -> Path:
    return _translate(tmp_path_factory.mktemp("std") / "m")


# ----------------------------------------------- dangling dependency (govcloud)
def _cached_terraform() -> tuple[Path, Path] | None:
    """A terraform binary and provider cache left by ``make terraform-check``."""
    cache = Path(
        os.environ.get("TERRAFORM_CHECK_CACHE", "~/.cache/idp-terraform-check")
    ).expanduser()
    found = sorted(cache.glob("bin/terraform/*/*/terraform"))
    plugins = cache / "plugins"
    if not found or not (plugins / "registry.terraform.io").is_dir():
        return None
    return found[-1], plugins


@pytest.mark.parametrize("variant", [v for t, v in TEMPLATE_VARIANTS])
def test_every_depends_on_names_a_declared_resource(variant, tmp_path):
    """A planned dependency must name something the module actually declares.

    The GovCloud transform sets ``WebUIBucket.WebsiteConfiguration`` to
    ``AWS::NoValue``. The bucket mapper's emitter wrote nothing for it while its
    planner still added a dependency on the website configuration, so every
    dependant of the bucket named an undeclared resource. The translation
    succeeded; only ``terraform validate`` refused the module.
    """
    root = _translate(tmp_path / "m", variant)
    dangling = []
    for module in _modules(root):
        text = _module_text(module)
        declared = {f"{t}.{n}" for kind, t, n in RESOURCE_BLOCK.findall(text)}
        declared |= {
            f"data.{t}.{n}"
            for kind, t, n in RESOURCE_BLOCK.findall(text)
            if kind == "data"
        }
        declared |= {f"module.{n}" for n in MODULE_BLOCK.findall(text)}
        for body in DEPENDS_ON.findall(text):
            for ref in re.findall(r"[a-z][a-z0-9_.]*", body):
                if ref not in declared:
                    dangling.append(f"{module.relative_to(root)}: {ref}")
    assert not dangling, sorted(set(dangling))[:20]


def test_the_govcloud_module_validates_when_terraform_is_cached(tmp_path):
    """Runs ``terraform validate`` offline on the GovCloud module.

    Uses the binary and provider cache ``make terraform-check`` leaves behind;
    with none present this is skipped, and ``make terraform-check`` (which
    validates every variant on every pinned binary) remains the gate.
    """
    cached = _cached_terraform()
    if cached is None:
        pytest.skip(
            "no cached terraform binary and providers; run `make terraform-check`"
        )
    exe, plugins = cached
    root = _translate(tmp_path / "m", "govcloud")
    gate = _load_gate()
    (root / gate.OVERRIDE_FILE).write_text(gate.override_tf(gate.gate_providers([])))
    env = {**os.environ, "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1"}
    env.pop("TF_CLI_ARGS", None)
    init = subprocess.run(  # nosec B603 - fixed argv, no shell
        [
            str(exe),
            "init",
            "-backend=false",
            "-input=false",
            "-no-color",
            f"-plugin-dir={plugins}",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if init.returncode != 0:
        pytest.skip(f"offline init failed (provider cache incomplete?): {init.stderr}")
    validate = subprocess.run(  # nosec B603 - fixed argv, no shell
        [str(exe), "validate", "-json", "-no-color"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(validate.stdout)
    errors = [d for d in report["diagnostics"] if d["severity"] == "error"]
    assert report["valid"] and not errors, errors[:5]


def _load_gate():
    import importlib.util

    script = REPO / "scripts" / "terraform_check.py"
    spec = importlib.util.spec_from_file_location("_terraform_check_gate", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ IAM roles
ROLE_BLOCK = re.compile(
    r'^resource "aws_iam_role" "([a-z0-9_]+)" \{\n(.*?)\n\}\n', re.M | re.S
)
DEFAULT_NAME = re.compile(
    r'^\s*name = "\$\{substr\("(?P<pair>[^"]+)", 0, (?P<n>\d+)\)\}-'
    r'\$\{substr\(md5\("(?P=pair)(?P<region>-\$\{local\.pseudo\.region\})?"\), 0, 8\)\}"$',
    re.M,
)
NESTED_STACK_NAME = re.compile(
    r'module "([a-z0-9_]+)" \{.*?\n\s*stack_name = "([^"]+)"', re.S
)


def _module_stack_names(root: Path, stack: str) -> dict[Path, str]:
    """``{module dir: its var.stack_name}`` for a root deployed as ``stack``."""
    names = {root: stack}
    pending = [root]
    while pending:
        module = pending.pop()
        for child, expr in NESTED_STACK_NAME.findall((module / "main.tf").read_text()):
            child_dir = module / "modules" / child
            if child_dir.is_dir() or (root / "modules" / child).is_dir():
                child_dir = (
                    child_dir if child_dir.is_dir() else root / "modules" / child
                )
                names[child_dir] = expr.replace("${var.stack_name}", names[module])
                pending.append(child_dir)
    return names


RETAINED_NAME = re.compile(
    r'^\s*name = "\$\{substr\("(?P<pair>[^"]+)", 0, (?P<n>\d+)\)\}-'
    r'\$\{substr\(md5\("(?P=pair)-\$\{local\.pseudo\.region\}"\), 0, 8\)\}-'
    r'\$\{time_static\.[a-z0-9_]+(?:\[0\])?\.unix\}"$',
    re.M,
)


#: The region names are evaluated for; any region gives the same prefix.
REGION = "us-west-2"


def _evaluate_name(body: str, stack: str) -> str | None:
    m = RETAINED_NAME.search(body)
    if m:
        pair = m["pair"].replace("${var.stack_name}", stack)
        digest = hashlib.md5(f"{pair}-{REGION}".encode()).hexdigest()[:8]  # nosec B324 - mirrors HCL md5()
        return f"{pair[: int(m['n'])]}-{digest}-1767225600"  # a 10-digit unix time
    m = DEFAULT_NAME.search(body)
    if m:
        pair = m["pair"].replace("${var.stack_name}", stack)
        hashed = f"{pair}-{REGION}" if m["region"] else pair
        digest = hashlib.md5(hashed.encode()).hexdigest()[:8]  # nosec B324 - mirrors HCL md5()
        return f"{pair[: int(m['n'])]}-{digest}"
    m = re.search(r'^\s*name = "([^"$]*\$\{var\.stack_name\}[^"]*)"$', body, re.M)
    if m and "${" not in m[1].replace("${var.stack_name}", ""):
        return m[1].replace("${var.stack_name}", stack)
    return None


def test_every_role_is_named_after_the_stack_and_takes_the_boundary(standard_module):
    """A deploying role scoped to ``role/<stack prefix>*`` can manage every role.

    Left unnamed, the provider names a role ``terraform-<timestamp>``, which such
    a role may neither create nor pass. Each role must also carry the
    permissions boundary when ``PermissionsBoundaryArn`` is set, the bridge role
    the translator adds included.
    """
    stack = "idp-longest-stack-nam25"  # the variable's length limit is 25
    assert len(stack) <= 25
    names: dict[str, str] = {}
    problems = []
    for module, module_stack in _module_stack_names(standard_module, stack).items():
        text = _module_text(module)
        for label, body in ROLE_BLOCK.findall(text):
            where = f"{module.relative_to(standard_module)}:{label}"
            name = _evaluate_name(body, module_stack)
            if name is None:
                problems.append(f"{where}: name is not derived from the stack name")
                continue
            if not name.startswith(stack):
                problems.append(f"{where}: {name} does not start with {stack}")
            if len(name) > 64:
                problems.append(f"{where}: {name} is longer than IAM's 64")
            if name in names:
                problems.append(f"{where}: {name} is also {names[name]}")
            names[name] = where
            if not re.search(
                r"^\s*permissions_boundary = .*var\.permissions_boundary_arn",
                body,
                re.M,
            ):
                problems.append(f"{where}: no permissions_boundary")
            if "create_before_destroy = true" not in body:
                problems.append(f"{where}: a rename would delete before creating")
            generated = "md5(" in body or "time_static." in body
            if generated and "ignore_changes = [name, name_prefix]" not in body:
                # A generated name is kept for the role's life: replacing a role
                # replaces the custom resources' dependants (see aws_mappers).
                problems.append(f"{where}: a generated name could be changed")
    assert names, "no aws_iam_role found"
    assert not problems, problems[:20]


def test_translation_is_deterministic(standard_module, tmp_path):
    """Translating twice gives identical files, so a re-plan after the role
    rename is empty: nothing in a name depends on when it was generated."""
    again = _translate(tmp_path / "again")
    first = {
        p.relative_to(standard_module): p.read_bytes()
        for p in standard_module.rglob("*")
        if p.is_file()
    }
    second = {
        p.relative_to(again): p.read_bytes() for p in again.rglob("*") if p.is_file()
    }
    assert first.keys() == second.keys()
    differing = [str(k) for k in first if first[k] != second[k]]
    assert not differing, differing


def test_a_retained_role_keeps_its_name_and_cannot_collide_on_redeploy(
    standard_module,
):
    """A delete keeps a retained role, so its name carries a creation-time
    suffix (as a retained table's does) and later name changes are ignored
    rather than replacing a resource CloudFormation keeps."""
    meta = json.loads((standard_module / "idp-terraform.json").read_text())
    retained = [a for a in meta["retainedAddresses"] if ".aws_iam_role." in a]
    assert retained, "the template no longer retains a role; drop this test"
    for address in retained:
        *path, _, label = address.split(".")
        # module.<a>.module.<b>.aws_iam_role.<label> -> modules/<a>/modules/<b>
        module = standard_module
        for name in path[1::2]:
            module = module / "modules" / name
        bodies = dict(ROLE_BLOCK.findall(_module_text(module)))
        body = bodies[label]
        assert RETAINED_NAME.search(body), (address, body)
        assert "ignore_changes = [name, name_prefix]" in body, address


def test_a_permission_granted_to_a_role_survives_the_role_being_replaced(
    standard_module,
):
    """Roles are replaced create-before-destroy, and Terraform applies that order
    to every dependant replaced with them. A Lambda permission whose principal is
    a role is replaced whenever the role is, so a fixed `statement_id` makes the
    new permission collide with the old one (409) while both exist. Measured on
    a live upgrade that renamed every role."""
    problems = []
    for module in _modules(standard_module):
        text = _module_text(module)
        for m in re.finditer(
            r'^resource "aws_lambda_permission" "(\w+)" \{\n(.*?)^\}', text, re.M | re.S
        ):
            body = m.group(2)
            if re.search(r"^\s*principal = aws_iam_role\.", body, re.M) and re.search(
                r"^\s*statement_id = ", body, re.M
            ):
                problems.append(f"{module.name}: {m.group(1)}")
    assert not problems, problems[:20]


# ------------------------------------------------- partition independence
@pytest.mark.parametrize("variant", [None, "headless", "govcloud"])
def test_the_module_does_not_depend_on_the_region_sam_is_given(
    variant, tmp_path, monkeypatch
):
    """SAM is handed a fixed session region because it refuses to run without
    one. Nothing it emits may depend on which: a module built with a
    commercial region must be byte-identical to one built with a GovCloud
    region, or the GovCloud module carries commercial ARNs and policies."""
    import idp_tf.expand as expand

    built = {}
    for region in ("us-east-1", "us-gov-west-1"):
        monkeypatch.setattr(expand, "SAM_REGION", region)
        built[region] = _translate(tmp_path / region, variant)
    a, b = built["us-east-1"], built["us-gov-west-1"]
    files_a = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert files_a == files_b
    differing = [
        str(f) for f in files_a if (a / f).read_bytes() != (b / f).read_bytes()
    ]
    assert not differing, differing


_SOURCE_TEMPLATES = [
    REPO / "template.yaml",
    *sorted(REPO.glob("nested/*/template.yaml")),
    *sorted(REPO.glob("patterns/*/template.yaml")),
]
_PARTITION_SUFFIX = re.compile(r"\.amazonaws\.com(?!\.cn)\b")
_HCL_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')


@pytest.mark.parametrize("variant", [None, "headless", "govcloud"])
def test_the_generated_module_names_no_partition(variant, tmp_path):
    """The generated counterpart of `make check-arn-partitions`, which reads only
    the CloudFormation templates. No `arn:aws:` may appear at all; an
    `amazonaws.com` literal may appear only where the template author wrote that
    exact string (the per-partition Cognito principals, the content security
    policy), which the template gate already rules on."""
    authored = "\n".join(p.read_text() for p in _SOURCE_TEMPLATES)
    root = _translate(tmp_path / "m", variant)
    problems = []
    for f in sorted(root.rglob("*.tf")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            where = f"{f.relative_to(root)}:{n}"
            if "arn:aws:" in line:
                problems.append(f"{where}: arn:aws: literal")
            for literal in _HCL_STRING.findall(line):
                if _PARTITION_SUFFIX.search(literal) and literal not in authored:
                    problems.append(f"{where}: {literal[:80]}")
    assert not problems, problems[:20]


def test_only_account_wide_names_carry_the_region(standard_module):
    """IAM role names are unique across an account, so the same stack name in two
    regions would collide; the region joins their hash. Every other generated
    name is unique within a region, and adding the region there would rename,
    and so replace, a function or table on upgrade."""
    roles, others = 0, []
    for module in _modules(standard_module):
        text = _module_text(module)
        for m in re.finditer(
            r'^resource "(\w+)" "(\w+)" \{\n(.*?)^\}', text, re.M | re.S
        ):
            kind, label, body = m.groups()
            for line in re.findall(r"^\s*name = .*md5\(.*$", body, re.M):
                regional = "local.pseudo.region" in line
                if kind == "aws_iam_role":
                    roles += 1
                    assert regional, f"{module.name}:{label}"
                elif regional:
                    others.append(f"{module.name}:{kind}.{label}")
    assert roles, "no generated role names found"
    assert not others, others[:10]
