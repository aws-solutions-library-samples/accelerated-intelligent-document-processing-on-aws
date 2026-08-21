# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Manifest (feature.yaml) parsing, validation, and type-safe accessors."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


class ManifestError(ValueError):
    """Raised when a feature.yaml is missing, unreadable, or fails schema validation."""


@dataclass(frozen=True)
class TemplateSpec:
    path: str
    requiresMainStackName: bool = True  # noqa: N815 — matches manifest casing


@dataclass(frozen=True)
class CommandStep:
    """One subprocess step of a structured build/package pipeline.

    Executed WITHOUT a shell (``subprocess.run(argv, shell=False)``), so there
    is no ``&&``/``|``/glob/variable expansion — ``cwd`` replaces the
    ``cd X && ...`` shell idiom. Preferred over the legacy shell-string forms
    (``ui.buildCommand`` / ``agentSource.packageCommand``)."""

    argv: List[str]
    cwd: Optional[str] = None


@dataclass(frozen=True)
class UiSpec:
    bundlePath: str  # noqa: N815
    buildCommand: Optional[str] = None  # noqa: N815 — legacy shell form
    build: List[CommandStep] = field(default_factory=list)  # structured form

    @property
    def has_build(self) -> bool:
        """True when the manifest declares any way to build the bundle."""
        return bool(self.build or self.buildCommand)


@dataclass(frozen=True)
class MarketplaceSpec:
    productCode: Optional[str] = None  # noqa: N815
    listingUrl: Optional[str] = None  # noqa: N815
    # Which authority THIS EXTENSION enforces its subscription against:
    # "none" | "simulated" | "marketplace-live". Baked into the extension's
    # template at publish time and forwarded to the host's registerFeature at
    # install, so the host can check the same authority the extension honours
    # instead of whatever the stack happens to be pointed at.
    #
    # Defaults to `none` (serve and declare) — deliberately the OPPOSITE of the
    # host catalog's `marketplace-live` default. An unrecognised value must not
    # lock a paying customer out of something they bought, so the extension side
    # degrades to serving; the host side must not over-claim verification, so it
    # degrades to the strictest authority. See docs/feature-platform.md.
    #
    # OPTIONAL in a second sense: an extension that already keeps the mode
    # somewhere else (e.g. a CloudFormation Mappings constant its functions read)
    # should omit this and send `licenseMode` in the registerFeature payload
    # directly. The host only requires it in the payload; declaring the same fact
    # here as well is the drift the mismatch detector exists to catch.
    licenseMode: Optional[str] = None  # noqa: N815
    # Used only when auto-seeding the marketplace simulator product on deploy.
    pricingModel: Optional[str] = None  # noqa: N815
    dimensions: List[Dict[str, Any]] = field(default_factory=list)


def bake_marketplace_identity(text: str, marketplace: "MarketplaceSpec") -> str:
    """Bake the three ``marketplace.*`` publish-time tokens into `text`.

    Lives here, next to the fields it reads, because BOTH publish paths need it:
    `pack.py` bakes a wrapper template and `publisher.py` bakes the feature
    template. They were two independent `.replace()` chains, and the moment a
    third token was added (`licenseMode`) only one of them learned about it — so
    the field baked correctly on the pack path and shipped as a literal
    `<FEATURE_LICENSE_MODE_TOKEN>` on the publish path. One list, one place.

    Every token is OPTIONAL: a placeholder that isn't present is a no-op, so a
    template that doesn't use one is unaffected.
    """
    return (
        text.replace("<FEATURE_PRODUCT_CODE_TOKEN>", marketplace.productCode or "")
        .replace("<FEATURE_LISTING_URL_TOKEN>", marketplace.listingUrl or "")
        # Absent → "none": an extension that says nothing about its licensing is
        # not claiming to enforce anything, and the host must not infer that it
        # does. The host's own catalog default is deliberately the strict one.
        .replace("<FEATURE_LICENSE_MODE_TOKEN>", marketplace.licenseMode or "none")
    )


@dataclass(frozen=True)
class AgentSourceSpec:
    artifactPath: str  # noqa: N815
    packageCommand: Optional[str] = None  # noqa: N815 — legacy shell form
    package: List[CommandStep] = field(default_factory=list)  # structured form


@dataclass(frozen=True)
class ConfigPresetSpec:
    """Vertical-product config preset shipped with the feature."""

    path: str


