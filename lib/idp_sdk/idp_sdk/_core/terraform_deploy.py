# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Publish, deploy and delete the accelerator as a Terraform module.

``idp-cli publish --terraform`` translates the published main template with the
translator in ``terraform/translator`` (not an installed package, so it runs as a
subprocess with that directory on ``PYTHONPATH``), zips the module tree
deterministically and uploads it beside the templates::

    <prefix>/<version>/terraform/idp-terraform-<version>.zip
    <prefix>/<version>/terraform/idp-terraform-<version>.zip.sha256
    <prefix>/terraform/idp-terraform-latest.json   {"version", "moduleUrl", "sha256"}

``idp-cli deploy --terraform`` writes a root configuration that calls that module,
runs ``init`` (with ``-upgrade`` only when the module source changed), saves a
plan, refuses it if it would delete a resource CloudFormation retains (unless
told otherwise), and applies exactly that plan; ``idp-cli delete --terraform`` empties the
deployment's buckets, removes the resources CloudFormation would have retained
from the state, and runs ``destroy``. The module's ``idp-terraform.json`` (written
by the translator, at the root of the zip) supplies the CloudFormation-parameter
to Terraform-variable map and the retained addresses.

The pure functions here (URL and key construction, the zip, parameter mapping,
the root configuration, the retained-address selection) carry the logic and are
what the unit tests exercise; :class:`TerraformRunner` is the thin subprocess
wrapper around the ``terraform`` or ``tofu`` binary.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from idp_sdk.exceptions import IDPConfigurationError, IDPStackError

logger = logging.getLogger(__name__)

MANIFEST_NAME = "idp-terraform.json"
POINTER_NAME = "idp-terraform-latest.json"
# Template variants ``publish --headless`` / ``--govcloud`` produce, each
# published as its own module beside the standard one.
VARIANTS = ("headless", "govcloud")
ROOT_MARKER = "# Managed by idp-cli (idp-cli deploy --terraform)."
MODULE_PREFIX = "module.idp."
# Directories never shipped in the module zip: provider caches and build scratch.
ZIP_SKIP_DIRS = frozenset({".terraform", ".build"})
# 1980-01-01 is the earliest timestamp a zip entry can carry.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

_S3_PATH_STYLE = re.compile(
    r"^https://s3[.-]([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/([^/]+)/(.+)$"
)
_S3_VHOST_STYLE = re.compile(
    r"^https://([^/]+?)\.s3[.-]([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/(.+)$"
)


# ---------------------------------------------------------------------------
# Keys, URLs and the pointer
# ---------------------------------------------------------------------------


def check_variant(variant: Optional[str]) -> Optional[str]:
    """``None`` (the standard template) or one of :data:`VARIANTS`."""
    if variant is not None and variant not in VARIANTS:
        raise IDPConfigurationError(
            f"unknown Terraform module variant {variant!r}; expected one of "
            + ", ".join(VARIANTS)
        )
    return variant


def _stem(variant: Optional[str]) -> str:
    """``idp-terraform`` or ``idp-terraform-<variant>``."""
    check_variant(variant)
    return f"idp-terraform-{variant}" if variant else "idp-terraform"


def module_zip_key(prefix: str, version: str, variant: Optional[str] = None) -> str:
    return f"{prefix}/{version}/terraform/{_stem(variant)}-{version}.zip"


def pointer_key(prefix: str, variant: Optional[str] = None) -> str:
    return f"{prefix}/terraform/{_stem(variant)}-latest.json"


def variant_template_key(prefix: str, variant: Optional[str] = None) -> str:
    """Where ``publish`` puts the main template of a variant."""
    check_variant(variant)
    return f"{prefix}/idp-{variant or 'main'}.yaml"


def s3_https_url(region: str, bucket: str, key: str) -> str:
    """The path-style HTTPS URL the template URLs use."""
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    return f"https://s3.{region}.{suffix}/{bucket}/{key}"


