import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    InternalServerError,
    NotFoundError,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from idp_common.docs_service import create_document_service
from idp_common.job_service import create_job_service
from idp_common.models import Status
from models import (
    GetJobResponse,
    PostJobRequest,
    PostJobResponse,
    Result,
    Timestamps,
    UploadInfo,
)

logger = Logger()
app = APIGatewayRestResolver(enable_validation=True)

# Initialize S3 client. In private VPC mode, S3_ENDPOINT_URL points at the
# VPC interface endpoint so presigned URLs target the customer's private
# network path. When unset, boto3 falls back to the public regional endpoint.
_s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None
_s3_addressing = "virtual" if _s3_endpoint_url else "path"
s3_client = boto3.client(
    "s3",
    endpoint_url=_s3_endpoint_url,
    config=boto3.session.Config(
        signature_version="s3v4",
        s3={"addressing_style": _s3_addressing},
    ),
)

# Environment variables
STAGING_BUCKET = os.environ.get("STAGING_BUCKET_NAME", "")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET_NAME", "")
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_IN_DAYS", "30"))
MAX_FILE_SIZE_BYTES = int(os.environ.get("MAX_FILE_SIZE_BYTES", "5368709120"))  # 5GB
PRESIGNED_URL_EXPIRY_SECONDS = int(
    os.environ.get("PRESIGNED_URL_EXPIRY_SECONDS", "3600")
)  # 1 hour

# Initialize services
document_service = create_document_service()
job_service = create_job_service()


def get_content_type(filename: str) -> str:
    """Determine content type from file extension."""
    if filename.lower().endswith(".zip"):
        return "application/zip"
    raise ValueError("Unsupported file type. Only .zip files are supported")


def compute_job_status(files: dict[str, Status]) -> str:
    """Compute overall job status from file statuses."""
    if not files:
        return "PENDING_UPLOAD"

    statuses = set(files.values())
    terminal = {Status.COMPLETED, Status.FAILED, Status.ABORTED, Status.REJECTED}

    if not all(s in terminal for s in statuses):
        return "IN_PROGRESS"
    if all(s == Status.COMPLETED for s in statuses):
        return "SUCCEEDED"
    if Status.COMPLETED in statuses:
        return "PARTIALLY_SUCCEEDED"
    if all(s == Status.ABORTED for s in statuses):
        return "ABORTED"
    return "FAILED"


def _caller_principal() -> str:
    """
    Extract the calling principal from the API Gateway authorizer claims.

    For Cognito client_credentials tokens (M2M), `sub` and `client_id` are
    both set to the app client UUID; we prefer `sub` for consistency with
    user-token flows but fall back to `client_id` if `sub` is absent.

    Returns an empty string if the caller cannot be identified. An empty
    string will never match any persisted CreatedBy value (we only persist
    non-empty principals), so requests without a resolvable principal are
    effectively denied by the same mismatch-as-404 path used for legitimate
    cross-client reads.
    """
    # Pull directly from the raw event dict rather than going through
    # Powertools' APIGatewayEventRequestContext wrapper, which raises KeyError
    # when `requestContext` is absent (e.g. in unit-test synthetic events and
    # when the function is invoked via a non-API-Gateway path). Access via
    # .raw_event is the documented escape hatch for this case.
    event = getattr(app.current_event, "raw_event", None) or {}
    request_context = event.get("requestContext") if isinstance(event, dict) else None
    if not isinstance(request_context, dict):
        return ""
    authorizer = request_context.get("authorizer") or {}
    if not isinstance(authorizer, dict):
        return ""
    claims = authorizer.get("claims") or {}
    if not isinstance(claims, dict):
        return ""
    return str(claims.get("sub") or claims.get("client_id") or "")


@app.post("/jobs")
def create_job(job: PostJobRequest) -> PostJobResponse:
    """Create a new job and generate presigned URL for file upload."""
    try:
        job_id = str(uuid.uuid4())
        logger.info(f"Creating job with ID: {job_id}")

        content_type = get_content_type(job.fileName)
        object_key = f"jobs/{job_id}/{job.fileName}"

        # Calculate TTL for DynamoDB record
        current_time = datetime.now(timezone.utc)
        expires_after = int(
            (current_time + timedelta(days=DATA_RETENTION_DAYS)).timestamp()
        )

        # Create job record
        metadata_dict = job.metadata.model_dump() if job.metadata else None
        created_by = _caller_principal()
        if not created_by:
            # No resolvable principal on an authorized request is unexpected
            # (API Gateway rejects unauthorized requests before Lambda is
            # invoked) but we log it so operators can spot misconfigurations.
            logger.warning(
                "create_job: could not resolve caller principal from authorizer claims; "
                "CreatedBy will be blank and this job will not be retrievable via GET /jobs"
            )
        job_service.create_job_record(
            job_id=job_id,
            expires_after=expires_after,
            metadata=metadata_dict,
            created_by=created_by or None,
            configuration_version=job.configurationVersion,
        )

        # Generate presigned POST URL. The configuration version is NOT carried
        # on the staging zip's S3 metadata here — it is persisted on the job
        # record above and re-applied by the batch pre-processor as
        # `config-version` metadata on each extracted file (the form the
        # pipeline's Document.from_s3_event actually reads). The DynamoDB job
        # record is the single source of truth for the per-job config version.
        presigned_post = s3_client.generate_presigned_post(
            Bucket=STAGING_BUCKET,
            Key=object_key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, MAX_FILE_SIZE_BYTES],
            ],
            ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
        )

        logger.info(f"Generated presigned POST URL for {object_key}")

        return PostJobResponse(
            jobId=job_id,
            upload=UploadInfo(
                uploadUrl=presigned_post["url"],
                expiresInSeconds=PRESIGNED_URL_EXPIRY_SECONDS,
                requiredHeaders=presigned_post["fields"],
            ),
        )

    except ValueError as e:
        logger.error(f"Validation error: {str(e)}", exc_info=True)
        raise BadRequestError(str(e))
    except ClientError as e:
        logger.error(f"AWS service error: {str(e)}", exc_info=True)
        raise InternalServerError(str(e))
    except Exception as e:
        logger.error(f"Unexpected error creating job: {str(e)}", exc_info=True)
        raise InternalServerError(str(e))