@dataclass(frozen=True)
class PackWrapperParams:
    """Names of the wrapper parameters that locate the published artifacts.
    The publisher bakes values into these parameter defaults at publish
    time so deploy-pack only needs --stack-name + --admin-email.

    The pack reads its feature artifacts IN PLACE from the publish bucket
    (like a normal `deploy`), so the wrapper takes the bucket + version-free
    prefix rather than a public artifact-source URL — there is no seller
    bucket and no pre-stage copy."""

    hostTemplateUrlParam: str = "IdpAcceleratorTemplateUrl"  # noqa: N815
    featureBucketParam: Optional[str] = None  # noqa: N815
    prefixParam: Optional[str] = None  # noqa: N815
    versionParam: Optional[str] = None  # noqa: N815


@dataclass(frozen=True)
class PackSpec:
    """Vertical-product pack manifest section. Drives publish-pack/deploy-pack."""

    wrapperTemplatePath: str  # noqa: N815
    wrapperParameters: PackWrapperParams = field(default_factory=PackWrapperParams)  # noqa: N815


@dataclass(frozen=True)
class FeatureManifest:
    """Typed view over a validated feature.yaml."""

    featureId: str  # noqa: N815
    displayName: str  # noqa: N815
    version: str
    template: TemplateSpec
    ui: UiSpec
    description: Optional[str] = None
    iconUrl: Optional[str] = None  # noqa: N815
    # OSS features: a docs-site slug (e.g. "extensions/sample-document-status") that the
    # UI resolves against the published docs site for the "Learn more" link.
    # Marketplace features have no separate docs concept — the UI falls back to
    # their marketplaceListingUrl.
    docsUrl: Optional[str] = None  # noqa: N815
    # Whether the feature gets its own Extensions nav entry while not yet
    # installed. False for reference samples (discoverable only via the
    # catalog browser). Installed features always appear in the nav.
    showInNav: bool = True  # noqa: N815
    marketplace: MarketplaceSpec = field(default_factory=MarketplaceSpec)
    defaultParameters: Dict[str, Any] = field(default_factory=dict)  # noqa: N815
    capabilities: List[str] = field(default_factory=list)
    agentSource: Optional[AgentSourceSpec] = None  # noqa: N815
    configPreset: Optional[ConfigPresetSpec] = None  # noqa: N815
    pipelineHooks: Dict[str, str] = field(default_factory=dict)  # noqa: N815
    pack: Optional[PackSpec] = None

    # Original unparsed dict — handy for debugging and for re-serialising into
    # the published manifest.json artifact.
    raw: Dict[str, Any] = field(default_factory=dict)


def _load_schema() -> Dict[str, Any]:
    schema_path = files("idp_feature_sdk.schemas").joinpath(
        "feature-manifest.schema.json"
    )
    return json.loads(schema_path.read_text(encoding="utf-8"))


_SCHEMA = _load_schema()
_VALIDATOR = Draft202012Validator(_SCHEMA)


