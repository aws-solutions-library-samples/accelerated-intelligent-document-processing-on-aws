# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import boto3
import cfnresponse
import os
import logging
from botocore.exceptions import ClientError
from log_sanitizer import sanitize_event_for_logging
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

cloudwatch = boto3.client('cloudwatch')

def split_widgets_by_type(widgets: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Splits widgets into timeSeries, table, and other types.
    Returns tuple of (timeseries_widgets, table_widgets, other_widgets)
    """
    timeseries_widgets = []
    table_widgets = []
    other_widgets = []
    
    for widget in widgets:
        properties = widget.get('properties', {})
        view = properties.get('view', '')
        
        if view == 'timeSeries':
            timeseries_widgets.append(widget)
        elif view == 'table':
            table_widgets.append(widget)
        else:
            other_widgets.append(widget)
    
    return timeseries_widgets, table_widgets, other_widgets

def get_max_y_value(widgets: List[Dict[str, Any]]) -> int:
    """Returns the maximum y-coordinate value among the given widgets"""
    if not widgets:
        return 0
    return max(widget.get('y', 0) + widget.get('height', 0) for widget in widgets)

def adjust_section_positions(widgets: List[Dict[str, Any]], start_y: int = 0) -> List[Dict[str, Any]]:
    """
    Adjusts positions of widgets within a section, maintaining their relative positions
    and applying the start_y offset.
    """
    if not widgets:
        return []
        
    adjusted_widgets = []
    offset = start_y
    
    for widget in widgets:
        widget_copy = widget.copy()
        widget_copy['y'] += offset
        adjusted_widgets.append(widget_copy)
    
    return adjusted_widgets

def merge_dashboard_bodies(dashboard_1_body: str, dashboard_2_body: str) -> Dict[str, Any]:
    """Merges two dashboard JSON bodies by combining their widgets section by section"""
    try:
        dashboard_1 = json.loads(dashboard_1_body)
        dashboard_2 = json.loads(dashboard_2_body)
            
        # Initialize widgets lists if not present
        dashboard_1.setdefault('widgets', [])
        dashboard_2.setdefault('widgets', [])
        
        # Split widgets by type for each dashboard
        d1_timeseries, d1_tables, d1_other = split_widgets_by_type(dashboard_1['widgets'])
        d2_timeseries, d2_tables, d2_other = split_widgets_by_type(dashboard_2['widgets'])
        
        merged_widgets = []
        current_y = 0
        
        # 1. Dashboard 1 timeSeries
        d1_timeseries_adjusted = adjust_section_positions(d1_timeseries, current_y)
        merged_widgets.extend(d1_timeseries_adjusted)
        current_y = get_max_y_value(merged_widgets)
        
        # 2. Dashboard 2 timeSeries
        d2_timeseries_adjusted = adjust_section_positions(d2_timeseries, current_y)
        merged_widgets.extend(d2_timeseries_adjusted)
        current_y = get_max_y_value(merged_widgets)
        
        # 3. Dashboard 1 tables
        d1_tables_adjusted = adjust_section_positions(d1_tables, current_y)
        merged_widgets.extend(d1_tables_adjusted)
        current_y = get_max_y_value(merged_widgets)
        
        # 4. Dashboard 2 tables
        d2_tables_adjusted = adjust_section_positions(d2_tables, current_y)
        merged_widgets.extend(d2_tables_adjusted)
        current_y = get_max_y_value(merged_widgets)
        
        # 5. Dashboard 1 other
        d1_other_adjusted = adjust_section_positions(d1_other, current_y)
        merged_widgets.extend(d1_other_adjusted)
        current_y = get_max_y_value(merged_widgets)
        
        # 6. Dashboard 2 other
        d2_other_adjusted = adjust_section_positions(d2_other, current_y)
        merged_widgets.extend(d2_other_adjusted)
        
        # Create merged dashboard
        merged_dashboard = dashboard_1.copy()
        merged_dashboard['widgets'] = merged_widgets
            
        return merged_dashboard
        
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding dashboard JSON: {e}")
        raise
    except Exception as e:
        logger.error(f"Error merging dashboards: {e}")
        raise

def get_dashboard_body(dashboard_name: str) -> Optional[str]:
    """Gets the dashboard body from CloudWatch"""
    try:
        response = cloudwatch.get_dashboard(DashboardName=dashboard_name)
        return response['DashboardBody']
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFound':
            logger.warning(f"Dashboard {dashboard_name} not found")
            return None
        logger.error(f"Error getting dashboard {dashboard_name}: {e}")
        raise

def create_or_update_dashboard(dashboard_name: str, dashboard_body: Dict[str, Any]) -> None:
    """Creates or updates a CloudWatch dashboard"""
    try:
        cloudwatch.put_dashboard(
            DashboardName=dashboard_name,
            DashboardBody=json.dumps(dashboard_body)
        )
    except Exception as e:
        logger.error(f"Error creating/updating dashboard: {e}")
        raise

def delete_dashboard(dashboard_name: str) -> None:
    """Deletes a CloudWatch dashboard"""
    try:
        cloudwatch.delete_dashboards(DashboardNames=[dashboard_name])
    except Exception as e:
        logger.error(f"Error deleting dashboard: {e}")
        # Don't raise - best effort deletion

def handler(event: Dict[str, Any], context: Any) -> None:
    """Custom resource handler that merges and creates/updates dashboard"""
    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

    try:
        request_type = event['RequestType']
        props = event.get('ResourceProperties', {})
            
        merged_dashboard_name = props.get('MergedDashboardName')
        if not merged_dashboard_name:
            raise ValueError("MergedDashboardName is required")
        
        # On delete, clean up the dashboard
        if request_type == 'Delete':
            delete_dashboard(merged_dashboard_name)
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physicalResourceId=merged_dashboard_name)
            return
            
        dashboard_1_name = props.get('Dashboard1Name')
        dashboard_2_name = props.get('Dashboard2Name')
        
        if not dashboard_1_name or not dashboard_2_name:
            raise ValueError("Dashboard1Name and Dashboard2Name are required")
            
        # Get dashboard bodies
        dashboard_1_body = get_dashboard_body(dashboard_1_name)
        if not dashboard_1_body:
            raise ValueError(f"Dashboard 1 {dashboard_1_name} not found")
            
        dashboard_2_body = get_dashboard_body(dashboard_2_name)
        if not dashboard_2_body:
            raise ValueError(f"Dashboard 2 {dashboard_2_name} not found")
        
        # Merge dashboards
        merged_dash = merge_dashboard_bodies(dashboard_1_body, dashboard_2_body)
        
        # Log the merged dashboard JSON
        logger.info(f"Merged dashboard JSON: {json.dumps(merged_dash, indent=2)}")
        
        # Create or update the merged dashboard
        create_or_update_dashboard(merged_dashboard_name, merged_dash)
        
        # Return success with the dashboard name and set the physical resource ID
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {
            'DashboardName': merged_dashboard_name
        }, physicalResourceId=merged_dashboard_name)
        
    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        # Pass the physical resource ID even in case of failure
        cfnresponse.send(event, context, cfnresponse.FAILED, {
            'Error': str(e)
        }, physicalResourceId=props.get('MergedDashboardName', 'INVALID'), reason=str(e))