@app.get("/jobs/<job_id>")
def get_job(job_id: str) -> GetJobResponse:
    """Get job status by ID."""
    try:
        job_record = job_service.get_job_record(job_id)

        if not job_record:
            raise NotFoundError(f"Job {job_id} not found")

        # Per-client job scoping: a job is only visible to the principal that
        # created it. Return 404 (not 403) on mismatch to avoid leaking the
        # existence of jobs owned by other clients. Legacy jobs without a
        # persisted CreatedBy (created before this check was added) are
        # treated as visible to any caller — they predate the auth model.
        created_by = job_record.get("CreatedBy")
        if created_by:
            caller = _caller_principal()
            if caller != created_by:
                logger.info(
                    "get_job: principal mismatch on job %s (caller=%s owner=%s); returning 404",
                    job_id,
                    caller or "<unknown>",
                    created_by,
                )
                raise NotFoundError(f"Job {job_id} not found")

        files = job_record.get("Files", {})

        # Enrich IN_PROGRESS files with actual document status
        files = enrich_file_statuses(job_id, files)

        # Determine overall status from file statuses
        job_status = compute_job_status(files)

        # A job is only fully terminal (SUCCEEDED/PARTIALLY_SUCCEEDED) once
        # JobTracker has packaged and uploaded results.zip and set
        # ResultsReady=true. Until then, report IN_PROGRESS so callers do not
        # race JobTracker into a 404 on the presigned download URL.
        results_ready = bool(job_record.get("ResultsReady", False))
        if job_status in ("SUCCEEDED", "PARTIALLY_SUCCEEDED") and not results_ready:
            job_status = "IN_PROGRESS"

        # Create/Upload zipfile when processing is complete
        result = None
        if job_status in ["SUCCEEDED", "PARTIALLY_SUCCEEDED"]:
            download_url = generate_presigned_url(job_id)
            result = Result(
                downloadUrl=download_url, expiresInSeconds=PRESIGNED_URL_EXPIRY_SECONDS
            )

        return GetJobResponse(
            jobId=job_id,
            status=job_status,
            configurationVersion=job_record.get("ConfigurationVersion"),
            timestamps=Timestamps(
                createdAt=job_record.get("CreatedAt", ""),
                updatedAt=job_record.get("UpdatedAt", ""),
            ),
            files={k: v.value for k, v in files.items()},
            result=result,
        )
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}", exc_info=True)
        raise BadRequestError(str(e))
    except ClientError as e:
        logger.error(f"AWS service error: {str(e)}", exc_info=True)
        raise InternalServerError(str(e))
    except Exception as e:
        logger.error(f"Unexpected error getting job: {str(e)}", exc_info=True)
        raise


def generate_presigned_url(job_id: str):
    object_key = f"jobs/{job_id}/results.zip"
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": OUTPUT_BUCKET, "Key": object_key},
        ExpiresIn=PRESIGNED_URL_EXPIRY_SECONDS,
    )


def enrich_file_statuses(job_id: str, files: dict[str, Status]) -> dict[str, Status]:
    """Enrich IN_PROGRESS files with actual document status from tracking table."""
    # Get all the files associated with job currently in progress
    processing_files = [
        f for f, status in files.items() if status == Status.IN_PROGRESS
    ]
    if not processing_files:
        return files

    # Partition the files in batches of 100 (hard limit for batch_get calls to DDB)
    enriched = dict(files)
    for i in range(0, len(processing_files), 100):
        batch = processing_files[i : i + 100]
        # Create list of PKs for the first x00 files
        doc_ids = [f"jobs/{job_id}/{f}" for f in batch]
        docs = document_service.batch_get_documents(doc_ids)
        # Get the current status for each file in batch from DDB
        for doc in docs:
            filename = doc.get("document_id", "").split("/")[-1]
            if filename and filename in enriched:
                enriched[filename] = Status(doc.get("status", Status.IN_PROGRESS.value))

    # Return list of all in progress files and their current status per DDB
    return enriched


def handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)
