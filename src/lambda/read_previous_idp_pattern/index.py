# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging

import boto3
import cfnresponse
from log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """
    Reads the current IDPPattern from SSM SettingsParameter before
    UpdateSettingsValues overwrites it with "Unified".
    This detects Pattern-1 → Unified upgrades so BDA can be auto-enabled.
    """
    logger.info(json.dumps(sanitize_event_for_logging(event)))
    response_data = {"IDPPattern": ""}

    try:
        if event["RequestType"] in ("Create", "Update"):
            ssm_name = event["ResourceProperties"].get("SettingsParameterName", "")
            if ssm_name:
                ssm = boto3.client("ssm")
                try:
                    param = ssm.get_parameter(Name=ssm_name)
                    settings = json.loads(param["Parameter"]["Value"])
                    response_data["IDPPattern"] = settings.get("IDPPattern", "")
                    logger.info(f"Previous IDPPattern: {response_data['IDPPattern']}")
                except ssm.exceptions.ParameterNotFound:
                    logger.info("Settings parameter not found (new stack)")
                except Exception as e:
                    logger.warning(f"Error reading settings: {e}")
    except Exception as e:
        logger.error(f"Error: {e}")

    cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data)
