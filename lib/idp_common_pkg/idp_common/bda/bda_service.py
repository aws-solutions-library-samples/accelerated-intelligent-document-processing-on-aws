# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import logging
import os
import time
import uuid
from typing import Optional

import boto3

from idp_common.bda.bda_ocr import build_profile_arn

logger = logging.getLogger(__name__)


class BdaService:
    def __init__(
        self,
        output_s3_uri: str,
        dataAutomationProjectArn: Optional[str] = None,
        dataAutomationProfileArn: Optional[str] = None,
    ):
        self._output_s3_uri = output_s3_uri

        self._dataAutomationProjectArn = dataAutomationProjectArn

        self._dataAutomationProfileArn = dataAutomationProfileArn
        if not self._dataAutomationProfileArn:
            session = boto3.Session()
            region = os.environ.get("AWS_REGION", "us-east-1")
            identity = session.client("sts").get_caller_identity()
            account_id = identity.get("Account")

            # Build via the shared helper rather than formatting the ARN here.
            # The hand-rolled version hardcoded `arn:aws:` and used a naive
            # region.split("-")[0], which is wrong twice:
            #   * outside the commercial partition the ARN can never resolve —
            #     us-gov-west-1 yielded `arn:aws:...`, so every BDA invoke failed
            #     with "The provided ARN is invalid" (reported from a live
            #     GovCloud deployment, issue #527);
            #   * ap-* regions need the geo prefix `apac`, not `ap`.
            # build_profile_arn handles both; the partition comes from the caller
            # ARN (arn:<partition>:...) exactly as ocr/service.py derives it.
            partition = (
                (identity.get("Arn") or "arn:aws:").split(":")[1] or "aws"
            )  # arn-partition-ok: fallback used only to PARSE the partition out
            # `or ""` guards a missing Account: without it the ARN would read
            # "…:None:data-automation-profile/…" and fail opaquely at invoke time.
            self._dataAutomationProfileArn = build_profile_arn(
                region, account_id or "", partition
            )

        self._bda_client = boto3.client("bedrock-data-automation-runtime")

        return

    def invoke_data_automation_async(
        self, input_s3_uri: str, blueprintArn: Optional[str] = None
    ):
        client_token = str(uuid.uuid4())
        payload = {
            "clientToken": client_token,
            "inputConfiguration": {"s3Uri": input_s3_uri},
            "outputConfiguration": {"s3Uri": self._output_s3_uri},
            "notificationConfiguration": {
                "eventBridgeConfiguration": {"eventBridgeEnabled": True}
            },
            "dataAutomationProfileArn": self._dataAutomationProfileArn,
        }
        if blueprintArn:
            blueprint = {
                "blueprintArn": blueprintArn,
            }
            payload["blueprints"] = [blueprint]
        elif self._dataAutomationProjectArn:
            payload["dataAutomationConfiguration"] = {
                "dataAutomationProjectArn": self._dataAutomationProjectArn,
                "stage": "LIVE",
            }

        response = self._bda_client.invoke_data_automation_async(**payload)
        logger.debug(
            f"Data automation job started with invocation ARN: {response['invocationArn']}"
        )
        return response

    def wait_data_automation_invocation(self, invocationArn: str, sleep_seconds=10):
        # Poll for job status until completion
        while True:
            status_response = self._bda_client.get_data_automation_status(
                invocationArn=invocationArn
            )

            status = status_response["status"]
            logger.debug(f"Current job status: {status}")

            if status in ["Success", "ServiceError", "ClientError"]:
                break

            # Wait before checking again
            time.sleep(sleep_seconds)

    def get_data_automation_invocation(self, invocationArn: str):
        status_response = self._bda_client.get_data_automation_status(
            invocationArn=invocationArn
        )
        logger.debug(status_response)
        status = status_response["status"]
        # Process the results
        if status == "Success":
            logger.debug("Data extraction completed successfully!")
            logger.debug(
                f"Output location: {status_response['outputConfiguration']['s3Uri']}"
            )

            # Here you could download and process the extracted data
            # For example, using boto3 S3 client to download the results

            return {
                "status": "success",
                "output_location": status_response["outputConfiguration"]["s3Uri"],
            }
        else:
            logger.debug(
                f"Data extraction failed with error type: {status_response.get('errorType')}"
            )
            logger.debug(f"Error message: {status_response.get('errorMessage')}")

            return {
                "status": "failed",
                "error_type": status_response.get("errorType"),
                "error_message": status_response.get("errorMessage"),
            }

    def invoke_data_automation(
        self, input_s3_uri: str, blueprintArn: Optional[str] = None, sleep_seconds=10
    ):
        invocation_response = self.invoke_data_automation_async(
            input_s3_uri=input_s3_uri, blueprintArn=blueprintArn
        )
        invocationArn = invocation_response["invocationArn"]
        self.wait_data_automation_invocation(
            invocationArn=invocationArn, sleep_seconds=sleep_seconds
        )
        return self.get_data_automation_invocation(invocationArn=invocationArn)

    # TODO: Add utilities to fetch the BDA results
