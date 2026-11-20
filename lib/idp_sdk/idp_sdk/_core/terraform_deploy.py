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
plan, refuses it if it would delete or replace a resource CloudFormation
retains or one that holds data (unless told otherwise), and applies exactly
that plan; ``idp-cli delete --terraform`` empties the
deployment's buckets, removes the resources CloudFormation would have retained
from the state, and runs ``destroy``. The module's ``idp-terraform.json`` (written
by the translator, at the root of the zip) supplies the CloudFormation-parameter
to Terraform-variable map, the retained addresses and the stateful ones.

The pure functions here (URL and key construction, the zip, parameter mapping,
the root configuration, the retained-address selection) carry the logic and are
what the unit tests exercise; :class:`TerraformRunner` is the thin subprocess
wrapper around the ``terraform`` or ``tofu`` binary.
"""

from __future__ import annotations

import hashlib
import importlib.util
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


#: Modules the translator imports that are not part of the SDK's own
#: dependencies. They come from the SDK's ``terraform`` extra.
TRANSLATOR_MODULES = ("samtranslator", "yaml")


def translator_command(
    source_dir: str,
    template_path: str,
    out_dir: str,
    nested_cache: str,
    version: str,
    previous_manifest: Optional[str] = None,
) -> Tuple[List[str], Dict[str, str]]:
    """The translator invocation and its environment.

    The published template is translated online: nested templates are fetched
    by ``TemplateURL`` into ``nested_cache``. The same template labels the
    deployment, at ``version``. ``previous_manifest`` is the path of the
    previous release's ``idp-terraform.json``, from which the translator writes
    ``moved`` blocks for resources whose address changed. The checkout's
    ``template.yaml`` supplies the scanner suppressions carried into the module.
    """
    translator = Path(source_dir) / "terraform" / "translator"
    if not (translator / "idp_tf" / "cli.py").is_file():
        raise IDPConfigurationError(
            f"Terraform translator not found at {translator}; --terraform needs a "
            "source checkout that contains terraform/translator"
        )
    missing = [m for m in TRANSLATOR_MODULES if importlib.util.find_spec(m) is None]
    if missing:
        raise IDPConfigurationError(
            f"The Terraform translator needs {', '.join(missing)}, which this "
            "Python environment does not have. Install the SDK's terraform extra "
            '(pip install -e "lib/idp_sdk[terraform]", which `make setup` does), '
            "or publish without --terraform."
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
    if previous_manifest:
        cmd += ["--previous-manifest", previous_manifest]
    # The published template has lost its comments, so the scanner suppressions
    # the module carries are read from the source template it was packaged from.
    source_template = Path(source_dir) / "template.yaml"
    if source_template.is_file():
        cmd += ["--suppressions-source", str(source_template)]
    return cmd, env


def previous_release_manifest(
    s3, bucket: str, prefix: str, variant: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The ``idp-terraform.json`` of the module the bucket's latest pointer names.

    Publishing reads it before uploading the new module, so it is the previous
    release's. ``None`` when there is no pointer yet (the first publish of a
    module to this bucket and prefix). Any other failure raises: translating
    without it would silently omit the ``moved`` blocks an upgrade needs.
    """
    key = pointer_key(prefix, variant)
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as e:  # noqa: BLE001 - only an absent pointer is expected
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            logger.info("no previous Terraform module at s3://%s/%s", bucket, key)
            return None
        raise IDPStackError(
            f"cannot read the previous Terraform pointer s3://{bucket}/{key}: {e}"
        ) from e
    module_url, sha = resolve_pointer(json.loads(body))
    return load_module_manifest(module_url, sha, lambda region: s3)