def load_manifest(project_dir: Path | str) -> FeatureManifest:
    """Locate, parse, and validate `<project_dir>/feature.yaml`. Raises ManifestError on failure."""
    project = Path(project_dir).resolve()
    manifest_path = project / "feature.yaml"
    if not manifest_path.is_file():
        raise ManifestError(
            f"feature.yaml not found at {manifest_path}. "
            f"Run `idp-feature-cli init` or copy from the feature-template/."
        )
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"feature.yaml is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(f"feature.yaml must be a mapping, got {type(raw).__name__}")

    errors = sorted(_VALIDATOR.iter_errors(raw), key=lambda e: list(e.absolute_path))
    if errors:
        fmt = _format_errors(errors)
        raise ManifestError(f"feature.yaml is invalid:\n{fmt}")

    # Schema passed — map into the typed dataclass.
    template = raw["template"]
    ui = raw["ui"]
    marketplace = raw.get("marketplace") or {}
    agent_source_raw = raw.get("agentSource")
    config_preset_raw = raw.get("configPreset")
    pack_raw = raw.get("pack")

    manifest = FeatureManifest(
        featureId=raw["featureId"],
        displayName=raw["displayName"],
        version=raw["version"],
        docsUrl=raw.get("docsUrl"),
        showInNav=raw.get("showInNav", True),
        template=TemplateSpec(
            path=template["path"],
            requiresMainStackName=template.get("requiresMainStackName", True),
        ),
        ui=UiSpec(
            bundlePath=ui["bundlePath"],
            buildCommand=ui.get("buildCommand"),
            build=_parse_steps(ui.get("build")),
        ),
        description=raw.get("description"),
        iconUrl=raw.get("iconUrl"),
        marketplace=MarketplaceSpec(
            productCode=marketplace.get("productCode"),
            listingUrl=marketplace.get("listingUrl"),
            licenseMode=marketplace.get("licenseMode"),
            pricingModel=marketplace.get("pricingModel"),
            dimensions=list(marketplace.get("dimensions") or []),
        ),
        defaultParameters=dict(raw.get("defaultParameters") or {}),
        capabilities=list(raw.get("capabilities") or []),
        agentSource=AgentSourceSpec(
            artifactPath=agent_source_raw["artifactPath"],
            packageCommand=agent_source_raw.get("packageCommand"),
            package=_parse_steps(agent_source_raw.get("package")),
        )
        if agent_source_raw
        else None,
        configPreset=ConfigPresetSpec(path=config_preset_raw["path"])
        if config_preset_raw
        else None,
        pipelineHooks=dict(raw.get("pipelineHooks") or {}),
        pack=_parse_pack(pack_raw) if pack_raw else None,
        raw=raw,
    )

    # Extra semantic checks the JSON schema can't express:
    _check_paths_exist(project, manifest)

    return manifest


def _parse_steps(raw: Optional[List[Dict[str, Any]]]) -> List[CommandStep]:
    """Map a schema-validated `commandSteps` array into CommandStep objects."""
    if not raw:
        return []
    return [CommandStep(argv=list(step["argv"]), cwd=step.get("cwd")) for step in raw]


def _parse_pack(raw: Dict[str, Any]) -> PackSpec:
    params_raw = raw.get("wrapperParameters") or {}
    return PackSpec(
        wrapperTemplatePath=raw["wrapperTemplatePath"],
        wrapperParameters=PackWrapperParams(
            hostTemplateUrlParam=params_raw.get(
                "hostTemplateUrlParam", "IdpAcceleratorTemplateUrl"
            ),
            featureBucketParam=params_raw.get("featureBucketParam"),
            prefixParam=params_raw.get("prefixParam"),
            versionParam=params_raw.get("versionParam"),
        ),
    )


def _format_errors(errors: List[ValidationError]) -> str:
    lines = []
    for err in errors:
        loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
        lines.append(f"  • {loc}: {err.message}")
    return "\n".join(lines)


def _check_paths_exist(project: Path, manifest: FeatureManifest) -> None:
    """Fail early if template path doesn't exist. UI bundle is optional (may be built later)."""
    template = project / manifest.template.path
    if not template.is_file():
        raise ManifestError(
            f"template file not found at {template} (from feature.yaml -> template.path)"
        )
    # UI bundle may not exist yet (publisher runs the ui build steps first), so
    # only check when neither build form is declared.
    if not manifest.ui.has_build:
        bundle = project / manifest.ui.bundlePath
        if not bundle.is_file():
            raise ManifestError(
                f"ui bundle not found at {bundle} and feature.yaml has no ui.build "
                f"steps (or legacy ui.buildCommand) to build it. Either add build "
                f"steps or commit the pre-built bundle."
            )
    # Step cwd directories must exist — a typo'd cwd would otherwise only
    # surface as a FileNotFoundError mid-publish.
    for label, steps in (
        ("ui.build", manifest.ui.build),
        (
            "agentSource.package",
            manifest.agentSource.package if manifest.agentSource else [],
        ),
    ):
        for i, step in enumerate(steps):
            if step.cwd is not None:
                cwd = project / step.cwd
                if not cwd.is_dir():
                    raise ManifestError(f"{label}[{i}].cwd directory not found: {cwd}")
    # A declared config preset must exist on disk — the publisher uploads it
    # verbatim, and the feature stack's ui-deployer downloads it at install
    # to apply via applyFeatureConfigPreset. A missing file would only surface
    # as a runtime NoSuchKey during install, so fail fast at publish time.
    if manifest.configPreset:
        preset = project / manifest.configPreset.path
        if not preset.is_file():
            raise ManifestError(
                f"config preset not found at {preset} "
                f"(from feature.yaml -> configPreset.path)"
            )