def parse_s3_https_url(url: str) -> Optional[Tuple[str, str, str]]:
    """(region, bucket, key) for an S3 HTTPS URL, or None for any other URL."""
    m = _S3_PATH_STYLE.match(url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = _S3_VHOST_STYLE.match(url)
    if m:
        return m.group(2), m.group(1), m.group(3)
    return None


def build_pointer(version: str, module_url: str, sha256: str) -> Dict[str, str]:
    return {"version": version, "moduleUrl": module_url, "sha256": sha256}


def sha256_file_body(sha256: str, zip_name: str) -> str:
    """``sha256sum`` format, so ``sha256sum -c`` reads it."""
    return f"{sha256}  {zip_name}\n"


def default_pointer_url(template_url: str, variant: Optional[str] = None) -> str:
    """The pointer URL beside a default template URL.

    The CloudFormation deploy defaults to ``<bucket>/<prefix>/idp-main.yaml`` for
    the region; the Terraform module's pointer is
    ``<bucket>/<prefix>/terraform/idp-terraform-latest.json``, and a variant's is
    ``<bucket>/<prefix>/terraform/idp-terraform-<variant>-latest.json``.
    """
    base, sep, _ = template_url.rpartition("/")
    if not sep:
        raise IDPConfigurationError(f"cannot derive a pointer URL from {template_url}")
    return f"{base}/terraform/{_stem(variant)}-latest.json"


def terraform_module_source(url: str) -> str:
    """The ``source`` to write for a module URL.

    Terraform's plain HTTPS getter sends no credentials, so it can read only a
    public object. An S3 URL is therefore written with the ``s3::`` getter, which
    signs with the caller's AWS credentials. That getter parses the region and
    bucket out of the host and path and calls the S3 API itself, and it reads a
    three-label ``s3-<region>.amazonaws.com`` host as path style; it misreads
    ``s3.<region>.amazonaws.com`` (four labels) as a virtual-hosted bucket named
    ``s3``. So the path-style dash form is written whatever form was given.
    """
    if url.startswith("s3::"):
        return url
    parsed = parse_s3_https_url(url)
    if not parsed:
        return url
    region, bucket, key = parsed
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    return f"s3::https://s3-{region}.{suffix}/{bucket}/{key}"


# ---------------------------------------------------------------------------
# The module zip
# ---------------------------------------------------------------------------


def iter_module_files(root: Path) -> List[Tuple[str, Path]]:
    """(archive name, path) for every file under ``root``, sorted, skipping
    :data:`ZIP_SKIP_DIRS` at any depth."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ZIP_SKIP_DIRS)
        for name in filenames:
            path = Path(dirpath) / name
            out.append((path.relative_to(root).as_posix(), path))
    return sorted(out)


def deterministic_module_zip(root: Path) -> bytes:
    """Zip a module tree so the same files always give the same bytes: sorted
    entries, a fixed timestamp, fixed permissions and a fixed compression level."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in iter_module_files(root):
            info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
            info.create_system = 3
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes(), compresslevel=9)
    return buf.getvalue()


def manifest_from_zip(data: bytes) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return json.loads(zf.read(MANIFEST_NAME))
    except KeyError as e:
        raise IDPConfigurationError(
            f"the Terraform module has no {MANIFEST_NAME} at its root"
        ) from e
    except zipfile.BadZipFile as e:
        raise IDPConfigurationError("the Terraform module is not a zip file") from e


# ---------------------------------------------------------------------------
# Translation (publish)
# ---------------------------------------------------------------------------


def translator_command(
    source_dir: str,
    template_path: str,
    out_dir: str,
    nested_cache: str,
    version: str,
) -> Tuple[List[str], Dict[str, str]]:
    """The translator invocation and its environment.

    The published template is translated online: nested templates are fetched
    by ``TemplateURL`` into ``nested_cache``. The same template labels the
    deployment, at ``version``.
    """
    translator = Path(source_dir) / "terraform" / "translator"
    if not (translator / "idp_tf" / "cli.py").is_file():
        raise IDPConfigurationError(
            f"Terraform translator not found at {translator}; --terraform needs a "
            "source checkout that contains terraform/translator"
        )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(translator), env.get("PYTHONPATH", "")) if p
    )
    cmd = [
        sys.executable,
        "-m",
        "idp_tf.cli",
        template_path,
        "--out",
        out_dir,
        "--nested-cache",
        nested_cache,
        "--label-template",
        template_path,
        "--version",
        version,
    ]
    return cmd, env


def translate_published_template(
    source_dir: str, template_path: str, work_dir: str, version: str
) -> Path:
    """Translate the published main template into ``<work_dir>/module``."""
    work = Path(work_dir)
    out = work / "module"
    cache = work / "nested-cache"
    for d in (out, cache):
        if d.exists():
            shutil.rmtree(d)
    work.mkdir(parents=True, exist_ok=True)
    cmd, env = translator_command(
        source_dir, template_path, str(out), str(cache), version
    )
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    for line in (proc.stdout or "").splitlines():
        logger.info("translator: %s", line)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise IDPStackError(f"Terraform translation failed: {detail}")
    if not (out / MANIFEST_NAME).is_file():
        raise IDPStackError(f"the translator wrote no {MANIFEST_NAME} in {out}")
    return out