def translate_published_template(
    source_dir: str,
    template_path: str,
    work_dir: str,
    version: str,
    previous_manifest: Optional[Dict[str, Any]] = None,
) -> Path:
    """Translate the published main template into ``<work_dir>/module``.

    ``previous_manifest`` (the previous release's ``idp-terraform.json``) makes
    the translator generate ``moved`` blocks for addresses that changed."""
    work = Path(work_dir)
    out = work / "module"
    cache = work / "nested-cache"
    for d in (out, cache):
        if d.exists():
            shutil.rmtree(d)
    work.mkdir(parents=True, exist_ok=True)
    previous_path = None
    if previous_manifest is not None:
        previous_path = work / "previous-idp-terraform.json"
        previous_path.write_text(json.dumps(previous_manifest))
    cmd, env = translator_command(
        source_dir,
        template_path,
        str(out),
        str(cache),
        version,
        str(previous_path) if previous_path else None,
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


def _aligned(pairs: List[Tuple[str, str]], indent: str = "") -> List[str]:
    """``name = value`` lines with the ``=`` padded to one column.

    ``terraform fmt`` aligns the ``=`` of consecutive single-line attributes, so
    a file written this way passes ``fmt -check`` in a repository that gates on
    it. Every caller passes values that fit on one line.
    """
    width = max((len(name) for name, _ in pairs), default=0)
    return [f"{indent}{name.ljust(width)} = {value}" for name, value in pairs]


def render_tfvars(values: Dict[str, Any]) -> str:
    lines = [ROOT_MARKER, "# Values for the variables declared in main.tf."]
    lines += _aligned([(name, _hcl_value(values[name])) for name in sorted(values)])
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


#: The module variable ``--tags`` sets, and the root variable of the same name.
TAGS_VARIABLE = "tags"
#: ``default_tags`` reaches every ``aws`` resource, the module's S3 objects among
#: them, and S3 allows an object at most 10 tags (a stack allows 50).
MAX_DEPLOYMENT_TAGS = 10


def render_provider_blocks(
    region: str, assume_role_arn: Optional[str] = None, default_tags: bool = False
) -> str:
    """The ``aws`` and ``awscc`` provider blocks, both in ``region``, and both
    assuming ``assume_role_arn`` when it is given. The two must agree: the module
    creates resources through both, and a role on one alone splits the
    deployment across two identities.

    With ``default_tags`` the ``aws`` block tags every resource it creates with
    ``var.tags``. ``awscc`` has no ``default_tags``; the module merges the same
    variable into each of its ``awscc`` resources instead.

    The two providers spell it differently, and the wrong spelling fails
    ``validate`` with "Unsupported block type": in ``hashicorp/aws`` it is a
    nested block (``assume_role { role_arn = ... }``), in ``hashicorp/awscc`` an
    object attribute (``assume_role = { role_arn = ... }``).
    """
    lines: List[str] = []
    for name in ("aws", "awscc"):
        lines.append(f'provider "{name}" {{')
        if assume_role_arn and name == "awscc":
            # Two consecutive attributes, so `terraform fmt` aligns their `=`.
            lines += _aligned(
                [("region", json.dumps(region)), ("assume_role", "{")], "  "
            )
        else:
            lines.append(f"  region = {json.dumps(region)}")
            if assume_role_arn:
                lines.append("  assume_role {")
        if assume_role_arn:
            lines.append(f"    role_arn = {json.dumps(assume_role_arn)}")
            lines.append("  }")
        if default_tags and name == "aws":
            lines.append("  default_tags {")
            lines.append(f"    tags = var.{TAGS_VARIABLE}")
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
        + render_provider_blocks(
            recorded_region,
            assume_role_arn,
            default_tags="  default_tags {" in match.group("aws"),
        )
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
    tags: Optional[Dict[str, str]] = None,
) -> str:
    """Root configuration: providers in the region and ``module "idp"``.

    Only variables with a value are declared and passed; every other module
    variable keeps its default. No backend block is written, so state is local
    in the directory unless the user adds a backend in another file. With
    ``assume_role_arn`` both providers assume that role, so Terraform creates the
    deployment's resources as the role rather than as the caller.

    With ``tags`` (non-empty), a ``tags`` variable is declared and given to the
    module, which merges it into its ``awscc`` resources, and to the ``aws``
    provider's ``default_tags``, which covers the rest. Without, the file is
    what it was before the module had a tags input.
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
        render_provider_blocks(region, assume_role_arn, default_tags=bool(tags)),
    ]
    names = sorted(values)
    if tags:
        specs = {
            **specs,
            TAGS_VARIABLE: {"type": "map(string)", "sensitive": False},
        }
        names = sorted([*names, TAGS_VARIABLE])
    for name in names:
        spec = specs.get(name, {"type": "string", "sensitive": False})
        attributes = [("type", str(spec["type"]))]
        if spec.get("sensitive"):
            attributes.append(("sensitive", "true"))
        blocks.append(f'variable "{name}" {{')
        blocks += _aligned(attributes, "  ")
        blocks.append("}")
        blocks.append("")
    blocks.append('module "idp" {')
    blocks += _aligned(
        [("source", json.dumps(source))] + [(name, f"var.{name}") for name in names],
        "  ",
    )
    blocks.append("}")
    return "\n".join(blocks) + "\n"


#: Where a kept value came from, as the upgrade output names it.
SOURCE_LABEL_STACK = "label stack"
SOURCE_PARAMETERS_FILE = PARAMETERS_FILE
SOURCE_TFVARS = "terraform.tfvars"


class RootConfigValues(dict):
    """The Terraform variable values :func:`write_root_config` wrote.

    A plain ``dict`` of variable name to value (what the function always
    returned), carrying how each parameter that was not passed got its value:

    - ``kept``: CloudFormation name -> value kept from the deployment's record,
      for every parameter not passed on this deploy;
    - ``kept_from``: CloudFormation name -> where that value was read
      (:data:`SOURCE_LABEL_STACK`, :data:`SOURCE_PARAMETERS_FILE` or
      :data:`SOURCE_TFVARS`);
    - ``recorded``: the CloudFormation names the label stack recorded, or
      ``None`` when no label stack was read;
    - ``upgrade``: whether an earlier deploy was found (a label stack or this
      directory's own record);
    - ``local``: whether this directory held a record of an earlier deploy;
    - ``tags``: the deployment's tags as written (empty for none), and
      ``tags_kept``: whether they are the previous deploy's, kept because none
      were passed.
    """

    tags: Dict[str, str]
    tags_kept: bool
    kept: Dict[str, Any]
    kept_from: Dict[str, str]
    recorded: Optional[frozenset]
    upgrade: bool
    local: bool


def label_parameter_values(stack: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """The deployment's effective parameter values its label stack records.

    ``stack`` is a ``DescribeStacks`` entry for the deployment's label stack.
    Terraform writes the label stack's parameters at every apply from the
    module's variables, so they are the value in effect of every parameter,
    passed or defaulted, by CloudFormation name; ``NoEcho`` parameters are not
    among them. ``None`` when there is no stack or it carries no parameters.
    """
    if not stack or "Parameters" not in stack:
        return None
    return {
        p["ParameterKey"]: p.get("ParameterValue", "")
        for p in stack.get("Parameters") or []
        if "ParameterKey" in p
    }


def _refuse_changed_creation_fixed(
    parameters: Dict[str, str],
    effective: Dict[str, Any],
    known: Dict[str, Any],
) -> None:
    """Refuse a ``CustomerManagedKmsKeyArn`` whose passed value differs from
    the value the deployment has (empty when it has none), as the
    CloudFormation deploy does (``guard_creation_fixed_parameters``).

    Terraform has no rollback, so this is what keeps a changed key from being
    applied part-way before the template's check refuses it. It is the only
    creation-fixed parameter checked here: ``ExternalIdPEmailMutable`` is
    derived by ``terraform_migrate`` from the user pool a migration adopts, so
    the value a deployment "was created with" is not the module default there.
    """
    from idp_sdk._core.stack import guard_creation_fixed_parameters

    given: Dict[str, str] = {}
    current: Dict[str, str] = {}
    for name in ("CustomerManagedKmsKeyArn",):
        spec = known.get(name)
        if spec is None or name not in parameters:
            continue
        given[name] = str(parameters[name])
        value = effective.get(spec["variable"])
        if value is not None:
            current[name] = (
                ("true" if value else "false")
                if isinstance(value, bool)
                else str(value)
            )
    try:
        guard_creation_fixed_parameters(
            given, stack_exists=True, current_params=current
        )
    except ValueError as e:
        raise IDPConfigurationError(str(e)) from e


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
    recorded: Optional[Dict[str, str]] = None,
    tags: Optional[Dict[str, str]] = None,
) -> RootConfigValues:
    """Write ``main.tf`` and ``terraform.tfvars`` into ``directory``.

    ``assume_role_arn`` puts ``assume_role`` in both provider blocks.

    ``tags`` replaces the deployment's tags, as a CloudFormation update given
    tags replaces the stack's. An empty dict stops passing any: the ``aws``
    resources lose them, but an ``awscc`` resource whose template sets no tags
    is given ``null``, which the provider may read as "keep what is there", so
    those may keep the tags they have. ``None`` keeps the tags this directory's
    ``terraform.tfvars`` holds from the previous deploy, as a CloudFormation
    update given none keeps the stack's. Refuses tags for a module that has no
    tags input (its manifest has no ``tagsVariable``), and more than
    :data:`MAX_DEPLOYMENT_TAGS`.

    Refuses a ``main.tf`` that idp-cli did not write. A parameter that is not
    passed keeps the value the deployment has, as a CloudFormation update keeps
    the previous value of every parameter it is not given. The value is taken,
    in order of precedence, from:

    1. ``parameters`` (and ``admin_email``), the values passed on this deploy;
    2. ``recorded``, the deployment's effective values by CloudFormation name as
       its label stack records them (see :func:`label_parameter_values`). The
       label stack lives with the deployment, so a second operator deploying
       from a fresh directory against the same state reads the same values, and
       it holds every parameter's value in effect, defaulted ones included, for
       deployments made before idp-cli recorded them;
    3. this directory's ``idp-cli-parameters.json`` (by CloudFormation name,
       which is stable where a Terraform variable name may change), or without
       that file the previous ``terraform.tfvars``. These are the only source
       for ``NoEcho`` parameters, which the label stack leaves out.

    A parameter none of these holds takes the module's default: a parameter new
    in this module, or every parameter of a deployment whose record cannot be
    read (no label stack, and a directory holding only the values passed, as
    idp-cli wrote before it recorded the effective ones). :func:`parameter_report`
    describes the outcome, and :func:`record_effective_parameters` writes every
    value in effect into ``idp-cli-parameters.json`` once the deploy has applied.

    Returns the variable values written, as a :class:`RootConfigValues`.
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
    known = manifest.get("parameters") or {}
    previous: Dict[str, Any] = {}
    previous_tags: Dict[str, str] = {}
    local_source = SOURCE_TFVARS
    if tfvars.exists():
        text = tfvars.read_text()
        if ROOT_MARKER not in text:
            raise IDPConfigurationError(
                f"{tfvars} exists and was not written by idp-cli; refusing to "
                "overwrite it."
            )
        previous_vars = parse_tfvars(text)
        previous_tags = previous_vars.pop(TAGS_VARIABLE, None) or {}
        # Earlier values are remembered by CloudFormation parameter name, which
        # is stable; a Terraform variable name can change between module
        # versions, and keying on it would drop the value without a word.
        remembered = directory / PARAMETERS_FILE
        if remembered.exists():
            local_source = SOURCE_PARAMETERS_FILE
            by_name = json.loads(remembered.read_text())
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
    from_label: Dict[str, Any] = {}
    for name, text_value in sorted((recorded or {}).items()):
        spec = known.get(name)
        if spec is None:
            logger.info(
                "Parameter %s on the label stack is not in this module and was dropped",
                name,
            )
            continue
        try:
            value = _convert(name, str(spec.get("type", "String")), text_value)
        except IDPConfigurationError as e:
            logger.warning("Ignoring the label stack's value of %s: %s", name, e)
            continue
        variable = spec["variable"]
        if variable in previous and previous[variable] != value:
            logger.info(
                "%s is %r on the label stack and %r in %s; keeping the label "
                "stack's, the value the deployment last applied",
                name,
                text_value if not spec.get("sensitive") else "(sensitive)",
                previous[variable] if not spec.get("sensitive") else "(sensitive)",
                local_source,
            )
        from_label[variable] = value
    values = RootConfigValues(previous)
    values.update(from_label)
    passed = map_parameters(parameters, manifest)
    if recorded is not None or tfvars.exists():
        _refuse_changed_creation_fixed(parameters, values, known)
    values.update(passed)
    values["stack_name"] = stack_name
    passed_names = set(parameters)
    if admin_email:
        admin = known.get("AdminEmail")
        if admin:
            values[admin["variable"]] = admin_email
            passed_names.add("AdminEmail")
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
    values.kept = {}
    values.kept_from = {}
    for name, spec in known.items():
        variable = spec["variable"]
        if name in passed_names or variable not in values:
            continue
        values.kept[name] = values[variable]
        values.kept_from[name] = (
            SOURCE_LABEL_STACK if variable in from_label else local_source
        )
    values.recorded = frozenset(recorded) if recorded is not None else None
    values.local = tfvars.exists()
    values.upgrade = recorded is not None or values.local
    values.tags_kept = tags is None and bool(previous_tags)
    values.tags = dict(previous_tags if tags is None else tags)
    if values.tags and manifest.get("tagsVariable") != TAGS_VARIABLE:
        where = (
            "this directory's previous deploy has tags"
            if values.tags_kept
            else "tags were passed"
        )
        raise IDPConfigurationError(
            f"{where}, and this module has no tags input (it was published "
            "before the Terraform module accepted tags). Use a newer module, or "
            f"deploy without tags: remove the tags line from {tfvars}, or pass "
            "tags={} to the SDK."
        )
    if len(values.tags) > MAX_DEPLOYMENT_TAGS:
        raise IDPConfigurationError(
            f"{len(values.tags)} tags given; a Terraform deployment takes at most "
            f"{MAX_DEPLOYMENT_TAGS}, because the aws provider's default_tags also "
            "tag the module's S3 objects, which S3 limits to 10 tags each"
        )
    if values.tags_kept:
        logger.info(
            "Keeping the previous deploy's tags (%s); pass tags to replace them",
            ", ".join(sorted(values.tags)),
        )
    main_tf.write_text(
        render_main_tf(
            region,
            module_url,
            values,
            specs,
            check_assume_role_arn(assume_role_arn),
            tags=values.tags,
        )
    )
    # The values can include NoEcho parameters.
    tfvars_values = dict(values)
    if values.tags:
        tfvars_values[TAGS_VARIABLE] = values.tags
    write_private(tfvars, render_tfvars(tfvars_values))
    _write_parameters_file(directory, manifest, values)
    return values


