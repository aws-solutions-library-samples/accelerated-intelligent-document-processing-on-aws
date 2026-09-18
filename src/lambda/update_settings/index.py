# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import cfnresponse
from log_sanitizer import sanitize_event_for_logging
import json
import os
import logging

# Initialize logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

# SSM client
ssm = boto3.client('ssm')

def updateSSMParameter(props):
    parameterName = props['SettingsName']
    parameterValues = props['SettingsKeyValuePairs']
    response = ssm.get_parameter(Name=parameterName)
    settingsJSON = response['Parameter']['Value']
    settings = json.loads(settingsJSON)
    print(f"Existing settings: {settings}")
    for key, value in parameterValues.items():
        settings[key] = value
    print(f"Updated settings: {settings}")
    newSettingsJSON = json.dumps(settings)
    ssm.put_parameter(Name=parameterName, Value=newSettingsJSON, Overwrite=True)
                        
def handler(event, context):        
    print(json.dumps(sanitize_event_for_logging(event)))
    status = cfnresponse.SUCCESS
    responseData = {}
    reason = "Success"
    props = event["ResourceProperties"]
    if event['RequestType'] != 'Delete':
        try:
            updateSSMParameter(props)
        except Exception as e:
            print(e)
            reason = f"Exception thrown: {e}"
            status = cfnresponse.FAILED              
    cfnresponse.send(event, context, status, responseData, reason=reason)