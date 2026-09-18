# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# from https://github.com/aws-samples/aws-waf-ipset-auto-update-aws-ip-ranges

import json
import urllib.request
import boto3
import os
import logging
import cfnresponse
from log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

def lambda_handler(event, context):
    """
    Lambda function that updates a WAF IPSet with AWS Lambda service IP ranges.
    
    This function fetches the latest AWS IP range data and updates a WAF IPSet with
    ranges associated with the Lambda service, allowing traffic from AWS Lambda services.
    It can be triggered by CloudWatch Events or as a Custom Resource during CloudFormation deployment.
    """

    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

    # Initialize response for CloudFormation custom resource
    if 'ResponseURL' in event:
        response_data = {}
        physical_id = event.get('PhysicalResourceId', context.log_stream_name)
        
        # Handle CloudFormation DELETE event
        if event.get('RequestType') == 'Delete':
            logger.info("CloudFormation DELETE event - no action needed")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data, physical_id)
            return

    try:
        ipset_id = os.environ.get('IPSET_ID')
        ipset_name = os.environ.get('IPSET_NAME')
        region = os.environ.get('AWS_REGION', 'us-east-1')
        
        if not ipset_id and not ipset_name:
            raise ValueError("Either IPSET_ID or IPSET_NAME environment variable must be set")
        
        # Get the IP ranges
        lambda_ip_ranges = get_aws_ip_ranges('EC2')  # Lambda uses EC2 addresses
        
        if not lambda_ip_ranges:
            logger.warning("No Lambda IP ranges found")
            lambda_ip_ranges = ["0.0.0.0/32"]  # Fallback to a dummy IP that won't match anything
        
        # Update the IPSet
        wafv2_client = boto3.client('wafv2', region_name=region)
        
        # If ipset_id not provided, try to find it by name
        if not ipset_id and ipset_name:
            ipset_id = get_ipset_id_by_name(wafv2_client, ipset_name, region)
            
        if not ipset_id:
            raise ValueError(f"Could not find IPSet with name {ipset_name}")
        
        # Get current IPSet
        ipset = get_ipset(wafv2_client, ipset_id, region)
        if not ipset:
            raise ValueError(f"Could not retrieve IPSet with ID {ipset_id}")
        
        # Update the IPSet
        response = update_ipset(wafv2_client, ipset, lambda_ip_ranges)
        
        logger.info(f"Successfully updated IPSet with {len(lambda_ip_ranges)} Lambda IP ranges")
        
        # Send success response for CloudFormation custom resource
        if 'ResponseURL' in event:
            response_data['Message'] = f"Updated IPSet with {len(lambda_ip_ranges)} Lambda IP ranges"
            cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data, physical_id)
            
        return {
            'statusCode': 200,
            'body': json.dumps(f"Updated IPSet with {len(lambda_ip_ranges)} Lambda IP ranges")
        }
    
    except Exception as e:
        logger.error(f"Error updating IPSet: {str(e)}")
        
        # Send failure response for CloudFormation custom resource
        if 'ResponseURL' in event:
            response_data['Message'] = f"Error updating IPSet: {str(e)}"
            cfnresponse.send(event, context, cfnresponse.FAILED, response_data, physical_id, reason=str(e))
            
        return {
            'statusCode': 500,
            'body': json.dumps(f"Error updating IPSet: {str(e)}")
        }

def get_aws_ip_ranges(service):
    """Fetch AWS IP ranges for a specific service."""
    url = 'https://ip-ranges.amazonaws.com/ip-ranges.json'
    
    try:
        response = urllib.request.urlopen(url)  # nosec B310 - hardcoded trusted AWS endpoint
        ip_ranges = json.loads(response.read().decode('utf-8'))
        
        # Extract IPv4 ranges for the specified service
        service_ranges = []
        for prefix in ip_ranges.get('prefixes', []):
            if prefix.get('service') == service:
                service_ranges.append(prefix.get('ip_prefix'))
        
        logger.info(f"Found {len(service_ranges)} IP ranges for {service} service")
        return service_ranges
    
    except Exception as e:
        logger.error(f"Error fetching AWS IP ranges: {str(e)}")
        raise

def get_ipset_id_by_name(wafv2_client, ipset_name, region):
    """Find an IPSet ID by its name."""
    try:
        response = wafv2_client.list_ip_sets(Scope='REGIONAL', Limit=100)
        
        for ipset in response.get('IPSets', []):
            if ipset['Name'] == ipset_name:
                return ipset['Id']
        
        # Handle pagination if needed
        while 'NextMarker' in response:
            response = wafv2_client.list_ip_sets(
                Scope='REGIONAL', 
                Limit=100,
                NextMarker=response['NextMarker']
            )
            
            for ipset in response.get('IPSets', []):
                if ipset['Name'] == ipset_name:
                    return ipset['Id']
        
        return None
    
    except Exception as e:
        logger.error(f"Error finding IPSet by name: {str(e)}")
        raise

def get_ipset(wafv2_client, ipset_id, region):
    """Get the current IPSet configuration."""
    try:
        ipsets = wafv2_client.list_ip_sets(Scope='REGIONAL', Limit=100)
        
        for ipset in ipsets.get('IPSets', []):
            if ipset['Id'] == ipset_id:
                return wafv2_client.get_ip_set(
                    Name=ipset['Name'],
                    Scope='REGIONAL',
                    Id=ipset_id
                )
        
        # Handle pagination if needed
        while 'NextMarker' in ipsets:
            ipsets = wafv2_client.list_ip_sets(
                Scope='REGIONAL', 
                Limit=100,
                NextMarker=ipsets['NextMarker']
            )
            
            for ipset in ipsets.get('IPSets', []):
                if ipset['Id'] == ipset_id:
                    return wafv2_client.get_ip_set(
                        Name=ipset['Name'],
                        Scope='REGIONAL',
                        Id=ipset_id
                    )
        
        return None
    
    except Exception as e:
        logger.error(f"Error getting IPSet: {str(e)}")
        raise

def update_ipset(wafv2_client, ipset, ip_ranges):
    """Update an IPSet with the specified IP ranges."""
    try:
        return wafv2_client.update_ip_set(
            Name=ipset['IPSet']['Name'],
            Scope='REGIONAL',
            Id=ipset['IPSet']['Id'],
            Description=ipset['IPSet']['Description'],
            Addresses=ip_ranges,
            LockToken=ipset['LockToken']
        )
    
    except Exception as e:
        logger.error(f"Error updating IPSet: {str(e)}")
        raise