def _write_parameters_file(
    directory: Path, manifest: Dict[str, Any], values: Dict[str, Any]
) -> None:
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


_VARIABLE_HEADER = re.compile(r'^variable "([^"]+)" \{$')
_DEFAULT_LINE = re.compile(r"^  default = (.*)$")


def module_variable_defaults(variables_tf: str) -> Dict[str, Any]:
    """Terraform variable name -> default, from a ``variables.tf`` the
    translator wrote.

    Reads the shape ``Translator._variables`` writes: a top-level
    ``variable "<name>" {`` block whose ``default = <value>`` line is indented
    two spaces, the value a number, a quoted string or a list of quoted strings
    (on one line, or one item per line closed by ``]``). A variable without a
    default, or with one in any other shape, is left out, so a caller treats its
    default as unknown rather than reading a wrong one.
    """
    out: Dict[str, Any] = {}
    lines = variables_tf.splitlines()
    i = 0
    current: Optional[str] = None
    while i < len(lines):
        line = lines[i]
        header = _VARIABLE_HEADER.match(line)
        if header:
            current = header.group(1)
        elif line == "}":
            current = None
        elif current is not None:
            default = _DEFAULT_LINE.match(line)
            if default:
                text = default.group(1)
                if text == "[":
                    items = []
                    i += 1
                    while i < len(lines) and lines[i] != "  ]":
                        items.append(lines[i].strip().rstrip(","))
                        i += 1
                    text = "[" + ", ".join(items) + "]"
                try:
                    out[current] = _parse_hcl_value(text)
                except ValueError:
                    pass
        i += 1
    return out