def _put(s3, bucket: str, key: str, body: bytes, content_type: str, public: bool):
    kwargs = {"Bucket": bucket, "Key": key, "Body": body, "ContentType": content_type}
    if not public:
        s3.put_object(**kwargs)
        return
    try:
        s3.put_object(**kwargs, ACL="public-read")
    except Exception as e:  # noqa: BLE001 - only the ownership refusal is retried
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code not in (
            "AccessControlListNotSupported",
            "InvalidBucketAclWithObjectOwnership",
        ):
            raise
        # BucketOwnerEnforced: anonymous read, if any, comes from the bucket policy.
        s3.put_object(**kwargs)


def upload_module(
    s3,
    *,
    module_dir: Path,
    bucket: str,
    prefix: str,
    region: str,
    version: str,
    public: bool = False,
    variant: Optional[str] = None,
) -> Dict[str, str]:
    """Zip and upload the module, its checksum and the latest pointer, under
    the variant's names when ``variant`` is given.

    Returns ``{"moduleUrl", "sha256", "pointerUrl", "version"}``.
    """
    data = deterministic_module_zip(module_dir)
    digest = hashlib.sha256(data).hexdigest()
    zip_key = module_zip_key(prefix, version, variant)
    zip_name = zip_key.rsplit("/", 1)[1]
    module_url = s3_https_url(region, bucket, zip_key)
    _put(s3, bucket, zip_key, data, "application/zip", public)
    _put(
        s3,
        bucket,
        f"{zip_key}.sha256",
        sha256_file_body(digest, zip_name).encode(),
        "text/plain",
        public,
    )
    ptr_key = pointer_key(prefix, variant)
    _put(
        s3,
        bucket,
        ptr_key,
        (
            json.dumps(build_pointer(version, module_url, digest), indent=2) + "\n"
        ).encode(),
        "application/json",
        public,
    )
    return {
        "moduleUrl": module_url,
        "sha256": digest,
        "pointerUrl": s3_https_url(region, bucket, ptr_key),
        "version": version,
    }


# ---------------------------------------------------------------------------
# Fetching (deploy)
# ---------------------------------------------------------------------------


def fetch_url(url: str, s3_factory: Optional[Callable[[str], Any]] = None) -> bytes:
    """Read an S3 HTTPS URL through the S3 API (so a private bucket works), or
    any other HTTPS URL with a plain GET."""
    parsed = parse_s3_https_url(url.removeprefix("s3::"))
    if parsed:
        region, bucket, key = parsed
        if s3_factory is not None:
            s3 = s3_factory(region)
        else:
            import boto3

            s3 = boto3.client("s3", region_name=region)
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if not url.startswith("https://"):
        raise IDPConfigurationError(f"module URL must be HTTPS: {url}")
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as resp:  # nosec B310 - https only
        return resp.read()


