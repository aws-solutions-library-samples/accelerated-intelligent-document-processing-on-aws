import json
import logging
import os
import uuid
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from log_sanitizer import sanitize_event_for_logging

# Set up logging
logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')

# Environment variables
CHAT_HISTORY_TABLE = os.environ['CHAT_HISTORY_TABLE']
DATA_RETENTION_DAYS = int(os.environ.get('DATA_RETENTION_DAYS', '365'))

def handler(event, context):
    """Handler for creating new chat sessions"""
    try:
        logger.info(
            f"Creating chat session with event: "
            f"{json.dumps(sanitize_event_for_logging(event))}"
        )
         
        # Generate session ID
        session_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        
        logger.info(f"Successfully created chat session {session_id}")
        
        # Calculate expiration time
        expiration_time = int((datetime.now(timezone.utc).timestamp() + (DATA_RETENTION_DAYS * 24 * 60 * 60)))
        
        # Get user ID from event
        user_id = 'anonymous'
        if 'identity' in event:
            if 'username' in event['identity']:
                user_id = event['identity']['username']
            elif 'sub' in event['identity']:
                user_id = event['identity']['sub']
        
        # Create session record
        table = dynamodb.Table(CHAT_HISTORY_TABLE)
        table.put_item(
            Item={
                'PK': f'USER#{user_id}',
                'SK': f'SESSION#{session_id}',
                'GSI1PK': f'SESSION#{session_id}',
                'GSI1SK': 'META',
                'type': 'session',
                'sessionId': session_id,
                'createdAt': timestamp,
                'lastMessageAt': timestamp,
                'messages': [],
                'ExpiresAfter': expiration_time
            }
        )
        
        # Return the session details
        return {
            'sessionId': session_id,
            'createdAt': timestamp,
            'lastMessageAt': timestamp,
            'messages': []
        }
            
    except Exception as e:
        error_msg = f"Failed to create chat session: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)
