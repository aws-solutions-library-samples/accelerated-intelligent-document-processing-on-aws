# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Search tool for natural language queries"""

import logging
import time
from typing import Any, Dict

import boto3
from botocore.exceptions import ClientError, EventStreamError

from .base import NO_CALLER, IDPTool

# NOTE: the analytics agent stack (idp_common.agents.analytics -> strands) is
# imported inside execute(), not here. This module is imported for every tool
# lookup, and a module-level import would turn a missing/broken agents
# dependency into a Runtime.ImportModuleError that disables ALL tools in the
# handler instead of just this one.

logger = logging.getLogger(__name__)


def log_analytics_events(context_msg: str = ""):
    """Helper to log analytics events safely."""
    try:
        from idp_common.agents.analytics.analytics_logger import analytics_logger
        analytics_logger.display_summary()
    except Exception as e:
        logger.warning(f"Failed to log analytics events: {e}")


class SearchTool(IDPTool):
    """Natural language search and analytics for IDP"""

    def execute(self, query: str, caller: Any = None, **kwargs) -> Dict[str, Any]:
        """Execute search query using analytics agent"""
        if caller is None:
            return dict(NO_CALLER)
        if not query:
            return {"error": "No query provided"}

        try:
            from idp_common.agents.analytics.agent import create_analytics_agent
            from idp_common.agents.analytics.config import get_analytics_config
        except ImportError as e:
            # A missing dependency (e.g. strands not present in the attached
            # Lambda layer) is a deployment/packaging defect: name the module
            # so the caller can report it, rather than a generic failure.
            logger.error(f"Search tool dependencies unavailable: {e}", exc_info=True)
            return {
                "success": False,
                "query": query,
                "error": (
                    f"Search tool dependencies are not installed in this "
                    f"deployment ({e}). The Lambda layer providing "
                    f"idp_common[agents] is missing or incomplete — redeploy "
                    f"the stack with a correctly built agents layer."
                ),
            }

        try:
            session = boto3.Session()
            config = get_analytics_config()
            agent = create_analytics_agent(config=config, session=session)

            start_time = time.time()
            logger.info(f"Query: [{query}]")
            result = agent(query)
            elapsed = time.time() - start_time
            
            log_analytics_events()
            logger.info(f"Process completed in {elapsed:.2f}s")

            return {
                "success": True,
                "query": query,
                "result": str(result)
            }

        except Exception as e:
            log_analytics_events()
            
            error_str = str(e).lower()
            error_code = getattr(e, 'response', {}).get('Error', {}).get('Code', '') if isinstance(e, (EventStreamError, ClientError)) else ''
            
            if 'unavailable' in error_str or error_code in ['ThrottlingException', 'ServiceUnavailable']:
                message = 'Service temporarily unavailable due to high demand. Please retry in a moment.'
            elif isinstance(e, (EventStreamError, ClientError)):
                message = f'Error: {str(e)}'
            else:
                message = 'An error occurred processing your request. Please try again.'
            
            logger.error(f"Query failed: {e}")
            
            return {
                "success": False,
                "query": query,
                "error": message
            }