def resolve_pointer(pointer: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    url = pointer.get("moduleUrl")
    if not isinstance(url, str) or not url:
        raise IDPConfigurationError("the Terraform pointer names no moduleUrl")
    sha = pointer.get("sha256")
    return url, sha if isinstance(sha, str) else None


def load_module_manifest(
    module_url: str,
    expected_sha256: Optional[str] = None,
    s3_factory: Optional[Callable[[str], Any]] = None,
) -> Dict[str, Any]:
    data = fetch_url(module_url, s3_factory)
    if expected_sha256:
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            raise IDPConfigurationError(
                f"the module at {module_url} has sha256 {actual}, but its pointer "
                f"says {expected_sha256}"
            )
    return manifest_from_zip(data)


# ---------------------------------------------------------------------------
# Parameters and the root configuration (deploy)
# ---------------------------------------------------------------------------


def _is_list_type(cfn_type: str) -> bool:
    return cfn_type.startswith("List<") or cfn_type == "CommaDelimitedList"


def hcl_type(cfn_type: str) -> str:
    if cfn_type == "Number":
        return "number"
    if _is_list_type(cfn_type):
        return "list(string)"
    return "string"


def _convert(name: str, cfn_type: str, value: str) -> Any:
    if cfn_type == "Number":
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                raise IDPConfigurationError(
                    f"parameter {name} is a Number, got {value!r}"
                ) from None
    if _is_list_type(cfn_type):
        return [s.strip() for s in value.split(",") if s.strip()]
    return value


def map_parameters(
    parameters: Dict[str, str], manifest: Dict[str, Any]
) -> Dict[str, Any]:
    """CloudFormation parameter values -> Terraform variable values.

    Unknown names are refused together, naming each. Number values become
    numbers and list-typed values (``List<...>``, ``CommaDelimitedList``) become
    lists split on commas.
    """
    pmap = manifest.get("parameters") or {}
    unknown = sorted(k for k in parameters if k not in pmap)
    if unknown:
        raise IDPConfigurationError(
            "unknown parameter(s) for this Terraform module: "
            + ", ".join(unknown)
            + ". Parameters use their CloudFormation names, as for a CloudFormation "
            "deploy."
        )
    out = {}
    for name, value in parameters.items():
        spec = pmap[name]
        out[spec["variable"]] = _convert(name, str(spec.get("type", "String")), value)
    return out


def missing_required(values: Dict[str, Any], manifest: Dict[str, Any]) -> List[str]:
    """CloudFormation names of required parameters that have no value."""
    pmap = manifest.get("parameters") or {}
    return sorted(
        name
        for name, spec in pmap.items()
        if spec.get("required") and spec["variable"] not in values
    )


# The deployment's parameter values by CloudFormation name, beside tfvars.
PARAMETERS_FILE = "idp-cli-parameters.json"
# The module source the directory was last initialised with (see needs_upgrade).
MODULE_SOURCE_FILE = ".idp-cli-module-source"
# The saved plan `deploy --terraform` checks and then applies; removed afterwards,
# because a plan file holds the variable values, NoEcho ones included.
PLAN_FILE = "idp-cli.tfplan"


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` readable by its owner only, from the start.

    Creating the file with the default mode and narrowing it afterwards leaves
    a window in which another user can open it; a file that already exists
    with a wider mode is narrowed before anything is written to it.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(text)
    finally:
        if fd != -1:
            os.close(fd)


def variable_specs(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Terraform variable name -> {type, sensitive}, including ``stack_name``."""
    specs: Dict[str, Dict[str, Any]] = {
        "stack_name": {"type": "string", "sensitive": False}
    }
    for spec in (manifest.get("parameters") or {}).values():
        specs[spec["variable"]] = {
            "type": hcl_type(str(spec.get("type", "String"))),
            "sensitive": bool(spec.get("sensitive")),
        }
    return specs


def _hcl_value(value: Any) -> str:
    """A tfvars value. JSON is valid HCL for strings, numbers and lists once
    template sequences (``${``, ``%{``) are escaped."""
    text = json.dumps(value)
    return text.replace("${", "$${").replace("%{", "%%{")


def _parse_hcl_value(text: str) -> Any:
    return json.loads(text.replace("$${", "${").replace("%%{", "%{"))


def render_tfvars(values: Dict[str, Any]) -> str:
    lines = [ROOT_MARKER, "# Values for the variables declared in main.tf."]
    for name in sorted(values):
        lines.append(f"{name} = {_hcl_value(values[name])}")
    return "\n".join(lines) + "\n"


def parse_tfvars(text: str) -> Dict[str, Any]:
    """Read back a tfvars file :func:`render_tfvars` wrote (one value per line)."""
    out = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition(" = ")
        if not sep:
            raise IDPConfigurationError(f"cannot read terraform.tfvars line: {line}")
        out[name.strip()] = _parse_hcl_value(value.strip())
    return out


_ROLE_ARN = re.compile(r"^arn:[a-z0-9-]+:iam::\d{12}:role/[\w+=,.@/-]+$")


def check_assume_role_arn(role_arn: Optional[str]) -> Optional[str]:
    """``role_arn`` if it is an IAM role ARN (or empty), else refuse it."""
    if not role_arn:
        return None
    if not _ROLE_ARN.match(role_arn):
        raise IDPConfigurationError(
            f"--terraform-assume-role-arn {role_arn!r} is not an IAM role ARN "
            "(arn:<partition>:iam::<account>:role/<name>)"
        )
    return role_arn


def render_provider_blocks(region: str, assume_role_arn: Optional[str] = None) -> str:
    """The ``aws`` and ``awscc`` provider blocks, both in ``region``, and both
    assuming ``assume_role_arn`` when it is given. The two must agree: the module
    creates resources through both, and a role on one alone splits the
    deployment across two identities.

    The two providers spell it differently, and the wrong spelling fails
    ``validate`` with "Unsupported block type": in ``hashicorp/aws`` it is a
    nested block (``assume_role { role_arn = ... }``), in ``hashicorp/awscc`` an
    object attribute (``assume_role = { role_arn = ... }``).
    """
    lines: List[str] = []
    for name, opener in (("aws", "  assume_role {"), ("awscc", "  assume_role = {")):
        lines.append(f'provider "{name}" {{')
        lines.append(f"  region = {json.dumps(region)}")
        if assume_role_arn:
            lines.append(opener)
            lines.append(f"    role_arn = {json.dumps(assume_role_arn)}")
            lines.append("  }")
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


_PROVIDER_BLOCKS = re.compile(
    r'provider "aws" \{\n(?P<aws>.*?)\n\}\n\nprovider "awscc" \{\n.*?\n\}\n\n', re.S
)
_REGION_LINE = re.compile(r'^  region = ("(?:[^"\\]|\\.)*")$', re.M)


def set_assume_role(
    main_tf: Path, assume_role_arn: str, region: Optional[str] = None
) -> None:
    """Rewrite the provider blocks of an idp-cli ``main.tf`` to assume a role.

    For ``delete --terraform``, which has no parameters to regenerate the whole
    file from. The region the deploy recorded in the file is kept: the state's
    resources are in that region, and a delete pointed at another would destroy
    nothing there. ``region``, when given, must equal it, or this refuses.
    Refuses a ``main.tf`` idp-cli did not write, or one whose provider blocks are
    not in the shape :func:`render_main_tf` writes.
    """
    text = main_tf.read_text()
    if ROOT_MARKER not in text:
        raise IDPConfigurationError(
            f"{main_tf} was not written by idp-cli; add assume_role to its "
            "providers yourself"
        )
    match = _PROVIDER_BLOCKS.search(text)
    recorded = _REGION_LINE.search(match.group("aws")) if match else None
    if not match or not recorded:
        raise IDPConfigurationError(
            f"cannot find the aws and awscc provider blocks idp-cli writes in "
            f"{main_tf}; add assume_role to them yourself"
        )
    recorded_region = json.loads(recorded.group(1))
    if region and region != recorded_region:
        raise IDPConfigurationError(
            f"{main_tf} deploys to {recorded_region}, not {region}; run the delete "
            f"with --region {recorded_region}"
        )
    new = (
        text[: match.start()]
        + render_provider_blocks(recorded_region, assume_role_arn)
        + "\n"
        + text[match.end() :]
    )
    main_tf.write_text(new)


def render_main_tf(
    region: str,
    module_url: str,
    values: Dict[str, Any],
    specs: Dict[str, Dict[str, Any]],
    assume_role_arn: Optional[str] = None,
) -> str:
    """Root configuration: providers in the region and ``module "idp"``.

    Only variables with a value are declared and passed; every other module
    variable keeps its default. No backend block is written, so state is local
    in the directory unless the user adds a backend in another file. With
    ``assume_role_arn`` both providers assume that role, so Terraform creates the
    deployment's resources as the role rather than as the caller.
    """
    source = terraform_module_source(module_url)
    blocks = [
        ROOT_MARKER,
        "# Rewritten on every deploy; put your own configuration (a backend block,",
        "# for example) in another .tf file in this directory.",
        "",
        "terraform {",
        "  required_providers {",
        '    aws   = { source = "hashicorp/aws" }',
        '    awscc = { source = "hashicorp/awscc" }',
        "  }",
        "}",
        "",
        render_provider_blocks(region, assume_role_arn),
    ]
    names = sorted(values)
    for name in names:
        spec = specs.get(name, {"type": "string", "sensitive": False})
        blocks.append(f'variable "{name}" {{')
        blocks.append(f"  type = {spec['type']}")
        if spec.get("sensitive"):
            blocks.append("  sensitive = true")
        blocks.append("}")
        blocks.append("")
    blocks.append('module "idp" {')
    blocks.append(f"  source = {json.dumps(source)}")
    for name in names:
        blocks.append(f"  {name} = var.{name}")
    blocks.append("}")
    return "\n".join(blocks) + "\n"


def write_root_config(
    directory: Path,
    *,
    region: str,
    module_url: str,
    stack_name: str,
    admin_email: Optional[str],
    parameters: Dict[str, str],
    manifest: Dict[str, Any],
    assume_role_arn: Optional[str] = None,
) -> Dict[str, Any]:
    """Write ``main.tf`` and ``terraform.tfvars`` into ``directory``.

    ``assume_role_arn`` puts ``assume_role`` in both provider blocks.

    Refuses a ``main.tf`` that idp-cli did not write. On a later deploy the
    values from the previous ``terraform.tfvars`` are kept and the new ones
    applied over them, as a CloudFormation update keeps the parameters it is not
    given. Returns the variable values written.
    """
    directory.mkdir(parents=True, exist_ok=True)
    main_tf = directory / "main.tf"
    tfvars = directory / "terraform.tfvars"
    if main_tf.exists() and ROOT_MARKER not in main_tf.read_text():
        raise IDPConfigurationError(
            f"{main_tf} exists and was not written by idp-cli; refusing to "
            "overwrite it. Use another --terraform-dir, or remove the file."
        )
    specs = variable_specs(manifest)
    previous: Dict[str, Any] = {}
    if tfvars.exists():
        text = tfvars.read_text()
        if ROOT_MARKER not in text:
            raise IDPConfigurationError(
                f"{tfvars} exists and was not written by idp-cli; refusing to "
                "overwrite it."
            )
        previous_vars = parse_tfvars(text)
        # Earlier values are remembered by CloudFormation parameter name, which
        # is stable; a Terraform variable name can change between module
        # versions, and keying on it would drop the value without a word.
        remembered = directory / PARAMETERS_FILE
        if remembered.exists():
            by_name = json.loads(remembered.read_text())
            known = manifest.get("parameters") or {}
            previous = {"stack_name": previous_vars.get("stack_name")}
            for name, value in by_name.items():
                if name in known:
                    previous[known[name]["variable"]] = value
                else:
                    logger.warning(
                        "Parameter %s from the previous deploy is not in this "
                        "module and was dropped",
                        name,
                    )
        else:
            previous = {k: v for k, v in previous_vars.items() if k in specs}
            for k in sorted(set(previous_vars) - set(specs)):
                logger.warning(
                    "Variable %s from the previous deploy is not in this module "
                    "and was dropped",
                    k,
                )
    if previous.get("stack_name") not in (None, stack_name):
        raise IDPConfigurationError(
            f"{directory} holds the deployment {previous['stack_name']!r}, not "
            f"{stack_name!r}; use another --terraform-dir."
        )
    values = dict(previous)
    values.update(map_parameters(parameters, manifest))
    values["stack_name"] = stack_name
    if admin_email:
        admin = (manifest.get("parameters") or {}).get("AdminEmail")
        if admin:
            values[admin["variable"]] = admin_email
        else:
            # The headless module has no Cognito and so no AdminEmail, as with
            # the headless CloudFormation template.
            logger.warning("Ignoring the admin email: this module has no AdminEmail")
    missing = missing_required(values, manifest)
    if missing:
        hint = " (--admin-email)" if missing == ["AdminEmail"] else ""
        raise IDPConfigurationError(
            "required parameter(s) have no value: " + ", ".join(missing) + hint
        )
    main_tf.write_text(
        render_main_tf(
            region, module_url, values, specs, check_assume_role_arn(assume_role_arn)
        )
    )
    # The values can include NoEcho parameters.
    write_private(tfvars, render_tfvars(values))
    by_variable = {
        spec["variable"]: name
        for name, spec in (manifest.get("parameters") or {}).items()
    }
    write_private(
        directory / PARAMETERS_FILE,
        json.dumps(
            {by_variable[k]: v for k, v in values.items() if k in by_variable},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return values


# ---------------------------------------------------------------------------
# Init and the plan guard (deploy)
# ---------------------------------------------------------------------------


def needs_upgrade(directory: Path, module_url: str) -> bool:
    """Whether ``init`` should run with ``-upgrade``.

    Only when the module source differs from the one the directory was last
    initialised with (or none is recorded): a new module can raise a provider
    floor, which the existing lock file would refuse. Otherwise the providers
    stay at the versions in ``.terraform.lock.hcl`` rather than floating to the
    newest release inside the module's range on every deploy.
    """
    recorded = directory / MODULE_SOURCE_FILE
    if not recorded.is_file():
        return True
    return recorded.read_text().strip() != terraform_module_source(module_url)


def record_module_source(directory: Path, module_url: str) -> None:
    (directory / MODULE_SOURCE_FILE).write_text(
        terraform_module_source(module_url) + "\n"
    )


def retained_deletions(plan: Dict[str, Any], retained: Iterable[str]) -> List[str]:
    """Planned deletions (replacements included) of retained resources.

    ``plan`` is ``terraform show -json <planfile>`` output; ``retained`` are the
    module-relative addresses from ``idp-terraform.json``. Each entry is a plan
    address, with `` (replace)`` when the resource is deleted to be recreated.
    """
    wanted = {MODULE_PREFIX + a for a in retained}
    out = []
    for change in plan.get("resource_changes") or []:
        actions = (change.get("change") or {}).get("actions") or []
        address = change.get("address") or ""
        if "delete" not in actions or strip_instance_keys(address) not in wanted:
            continue
        out.append(address + (" (replace)" if "create" in actions else ""))
    return sorted(out)


ALLOW_REPLACE_RETAINED_FLAG = "--terraform-allow-replace-retained"


def refuse_retained_deletions(blocked: List[str], directory: Path) -> None:
    raise IDPStackError(
        f"The plan would delete {len(blocked)} resource(s) that CloudFormation "
        "retains (DeletionPolicy Retain), which can lose their data:\n"
        + "\n".join(f"  - {a}" for a in blocked)
        + f"\nNothing was applied. Review the change with `terraform plan` in "
        f"{directory}. To apply it anyway, re-run with "
        f"{ALLOW_REPLACE_RETAINED_FLAG} (SDK: allow_replace_retained=True)."
    )


# ---------------------------------------------------------------------------
# State (delete)
# ---------------------------------------------------------------------------

_INSTANCE_KEY = re.compile(r'\[(?:\d+|"(?:[^"\\]|\\.)*")\]')


def strip_instance_keys(address: str) -> str:
    return _INSTANCE_KEY.sub("", address)


def retained_state_addresses(
    retained: Iterable[str], state_list: Iterable[str]
) -> Tuple[List[str], List[str]]:
    """(state entries to remove, retained addresses not in state).

    ``retained`` are module-relative addresses without instance keys; each is
    prefixed with ``module.idp.`` and matched against ``terraform state list``
    entries with their instance keys stripped, so every instance is removed.
    """
    by_bare: Dict[str, List[str]] = {}
    for entry in state_list:
        by_bare.setdefault(strip_instance_keys(entry), []).append(entry)
    remove: List[str] = []
    absent: List[str] = []
    for addr in sorted(set(retained)):
        full = MODULE_PREFIX + addr
        if full in by_bare:
            remove.extend(by_bare[full])
        else:
            absent.append(full)
    return remove, absent


def _state_resources(state: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    def walk(module):
        yield from module.get("resources", []) or []
        for child in module.get("child_modules", []) or []:
            yield from walk(child)

    root = (state.get("values") or {}).get("root_module") or {}
    yield from walk(root)


_BUCKET_TYPES = {"aws_s3_bucket": "bucket", "awscc_s3_bucket": "bucket_name"}


def state_buckets(state: Dict[str, Any], exclude: Iterable[str] = ()) -> List[Dict]:
    """Buckets in ``terraform show -json`` output, as the ``bucket_info`` dicts
    :meth:`StackDeployer._empty_buckets` takes. Entries in ``exclude`` (state
    addresses) are left out."""
    skip = set(exclude)
    out = []
    for res in _state_resources(state):
        attr = _BUCKET_TYPES.get(res.get("type", ""))
        if res.get("mode") != "managed" or not attr or res.get("address") in skip:
            continue
        name = (res.get("values") or {}).get(attr)
        if name:
            out.append({"logical_id": res["address"], "bucket_name": name})
    return out


_PHYSICAL_ATTRS = (
    "bucket",
    "bucket_name",
    "name",
    "table_name",
    "log_group_name",
    "id",
)


def _category(tf_type: str) -> str:
    if "dynamodb_table" in tf_type:
        return "dynamodb_tables"
    if "s3_bucket" in tf_type and tf_type.endswith("bucket"):
        return "s3_buckets"
    if "log_group" in tf_type:
        return "log_groups"
    return "other"


def retained_report(state: Dict[str, Any], removed: Iterable[str]) -> Dict[str, List]:
    """The retained resources in the shape
    :meth:`StackDeployer.get_retained_resources_after_deletion` returns."""
    report: Dict[str, List] = {
        "dynamodb_tables": [],
        "log_groups": [],
        "s3_buckets": [],
        "other": [],
    }
    wanted = set(removed)
    for res in _state_resources(state):
        if res.get("address") not in wanted:
            continue
        if str(res.get("type", "")).startswith(("aws_s3_bucket_", "time_static")):
            # A retained bucket's settings (encryption, versioning, ...) are
            # separate Terraform resources kept with it; report the bucket once.
            continue
        vals = res.get("values") or {}
        physical = next((vals[a] for a in _PHYSICAL_ATTRS if vals.get(a)), None)
        report[_category(res.get("type", ""))].append(
            {
                "logical_id": res["address"],
                "physical_id": physical,
                "type": res.get("type"),
                "status": "RETAINED",
                "status_reason": "Removed from Terraform state before destroy",
                "stack": "terraform",
            }
        )
    return report


# ---------------------------------------------------------------------------
# The binary
# ---------------------------------------------------------------------------


class TerraformRunner:
    """Runs ``terraform`` or ``tofu`` in a root configuration directory.

    Interactive commands stream their output to stderr (stdout is reserved for
    machine-readable payloads); ``show -json`` and ``state list`` are captured.
    """

    def __init__(self, binary: str, directory: Path):
        path = shutil.which(binary)
        if not path:
            raise IDPConfigurationError(
                f"'{binary}' was not found on PATH. Install Terraform "
                "(https://developer.hashicorp.com/terraform/install) or OpenTofu, "
                "or name the binary with --terraform-bin."
            )
        self.binary = path
        self.directory = Path(directory)

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env.setdefault("TF_IN_AUTOMATION", "1")
        return env

    def run(self, *args: str) -> None:
        cmd = [self.binary, *args]
        logger.info("Running %s in %s", " ".join(cmd), self.directory)
        proc = subprocess.run(
            cmd, cwd=self.directory, env=self._env(), stdout=sys.stderr, check=False
        )
        if proc.returncode != 0:
            raise IDPStackError(
                f"`{Path(self.binary).name} {' '.join(args)}` failed with exit "
                f"code {proc.returncode}"
            )

    def capture(self, *args: str) -> str:
        cmd = [self.binary, *args]
        proc = subprocess.run(
            cmd,
            cwd=self.directory,
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise IDPStackError(
                f"`{Path(self.binary).name} {' '.join(args)}` failed: "
                f"{(proc.stderr or '').strip()}"
            )
        return proc.stdout or ""

    def init(self, upgrade: bool = False) -> None:
        # -upgrade only when asked (see needs_upgrade): a newer module can raise
        # a provider floor, which the existing lock file would refuse.
        self.run("init", "-input=false", *(["-upgrade"] if upgrade else []))

    def plan(self, plan_file: str) -> None:
        self.run("plan", "-input=false", f"-out={plan_file}")

    def show_plan(self, plan_file: str) -> Dict[str, Any]:
        return json.loads(self.capture("show", "-json", plan_file))

    def apply_plan(self, plan_file: str) -> None:
        # A saved plan applies without a prompt, and exactly as it was checked.
        self.run("apply", "-input=false", plan_file)

    def destroy(self) -> None:
        self.run("destroy", "-auto-approve", "-input=false")

    def state_list(self) -> List[str]:
        text = self.capture("state", "list")
        return [line.strip() for line in text.splitlines() if line.strip()]

    def show_state(self) -> Dict[str, Any]:
        text = self.capture("show", "-json")
        return json.loads(text) if text.strip() else {}

    def state_rm(self, addresses: List[str]) -> None:
        if addresses:
            self.run("state", "rm", *addresses)


def read_manifest_from_dir(directory: Path) -> Dict[str, Any]:
    """The manifest of the module ``init`` installed in ``directory``."""
    modules_json = directory / ".terraform" / "modules" / "modules.json"
    if modules_json.is_file():
        for mod in json.loads(modules_json.read_text()).get("Modules", []):
            if mod.get("Key") == "idp":
                path = directory / mod["Dir"] / MANIFEST_NAME
                if path.is_file():
                    return json.loads(path.read_text())
    raise IDPConfigurationError(
        f"cannot find the idp module's {MANIFEST_NAME} under {directory}/.terraform; "
        "is this a directory `idp-cli deploy --terraform` wrote and initialised?"
    )