def read_module_defaults_from_dir(directory: Path) -> Optional[Dict[str, Any]]:
    """The defaults of the ``idp`` module ``init`` installed in ``directory``,
    or ``None`` when no module is installed there."""
    modules_json = directory / ".terraform" / "modules" / "modules.json"
    if not modules_json.is_file():
        return None
    try:
        modules = json.loads(modules_json.read_text()).get("Modules", [])
    except ValueError:
        return None
    for mod in modules:
        if mod.get("Key") == "idp":
            path = directory / mod["Dir"] / "variables.tf"
            if path.is_file():
                return module_variable_defaults(path.read_text())
    return None


#: A default :func:`parameter_report` could not read.
_UNKNOWN = object()


def _shown(spec: Dict[str, Any], value: Any) -> str:
    if spec.get("sensitive"):
        return "(sensitive)"
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


def parameter_report(
    values: RootConfigValues,
    manifest: Dict[str, Any],
    new_defaults: Optional[Dict[str, Any]],
    old_defaults: Optional[Dict[str, Any]] = None,
) -> List[Tuple[int, str]]:
    """``(logging level, line)`` pairs describing what an upgrade kept.

    ``values`` is what :func:`write_root_config` returned; ``new_defaults`` and
    ``old_defaults`` are :func:`read_module_defaults_from_dir` for the module
    being deployed and the one installed before (``None`` when unknown). Lists:

    - the values kept that differ from the new module's default (all kept
      values when its defaults are unknown), and how many others were kept;
    - the parameters that take the new module's default because no record of
      the deployment holds them: those new in this module, and, when the
      deployment's effective values could not be read (no label stack), a
      warning naming every parameter whose default changed between the two
      modules, which this deploy therefore changes.

    Empty for a first deploy.
    """
    if not values.upgrade:
        return []
    known = manifest.get("parameters") or {}
    out: List[Tuple[int, str]] = []
    shown_kept = []
    same = 0
    for name in sorted(values.kept):
        spec = known[name]
        value = values.kept[name]
        default = (new_defaults or {}).get(spec["variable"], _UNKNOWN)
        if new_defaults is not None and default == value:
            same += 1
            continue
        line = f"  {name} = {_shown(spec, value)} (from the {values.kept_from[name]}"
        if default is not _UNKNOWN:
            line += f"; this module's default is {_shown(spec, default)}"
        shown_kept.append(line + ")")
    if shown_kept or same:
        header = (
            f"Kept {len(values.kept)} parameter value(s) the deployment already "
            "had and this deploy did not pass"
        )
        if new_defaults is not None:
            header += (
                f"; {same} equal this module's default, the others are:"
                if shown_kept
                else f"; all {same} equal this module's default."
            )
        else:
            header += ":"
        out.append((logging.INFO, header))
        out.extend((logging.INFO, line) for line in shown_kept)

    unset = {
        name: spec
        for name, spec in sorted(known.items())
        if spec["variable"] not in values
    }

    def is_new(name: str, spec: Dict[str, Any]) -> bool:
        if old_defaults is not None and spec["variable"] in old_defaults:
            return False
        if values.recorded is not None:
            return name not in values.recorded and not spec.get("sensitive")
        return old_defaults is not None

    new = []
    for name, spec in unset.items():
        if is_new(name, spec):
            default = (new_defaults or {}).get(spec["variable"], "?")
            new.append(f"{name} = {_shown(spec, default)}")
    if new:
        out.append(
            (
                logging.INFO,
                "New parameter(s) in this module, taking its default: "
                + ", ".join(new),
            )
        )
    if values.recorded is None:
        changed = []
        if old_defaults is not None and new_defaults is not None:
            for name, spec in unset.items():
                var = spec["variable"]
                if (
                    var in old_defaults
                    and var in new_defaults
                    and old_defaults[var] != new_defaults[var]
                ):
                    changed.append(
                        f"  {name}: {_shown(spec, old_defaults[var])} -> "
                        f"{_shown(spec, new_defaults[var])}"
                    )
        if changed:
            out.append(
                (
                    logging.WARNING,
                    "The deployment's settings could not be read from its label "
                    f"stack, and {PARAMETERS_FILE} records only the parameters "
                    "passed to earlier deploys, so the parameters below, never "
                    "passed, take this module's default, which differs from the "
                    "default they were deployed with. Pass the earlier value with "
                    "--parameters to keep it:",
                )
            )
            out.extend((logging.WARNING, line) for line in changed)
        elif old_defaults is None and unset:
            out.append(
                (
                    logging.WARNING,
                    "No record of this deployment's settings was found for the "
                    "parameters below: the label stack could not be read, this "
                    "directory does not record them, and the defaults they were "
                    "deployed with are unknown (no module is installed here). They "
                    "take this module's default; pass any the deployment set "
                    "differently with --parameters: " + ", ".join(unset),
                )
            )
    # The label stack leaves NoEcho values out, so a directory with no record
    # of its own (a second operator's) cannot know them.
    sensitive_unset = [name for name, spec in unset.items() if spec.get("sensitive")]
    if sensitive_unset and values.recorded is not None and not values.local:
        out.append(
            (
                logging.WARNING,
                "NoEcho parameter(s) are not recorded on the label stack and this "
                "directory holds no value for them, so they take this module's "
                "default; pass them again if the deployment set them: "
                + ", ".join(sensitive_unset),
            )
        )
    return out


