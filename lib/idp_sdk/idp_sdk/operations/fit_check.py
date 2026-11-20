# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fit check: run sample documents through a deployed stack and report the result.

Every step is an existing SDK operation; this module orders them and writes the
report. See docs/fit-check.md.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from idp_sdk.exceptions import IDPError, IDPProcessingError, IDPValidationError
from idp_sdk.models.fit_check import FitCheckResult

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

DEFAULT_MAX_DOCUMENTS = 50
DEFAULT_TIMEOUT_SECONDS = 3600
DEFAULT_POLL_SECONDS = 15
PROFILE_PREFIX = "fit-check-"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class FitCheckOperation:
    """``client.fit_check``: one command from sample documents to a fit report."""

    def __init__(self, client):
        self._client = client

    # ------------------------------------------------------------- public

    def run(
        self,
        samples_dir: Optional[str] = None,
        stack_name: Optional[str] = None,
        *,
        description: Optional[str] = None,
        config_profile: Optional[str] = None,
        ground_truth_dir: Optional[str] = None,
        output_dir: Optional[str] = None,
        cleanup: bool = False,
        max_documents: int = DEFAULT_MAX_DOCUMENTS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        poll_seconds: int = DEFAULT_POLL_SECONDS,
        from_record: Optional[str] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> FitCheckResult:
        """Process ``samples_dir`` on a stack and write ``fit-report.json`` and
        ``fit-report.md`` to ``output_dir`` (default ``./fit-check-<timestamp>/``).

        Without ``config_profile`` a NEW profile ``fit-check-<timestamp>`` is
        generated: by Discovery from the samples Discovery reads (one file through
        single-document Discovery, several through multi-document Discovery, then
        single-document Discovery per file if that finds no class), and by Quick
        Start's prompt path from ``description`` when Discovery yields nothing. The
        active profile is never changed: its name and content, and the content of
        ``Config#default``, are hashed before the run and again at the end, and a
        difference raises ``IDPError`` after the report is written.

        ``cleanup`` deletes the uploaded documents with everything
        ``batch.delete_documents`` removes, any uploaded baselines, and the
        generated profile (never one passed in ``config_profile``).

        ``from_record`` rebuilds the report from a ``fit-run-record.json`` with no
        AWS access; every other argument but ``output_dir`` is then ignored.

        Raises:
            IDPValidationError: no sample can be processed, more than
                ``max_documents`` can, ``config_profile`` does not exist, or the
                generated profile's name is already taken.
            IDPProcessingError: no configuration could be generated.
            IDPError: the active profile or ``Config#default`` changed.
        """
        say = progress or (lambda message: logger.info(message))
        if from_record:
            record = json.loads(Path(from_record).read_text(encoding="utf-8"))
            out = Path(output_dir or Path(from_record).parent)
            return self._write(record, out, say, write_record=False)
        if not samples_dir:
            raise IDPValidationError("samples_dir is required")

        from idp_sdk._core.fit_check import collect
        from idp_sdk._core.fit_check.inputs import scan_samples

        name = self._client._require_stack(stack_name)
        region = self._client._region
        stamp = _timestamp()
        out = Path(output_dir or f"./{PROFILE_PREFIX}{stamp}")

        samples = scan_samples(samples_dir)
        accepted = [s for s in samples if s.refused is None]
        for s in samples:
            if s.refused:
                say(f"Refused {s.file}: {s.refused}")
        if not accepted:
            raise IDPValidationError(f"No file in {samples_dir} can be processed")
        if len(accepted) > max_documents:
            raise IDPValidationError(
                f"{len(accepted)} files can be processed, more than --max-documents "
                f"{max_documents}; raise it or point at a smaller folder"
            )
        resources = self._client._get_stack_resources(name)
        batch_id = f"{PROFILE_PREFIX}{stamp}"
        pages = sum(s.pages or 0 for s in accepted)
        say(
            f"Will upload {len(accepted)} file(s) ({pages} page(s) counted before "
            f"upload; Office and text files are counted during OCR) to "
            f"s3://{resources.get('InputBucket')}/{batch_id}/"
        )
        for s in accepted:
            say(
                f"  {s.file}: {s.format}, {s.pages if s.pages is not None else '?'} page(s)"
            )

        record = collect.new_record(
            stack_name=name,
            region=region,
            solution_version=self._stack_version(name),
            samples=[s.to_dict() for s in samples],
        )
        record["ground_truth"] = bool(ground_truth_dir)
        record["guard"]["before"] = self._snapshot(name)
        say(_guard_line("before", record["guard"]["before"]))

        generated_name: Optional[str] = None
        uploaded: List[str] = []
        try:
            if config_profile:
                if config_profile not in self._profile_names(name):
                    raise IDPValidationError(
                        f"Configuration profile not found: {config_profile}"
                    )
                profile, generated_by = config_profile, "existing-profile"
            else:
                profile = batch_id
                if profile in self._profile_names(name):
                    raise IDPValidationError(f"Profile {profile} already exists")
                generated_name = profile
                generated_by = self._generate_profile(
                    name, profile, accepted, description, say
                )
            config = self._client.config.download(
                stack_name=name, config_profile=profile
            ).config
            settings = collect.profile_settings(config)
            record["configuration"] = {
                "profile": profile,
                "generated_by": generated_by,
                "settings": settings,
                "pricing": config.get("pricing") or [],
            }
            say(
                f"Profile {profile} ({generated_by}): classes "
                f"{sorted(settings['classes'])}"
            )

            uploaded = self._submit(name, batch_id, profile, accepted, ground_truth_dir)
            say(f"Submitted batch {batch_id}; waiting for results")
            statuses = self._wait(name, batch_id, timeout_seconds, poll_seconds, say)
            record["documents"] = self._collect(
                name, resources, accepted, batch_id, statuses, bool(ground_truth_dir)
            )
        finally:
            if cleanup:
                record["cleanup"] = self._cleanup(
                    name,
                    resources,
                    batch_id if uploaded else None,
                    uploaded if ground_truth_dir else [],
                    generated_name
                    if generated_name and generated_name in self._profile_names(name)
                    else None,
                    say,
                )
            record["guard"]["after"] = self._snapshot(name)
            say(_guard_line("after", record["guard"]["after"]))
            record["guard"]["unchanged"] = (
                record["guard"]["before"] == record["guard"]["after"]
            )

        result = self._write(record, out, say, write_record=True)
        result.config_profile = record["configuration"].get("profile")
        result.batch_id = batch_id
        if not record["guard"]["unchanged"]:
            raise IDPError(
                "The active configuration changed during the fit check: "
                f"before {record['guard']['before']}, after {record['guard']['after']}"
            )
        return result

    # ------------------------------------------------------------- steps

    def _stack_version(self, name: str) -> Optional[str]:
        import boto3

        cfn = boto3.client("cloudformation", region_name=self._client._region)
        description = cfn.describe_stacks(StackName=name)["Stacks"][0].get(
            "Description", ""
        )
        match = re.search(r"\(v(\d+\.\d+\.\d+[^)\s]*)\)", description or "")
        return match.group(1) if match else None

    def _profile_names(self, name: str) -> Dict[str, bool]:
        listing = self._client.config.list(stack_name=name)
        return {v.version_name: v.is_active for v in listing.versions}

    def _snapshot(self, name: str) -> Dict[str, Optional[str]]:
        from idp_sdk._core.fit_check.collect import canonical_sha256

        active = next(
            (n for n, is_active in self._profile_names(name).items() if is_active),
            "default",
        )
        active_config = self._client.config.download(
            stack_name=name, config_profile=active
        ).config
        default_config = self._client.config.download(
            stack_name=name, config_profile="default"
        ).config
        return {
            "active_profile": active,
            "active_sha256": canonical_sha256(active_config),
            "default_sha256": canonical_sha256(default_config),
        }

    def _generate_profile(self, name, profile, accepted, description, say) -> str:
        from idp_sdk._core.fit_check.inputs import (
            DISCOVERY_FORMATS,
            MULTI_DOC_DISCOVERY_FORMATS,
        )

        def classes() -> List[str]:
            if profile not in self._profile_names(name):
                return []
            config = self._client.config.download(
                stack_name=name, config_profile=profile
            ).config
            return [c for c in config.get("classes") or []]

        singles = [s.path for s in accepted if s.format in DISCOVERY_FORMATS]
        multi = [s.path for s in accepted if s.format in MULTI_DOC_DISCOVERY_FORMATS]
        if len(multi) >= 2:
            say(f"Multi-document Discovery over {len(multi)} file(s)")
            try:
                self._client.discovery.run_multi_doc(
                    document_paths=multi,
                    save_to_config=True,
                    config_profile=profile,
                    stack_name=name,
                    region=self._client._region,
                )
            except Exception as e:  # missing extra, a model error: fall through
                say(f"Multi-document Discovery did not complete: {e}")
            if not classes():
                say("No class found; single-document Discovery per file instead")
        if not classes():
            for path in singles:
                say(f"Discovery on {Path(path).name}")
                try:
                    self._client.discovery.run(
                        path, config_profile=profile, stack_name=name
                    )
                except Exception as e:
                    say(f"Discovery on {Path(path).name} failed: {e}")
        if classes():
            return "discovery"
        if description:
            say("Discovery found no class; Quick Start from --description")
            result = self._client.config.bootstrap(
                description,
                stack_name=name,
                target_profile=profile,
                generate_test_set=False,
                status_callback=lambda pct, msg: say(f"  [{pct:3.0f}%] {msg}"),
            )
            if result.success and classes():
                return "quick-start"
            raise IDPProcessingError(
                f"Quick Start did not create a class: {result.error}"
            )
        raise IDPProcessingError(
            "Discovery found no document class in the samples. Pass --description "
            "for Quick Start, or --config-profile to use an existing profile"
        )

    def _submit(self, name, batch_id, profile, accepted, ground_truth_dir) -> List[str]:
        with tempfile.TemporaryDirectory(prefix="fit-check-") as tmp:
            manifest = Path(tmp) / "manifest.csv"
            with manifest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["document_path", "baseline_source"])
                for s in accepted:
                    baseline = ""
                    if ground_truth_dir:
                        candidate = Path(ground_truth_dir) / s.file
                        if candidate.is_dir():
                            baseline = str(candidate.resolve())
                    writer.writerow([s.path, baseline])
            result = self._client.batch.process(
                manifest=str(manifest),
                stack_name=name,
                batch_id=batch_id,
                config_profile=profile,
            )
        return list(result.document_ids)

    def _wait(self, name, batch_id, timeout_seconds, poll_seconds, say):
        deadline = time.monotonic() + timeout_seconds
        last = None
        while True:
            status = self._client.batch.get_status(batch_id, stack_name=name)
            line = (
                f"  {status.completed} completed, {status.failed} failed, "
                f"{status.in_progress} running, {status.queued} queued"
            )
            if line != last:
                say(line)
                last = line
            if status.all_complete or time.monotonic() >= deadline:
                break
            time.sleep(poll_seconds)
        out = {}
        for doc in status.documents:
            bucket = getattr(doc.bucket, "value", doc.bucket)
            entry = {
                "num_pages": doc.num_pages,
                "duration_seconds": doc.duration_seconds,
                "error": doc.error,
                "notes": [],
            }
            if bucket == "completed":
                entry["status"] = "COMPLETED"
            elif bucket == "awaiting_review":
                entry["status"] = "AWAITING_REVIEW"
                entry["notes"].append(
                    "Processing finished and the document is waiting in a review queue"
                )
            elif bucket == "failed":
                entry["status"] = "FAILED"
            else:
                entry["status"] = "NOT_FINISHED"
                entry["error"] = (
                    f"Not finished within {timeout_seconds} s (last status "
                    f"{getattr(doc.status, 'value', doc.status)})"
                )
            out[doc.document_id] = entry
        return out

    def _collect(self, name, resources, accepted, batch_id, statuses, with_eval):
        import boto3

        from idp_common.dynamodb.client import DynamoDBClient
        from idp_common.dynamodb.service import DocumentDynamoDBService
        from idp_sdk._core.fit_check.collect import collect_document

        region = self._client._region
        s3 = boto3.client("s3", region_name=region)
        tracking = DocumentDynamoDBService(
            dynamodb_client=DynamoDBClient(
                table_name=resources.get("DocumentsTable"), region=region
            )
        )
        documents = []
        for sample in accepted:
            key = f"{batch_id}/{sample.file}"
            documents.append(
                collect_document(
                    sample=sample.to_dict(),
                    object_key=key,
                    status=statuses.get(key) or {"status": "NOT_FOUND"},
                    tracking=tracking,
                    s3=s3,
                    output_bucket=resources["OutputBucket"],
                    with_evaluation=with_eval,
                )
            )
        return documents

    def _cleanup(self, name, resources, batch_id, baseline_keys, profile, say):
        import boto3

        summary: Dict[str, Any] = {"batch_id": batch_id, "profile": profile}
        lines = []
        if batch_id:
            deletion = self._client.batch.delete_documents(
                batch_id=batch_id, stack_name=name
            )
            summary["documents_deleted"] = deletion.deleted_count
            summary["documents_failed"] = deletion.failed_count
            lines.append(
                f"Deleted {deletion.deleted_count} document(s) of batch {batch_id} "
                f"({deletion.failed_count} failed): input objects, every output "
                "version, tracking and list records, run records and review items."
            )
        for key in baseline_keys:
            try:
                self._client.evaluation.delete_baseline(key, stack_name=name)
            except Exception as e:
                lines.append(f"Baseline {key} not deleted: {e}")
        if baseline_keys:
            lines.append(f"Deleted the baselines of {len(baseline_keys)} document(s).")
        if profile:
            deleted = self._client.config.delete(
                stack_name=name, config_profile=profile
            )
            summary["profile_deleted"] = deleted.success
            lines.append(
                f"Deleted configuration profile {profile}."
                if deleted.success
                else f"Profile {profile} not deleted: {deleted.error}"
            )
        # Read back what was meant to go.
        s3 = boto3.client("s3", region_name=self._client._region)
        remaining = {}
        if batch_id:
            for bucket_key in ("InputBucket", "OutputBucket"):
                bucket = resources.get(bucket_key)
                if bucket:
                    listed = s3.list_objects_v2(Bucket=bucket, Prefix=f"{batch_id}/")
                    remaining[bucket_key] = listed.get("KeyCount", 0)
        summary["objects_remaining"] = remaining
        if profile:
            summary["profile_remaining"] = profile in self._profile_names(name)
        lines.append(
            "Read back: "
            + ", ".join(
                f"{k} objects under the batch prefix: {v}" for k, v in remaining.items()
            )
            + (
                f"; profile {profile} still listed: {summary['profile_remaining']}"
                if profile
                else ""
            )
            + ". Reporting-database rows, logs and Step Functions history are kept "
            "under the stack's retention settings."
        )
        summary["summary"] = " ".join(lines)
        for line in lines:
            say(line)
        return summary

    def _write(self, record, out: Path, say, *, write_record: bool) -> FitCheckResult:
        from idp_sdk._core.fit_check.report import (
            build_report,
            render_markdown,
            validate_report,
        )

        report = build_report(record)
        validate_report(report)
        out.mkdir(parents=True, exist_ok=True)
        report_path = out / "fit-report.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        markdown_path = out / "fit-report.md"
        markdown_path.write_text(render_markdown(report, record), encoding="utf-8")
        record_path = None
        if write_record:
            record_path = out / "fit-run-record.json"
            record_path.write_text(
                json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8"
            )
        say(f"Wrote {report_path} and {markdown_path}")
        guard = record.get("guard") or {}
        return FitCheckResult(
            output_dir=str(out),
            report_path=str(report_path),
            markdown_path=str(markdown_path),
            record_path=str(record_path) if record_path else None,
            report=report,
            config_profile=(record.get("configuration") or {}).get("profile"),
            active_profile_unchanged=guard.get("unchanged"),
            guard=guard,
            cleanup=record.get("cleanup"),
        )


def _guard_line(when: str, snap: Dict[str, Optional[str]]) -> str:
    return (
        f"Active configuration {when}: profile {snap['active_profile']}, "
        f"sha256 {snap['active_sha256']}; Config#default sha256 {snap['default_sha256']}"
    )