def effective_parameters(
    values: Dict[str, Any],
    manifest: Dict[str, Any],
    defaults: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Every parameter's value in effect, by CloudFormation name: the value
    written, or the module's default for a parameter with none."""
    out = {}
    for name, spec in (manifest.get("parameters") or {}).items():
        variable = spec["variable"]
        if variable in values:
            out[name] = values[variable]
        elif defaults is not None and variable in defaults:
            out[name] = defaults[variable]
    return out


def record_effective_parameters(
    directory: Path,
    manifest: Dict[str, Any],
    values: Dict[str, Any],
    defaults: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Write every parameter's value in effect into ``idp-cli-parameters.json``,
    defaulted ones included, so the next deploy from this directory keeps them
    even where no label stack can be read. Returns what was written."""
    by_name = effective_parameters(values, manifest, defaults)
    write_private(
        directory / PARAMETERS_FILE,
        json.dumps(by_name, indent=2, sort_keys=True) + "\n",
    )
    return by_name


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


def protected_addresses(*manifests: Dict[str, Any]) -> Dict[str, str]:
    """``{module-relative address: why}`` a plan may not delete or replace.

    The union, over the installed and the new module's ``idp-terraform.json``,
    of ``retainedAddresses`` (CloudFormation ``DeletionPolicy: Retain``) and
    ``statefulAddresses`` (every resource of a type whose replacement loses
    data, computed by the translator from its ``STATEFUL_TYPES`` list). A
    version-1 manifest has no ``statefulAddresses``; its retained ones still
    count.
    """
    out: Dict[str, str] = {}
    for manifest in manifests:
        for address in manifest.get("statefulAddresses") or []:
            out.setdefault(address, "stateful")
    for manifest in manifests:
        for address in manifest.get("retainedAddresses") or []:
            out[address] = "retained"
    return out


def protected_deletions(plan: Dict[str, Any], protected: Iterable[str]) -> List[str]:
    """Planned deletions (replacements included) of protected resources.

    ``plan`` is ``terraform show -json <planfile>`` output; ``protected`` are
    module-relative addresses from ``idp-terraform.json``. Each entry is a plan
    address, with `` (replace)`` when the resource is deleted to be recreated.
    A resource whose address changed without a ``moved`` block shows here as a
    deletion of the old address, which is the case this exists for.
    """
    wanted = {MODULE_PREFIX + a for a in protected}
    out = []
    for change in plan.get("resource_changes") or []:
        actions = (change.get("change") or {}).get("actions") or []
        address = change.get("address") or ""
        if "delete" not in actions or strip_instance_keys(address) not in wanted:
            continue
        out.append(address + (" (replace)" if "create" in actions else ""))
    return sorted(out)


#: Kept for callers of the retained-only guard; the deploy guard is wider now.
retained_deletions = protected_deletions

ALLOW_REPLACE_STATEFUL_FLAG = "--terraform-allow-replace-stateful"
#: The flag's earlier name, still accepted.
ALLOW_REPLACE_RETAINED_FLAG = "--terraform-allow-replace-retained"


def refuse_protected_deletions(
    blocked: List[str], directory: Path, why: Optional[Dict[str, str]] = None
) -> None:
    why = why or {}

    def reason(address: str) -> str:
        bare = strip_instance_keys(address.removesuffix(" (replace)"))
        kind = why.get(bare.removeprefix(MODULE_PREFIX))
        return {
            "retained": " (CloudFormation retains it)",
            "stateful": " (holds data)",
        }.get(kind or "", "")

    raise IDPStackError(
        f"The plan would delete or replace {len(blocked)} resource(s) that hold "
        "data or that CloudFormation retains (DeletionPolicy Retain), which can "
        "lose their data:\n"
        + "\n".join(f"  - {a}{reason(a)}" for a in blocked)
        + f"\nNothing was applied. Review the change with `terraform plan` in "
        f"{directory}. A resource whose address changed needs a `moved` block "
        "(the module generates them from terraform/translator/renames.yaml). To "
        f"apply it anyway, re-run with {ALLOW_REPLACE_STATEFUL_FLAG} (SDK: "
        "allow_replace_stateful=True)."
    )


#: Kept for callers of the retained-only guard.
refuse_retained_deletions = refuse_protected_deletions


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
    :meth:`StackDeployer.get_retained_resources_after_deletion` returns, plus
    ``service_log_groups``: the log groups AWS services created for resources
    this delete destroys, which are left behind (see :func:`service_log_groups`).
    An entry whose identifier is not a readable name carries a ``display_name``.
    """
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
        entry = {
            "logical_id": res["address"],
            "physical_id": physical,
            "type": res.get("type"),
            "status": "RETAINED",
            "status_reason": "Removed from Terraform state before destroy",
            "stack": "terraform",
        }
        describe = _DISPLAY_NAMES.get(str(res.get("type", "")))
        if describe:
            entry["display_name"] = describe(vals)
        report[_category(res.get("type", ""))].append(entry)
    report["service_log_groups"] = service_log_groups(state, wanted)
    return report


def _api_gateway_account(vals: Dict[str, Any]) -> str:
    role = vals.get("cloudwatch_role_arn")
    text = "API Gateway account settings for this Region"
    return f"{text} (CloudWatch Logs role {role})" if role else text


def _kms_key(vals: Dict[str, Any]) -> str:
    key = vals.get("key_id") or vals.get("id")
    return f"KMS key {key}, which encrypts the retained buckets"


#: Resources whose only identifier is not a name a reader can act on. The
#: account-level API Gateway setting's id is the provider's or Cloud Control's
#: identifier, which says nothing about what was kept; a KMS key's id says
#: nothing about why it was kept.
_DISPLAY_NAMES = {
    "aws_api_gateway_account": _api_gateway_account,
    "awscc_apigateway_account": _api_gateway_account,
    "aws_kms_key": _kms_key,
    "awscc_kms_key": _kms_key,
}


def _block(value: Any) -> Dict[str, Any]:
    """A nested block from ``terraform show -json``: the aws provider renders one
    as a list of objects, awscc's single-nested attributes as an object."""
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, dict) else {}


def _log_group_names(state: Dict[str, Any]) -> set:
    names = set()
    for res in _state_resources(state):
        if res.get("mode", "managed") != "managed":
            continue
        if "log_group" not in str(res.get("type", "")):
            continue
        vals = res.get("values") or {}
        name = vals.get("name") or vals.get("log_group_name")
        if name:
            names.add(name)
    return names


def _glue_crawler_log_group(
    vals: Dict[str, Any], security_configs: Dict[str, Dict[str, Any]]
) -> tuple:
    """(log group, shared) for a Glue crawler.

    A crawler writes to ``/aws-glue/crawlers``, which every crawler in the
    Region shares, unless its security configuration encrypts CloudWatch Logs;
    then Glue writes to ``/aws-glue/crawlers-role/<role name>-<configuration>``
    (AWS Glue Developer Guide, "Encrypting logs written to CloudWatch Logs").
    """
    config_name = vals.get("crawler_security_configuration") or vals.get(
        "security_configuration"
    )
    config = security_configs.get(config_name or "") or {}
    encryption = _block(config.get("encryption_configuration"))
    mode = _block(encryption.get("cloudwatch_encryption")).get(
        "cloudwatch_encryption_mode"
    )
    role = str(vals.get("role") or "").rsplit("/", 1)[-1]
    if mode and str(mode).upper() != "DISABLED" and role:
        return f"/aws-glue/crawlers-role/{role}-{config_name}", False
    return "/aws-glue/crawlers", True


def service_log_groups(
    state: Dict[str, Any], exclude: Iterable[str] = ()
) -> List[Dict[str, Any]]:
    """Log groups AWS services create on the deployment's behalf, which neither
    ``destroy`` nor a CloudFormation stack delete removes.

    Derived from the state: a Lambda function logging to its default
    ``/aws/lambda/<function>`` group, a CodeBuild project logging to its default
    ``/aws/codebuild/<project>`` group, and a Glue crawler, when no log group of
    that name is in the state. The service creates the group the first time the
    resource runs, so a function or build that never ran left none. A group
    named explicitly but not in the state is someone else's and is not listed.
    Resources in ``exclude`` (state addresses, the retained ones) are kept, so
    their groups are not left behind by this delete.
    """
    skip = set(exclude)
    declared = _log_group_names(state)
    resources = [
        r
        for r in _state_resources(state)
        if r.get("mode", "managed") == "managed" and r.get("address") not in skip
    ]
    security_configs = {}
    for res in resources:
        if "glue_security_configuration" in str(res.get("type", "")):
            vals = res.get("values") or {}
            for key in ("name", "id"):
                if vals.get(key):
                    security_configs[vals[key]] = vals

    found: Dict[str, Dict[str, Any]] = {}

    def add(group: str, res: Dict[str, Any], reason: str, shared: bool = False):
        if group in declared:
            return None
        entry = found.setdefault(
            group,
            {
                "logical_id": res["address"],
                "physical_id": group,
                "type": "aws_cloudwatch_log_group",
                "status": "NOT_MANAGED",
                "status_reason": reason,
                "stack": "terraform",
                "created_for": [],
                "shared": shared,
            },
        )
        entry["created_for"].append(res["address"])
        return entry

    for res in resources:
        tf_type = str(res.get("type", ""))
        vals = res.get("values") or {}
        if tf_type == "aws_lambda_function" and vals.get("function_name"):
            default = f"/aws/lambda/{vals['function_name']}"
            configured = _block(vals.get("logging_config")).get("log_group")
            if not configured or configured == default:
                add(default, res, "Created by Lambda when the function first ran")
        elif tf_type == "aws_codebuild_project" and vals.get("name"):
            default = f"/aws/codebuild/{vals['name']}"
            logs = _block(_block(vals.get("logs_config")).get("cloudwatch_logs"))
            if str(logs.get("status") or "ENABLED").upper() == "DISABLED":
                continue
            configured = logs.get("group_name")
            if not configured or configured == default:
                add(default, res, "Created by CodeBuild when a build first ran")
        elif tf_type in ("aws_glue_crawler", "awscc_glue_crawler"):
            group, shared = _glue_crawler_log_group(vals, security_configs)
            entry = add(
                group, res, "Created by AWS Glue when the crawler first ran", shared
            )
            if entry is not None and shared:
                # The group is not this deployment's; its streams, named after
                # the crawler, are.
                entry.setdefault("log_streams", []).append(
                    vals.get("name") or res["address"]
                )
    return sorted(found.values(), key=lambda e: e["physical_id"])


# ---------------------------------------------------------------------------
# The binary
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Where the state is kept: local (the default) or an S3 backend
# ---------------------------------------------------------------------------

#: ``terraform_state`` values. ``local`` writes no backend, as before the option
#: existed; ``s3`` writes :data:`BACKEND_FILE` naming a bucket idp-cli manages.
STATE_LOCAL = "local"
STATE_S3 = "s3"
STATE_CHOICES = (STATE_LOCAL, STATE_S3)
BACKEND_FILE = "backend.tf"
#: The local state file Terraform writes with no backend configured.
LOCAL_STATE_FILE = "terraform.tfstate"
_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


def check_terraform_state(value: Optional[str]) -> str:
    """``value`` normalised; ``None`` is :data:`STATE_LOCAL`."""
    choice = (value or STATE_LOCAL).strip().lower()
    if choice not in STATE_CHOICES:
        raise IDPConfigurationError(
            f"terraform_state must be one of {', '.join(STATE_CHOICES)}, not {value!r}"
        )
    return choice


def state_bucket_name(account: str, region: str, stack_name: str) -> str:
    """The deployment's state bucket: one per account, Region and deployment.

    The stack name goes in the middle because it may end in a hyphen, which a
    bucket name may not; a Region always ends in a digit. Lowercased, as S3
    requires: two deployments whose names differ only in case share a bucket,
    and their state objects are still apart, because :func:`state_key` keeps
    the name's case.
    """
    name = f"idp-tf-{stack_name.lower()}-{account}-{region}"
    if not _BUCKET_NAME.match(name):
        raise IDPConfigurationError(
            f"cannot name a state bucket for {stack_name!r} in {region}: {name!r} "
            "is not a valid S3 bucket name (at most 63 characters of lowercase "
            "letters, digits and hyphens)"
        )
    return name


def state_key(stack_name: str) -> str:
    return f"{stack_name}/{LOCAL_STATE_FILE}"


def state_lock_table_name(stack_name: str) -> str:
    """The DynamoDB table the backend locks the state with.

    Not S3-native locking (``use_lockfile``): that needs Terraform or OpenTofu
    1.10, and the oldest releases ``make terraform-check`` validates the module
    with are 1.7. Two operators whose binaries chose different mechanisms would
    not exclude each other, so every deploy uses the table.
    """
    return f"idp-tf-lock-{stack_name}"


class StateBackend(dict):
    """The S3 backend a deploy writes: ``bucket``, ``key``, ``region``,
    ``dynamodb_table``, plus what :func:`ensure_state_backend` found
    (``created_bucket``, ``created_table``, ``state_exists``)."""

    @property
    def bucket(self) -> str:
        return self["bucket"]


def render_backend_tf(backend: Dict[str, Any]) -> str:
    return "\n".join(
        [
            ROOT_MARKER,
            "# Rewritten on every deploy with --terraform-state s3. The bucket and",
            "# the lock table are kept when the deployment is deleted.",
            "",
            "terraform {",
            '  backend "s3" {',
            f"    bucket         = {json.dumps(backend['bucket'])}",
            f"    key            = {json.dumps(backend['key'])}",
            f"    region         = {json.dumps(backend['region'])}",
            "    encrypt        = true",
            f"    dynamodb_table = {json.dumps(backend['dynamodb_table'])}",
            "  }",
            "}",
            "",
        ]
    )


def state_bucket_policy(bucket: str, partition: str) -> Dict[str, Any]:
    """Deny every request that is not over TLS: the state holds secrets in clear."""
    arn = f"arn:{partition}:s3:::{bucket}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [arn, f"{arn}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            }
        ],
    }


_PUBLIC_ACCESS_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}
_SSE_S3 = {
    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
}


def _error_code(e: Exception) -> str:
    return str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))


def check_backend_switch(directory: Path, terraform_state: str) -> None:
    """Refuse a change of backend this increment cannot carry out.

    Moving a local state into S3 (or back) is ``terraform init
    -migrate-state``, which idp-cli does not run yet: with ``-input=false``
    init refuses, and a deploy that wrote a new backend over an existing local
    state would plan every resource as new. Also refuses to overwrite a
    ``backend.tf`` idp-cli did not write.
    """
    backend_tf = directory / BACKEND_FILE
    managed = backend_tf.is_file() and ROOT_MARKER in backend_tf.read_text()
    if terraform_state == STATE_LOCAL:
        if managed:
            raise IDPConfigurationError(
                f"{directory} keeps its Terraform state in S3 (see {backend_tf}); "
                "pass --terraform-state s3 (terraform_state='s3') to deploy it. "
                "Moving the state back to a local file is not supported."
            )
        return
    if backend_tf.exists() and not managed:
        raise IDPConfigurationError(
            f"{backend_tf} exists and was not written by idp-cli; refusing to "
            "overwrite it. Remove it, or deploy without --terraform-state s3 to "
            "keep the backend it configures."
        )
    if not managed and (directory / LOCAL_STATE_FILE).exists():
        raise IDPConfigurationError(
            f"{directory} already has a local state ({LOCAL_STATE_FILE}). Moving "
            "an existing local state into S3 is not supported yet; deploy this "
            "directory without --terraform-state s3, or run `terraform init "
            "-migrate-state` yourself after writing a backend."
        )


def ensure_state_backend(
    region: str,
    stack_name: str,
    *,
    s3: Any = None,
    sts: Any = None,
    dynamodb: Any = None,
) -> StateBackend:
    """Create the state bucket and lock table if missing; reuse them if present.

    A new bucket gets a public access block, versioning, SSE-S3 default
    encryption and a policy denying non-TLS requests. An existing bucket must
    belong to the caller's account (a bucket of that name in another account is
    refused, never written to); it is never recreated or emptied, and of those
    four settings only the ones it lacks entirely are applied, which finishes a
    creation an earlier deploy was interrupted in. A setting someone changed
    (versioning suspended, a different policy) is left alone with a warning.
    """
    import boto3

    sts = sts or boto3.client("sts", region_name=region)
    s3 = s3 or boto3.client("s3", region_name=region)
    dynamodb = dynamodb or boto3.client("dynamodb", region_name=region)

    identity = sts.get_caller_identity()
    account = identity["Account"]
    partition = identity["Arn"].split(":")[1]
    bucket = state_bucket_name(account, region, stack_name)
    key = state_key(stack_name)
    table = state_lock_table_name(stack_name)

    created_bucket = False
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
    except Exception as e:  # botocore ClientError
        code = _error_code(e)
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise IDPConfigurationError(
                f"state bucket {bucket} exists but is not readable as a bucket of "
                f"account {account} ({code or e}): it belongs to another account, "
                "or these credentials lack s3:ListBucket on it. Nothing was "
                "written to it."
            ) from e
        created_bucket = _create_state_bucket(s3, bucket, region)
    _complete_state_bucket(s3, bucket, partition, account)
    logger.info(
        "%s Terraform state bucket %s",
        "Created" if created_bucket else "Reusing",
        bucket,
    )

    created_table = False
    try:
        dynamodb.describe_table(TableName=table)
    except Exception as e:
        if _error_code(e) != "ResourceNotFoundException":
            raise
        dynamodb.create_table(
            TableName=table,
            AttributeDefinitions=[{"AttributeName": "LockID", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "LockID", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName=table)
        created_table = True
        logger.info("Created Terraform state lock table %s", table)

    state_exists = True
    if created_bucket:
        state_exists = False
    else:
        try:
            s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
        except Exception as e:
            if _error_code(e) not in ("404", "NoSuchKey", "NotFound"):
                raise
            state_exists = False
    return StateBackend(
        bucket=bucket,
        key=key,
        region=region,
        dynamodb_table=table,
        created_bucket=created_bucket,
        created_table=created_table,
        state_exists=state_exists,
    )


def _create_state_bucket(s3: Any, bucket: str, region: str) -> bool:
    """Create ``bucket``; ``False`` when it turned out to be the caller's already."""
    kwargs: Dict[str, Any] = {"Bucket": bucket}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        s3.create_bucket(**kwargs)
    except Exception as e:
        if _error_code(e) == "BucketAlreadyOwnedByYou":
            return False
        if _error_code(e) == "BucketAlreadyExists":
            raise IDPConfigurationError(
                f"state bucket name {bucket} is taken by another account; "
                "nothing was written to it"
            ) from e
        raise
    s3.get_waiter("bucket_exists").wait(Bucket=bucket)
    return True


def _complete_state_bucket(s3: Any, bucket: str, partition: str, account: str) -> None:
    """Apply each protective setting the bucket lacks entirely; warn on a changed one.

    Every call names the expected owner, so none can reach a bucket that changed
    hands after :func:`ensure_state_backend` checked it.
    """
    own = {"Bucket": bucket, "ExpectedBucketOwner": account}
    try:
        pab = s3.get_public_access_block(**own)
    except Exception as e:
        if _error_code(e) != "NoSuchPublicAccessBlockConfiguration":
            raise
        s3.put_public_access_block(
            **own, PublicAccessBlockConfiguration=_PUBLIC_ACCESS_BLOCK
        )
        pab = None

    if pab is not None:
        off = sorted(
            k
            for k in _PUBLIC_ACCESS_BLOCK
            if not pab.get("PublicAccessBlockConfiguration", {}).get(k)
        )
        if off:
            logger.warning(
                "State bucket %s's public access block has %s off; it was left "
                "as it is",
                bucket,
                ", ".join(off),
            )

    status = s3.get_bucket_versioning(**own).get("Status")
    if status is None:
        s3.put_bucket_versioning(**own, VersioningConfiguration={"Status": "Enabled"})
    elif status != "Enabled":
        logger.warning(
            "Versioning on state bucket %s is %s; a lost or corrupted state "
            "cannot be recovered from an earlier version",
            bucket,
            status,
        )

    try:
        s3.get_bucket_encryption(**own)
    except Exception as e:
        if _error_code(e) != "ServerSideEncryptionConfigurationNotFoundError":
            raise
        s3.put_bucket_encryption(**own, ServerSideEncryptionConfiguration=_SSE_S3)

    try:
        policy = json.loads(s3.get_bucket_policy(**own)["Policy"])
    except Exception as e:
        if _error_code(e) != "NoSuchBucketPolicy":
            raise
        s3.put_bucket_policy(
            **own, Policy=json.dumps(state_bucket_policy(bucket, partition))
        )
        return
    if not any(
        s.get("Effect") == "Deny"
        and "false"
        == str(
            (s.get("Condition") or {}).get("Bool", {}).get("aws:SecureTransport", "")
        ).lower()
        for s in policy.get("Statement") or []
    ):
        logger.warning(
            "State bucket %s has a bucket policy without a deny on non-TLS "
            "requests; it was left as it is",
            bucket,
        )


def write_backend_tf(directory: Path, backend: Dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / BACKEND_FILE).write_text(render_backend_tf(backend))


#: What ``terraform state list`` (and OpenTofu's) prints when no state exists yet.
NO_STATE_MESSAGE = "No state file was found"


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
        """The addresses in state; empty when there is no state yet.

        ``terraform state list`` exits 1 with "No state file was found!" in a
        directory that has been initialised but never applied or imported into,
        which is where a migration's ``adopt-user-pool`` phase first asks.
        """
        try:
            text = self.capture("state", "list")
        except IDPStackError as e:
            if NO_STATE_MESSAGE in str(e):
                return []
            raise
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
