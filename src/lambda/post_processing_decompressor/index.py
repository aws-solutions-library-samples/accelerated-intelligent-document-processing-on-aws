# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import json
import os
import logging
from idp_common.models import POST_PROCESSING_SUPPRESSED_STATUSES, Document, Status
from typing import Dict, Any

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

lambda_client = boto3.client('lambda')

CUSTOM_POST_PROCESSOR_ARN = os.environ['CUSTOM_POST_PROCESSOR_ARN']
WORKING_BUCKET = os.environ['WORKING_BUCKET']

# Ceiling on the payload we hand to an ASYNCHRONOUS Lambda invoke. AWS caps an
# InvocationType='Event' payload at 1MB — NOT the 6MB that applies to a
# synchronous RequestResponse invoke, and not the 256KB quoted for some other
# async AWS APIs. Verified empirically against us-west-2: a 1,441,631-byte
# payload is rejected with
#   RequestEntityTooLargeException: ... too large for the Event invocation type
#   (limit 1048576 bytes)
# while a ~700KB payload is accepted (202).
#
# Inflating the compressed document reference easily blows past 1MB on a large
# multi-section packet, which used to surface as that exception, then 3
# EventBridge retries, then the DLQ — so the hook silently never fired for
# exactly the biggest documents. When the decompressed payload does not fit we
# send the ORIGINAL compressed event instead (always small) rather than dropping
# the invocation; the custom post-processor then resolves the reference itself.
#
# Held ~4.6% under the 1,048,576-byte hard limit to cover the invoke envelope, so
# the check is conservative without needlessly downgrading documents that would
# have fitted (an earlier 240KB threshold sent everything above a quarter of the
# real ceiling down the fallback path).
MAX_ASYNC_PAYLOAD_BYTES = 1_000_000


def handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """
    Decompresses documents from StepFunction input and output, then invokes custom post-processor lambda.
    
    This lambda acts as an intermediary between EventBridge and the custom post-processing lambda,
    handling document decompression so external lambdas don't need to import idp_common.
    
    Args:
        event: EventBridge event containing StepFunction execution details
        context: Lambda context
        
    Returns:
        Response from custom post-processor lambda invocation
    """
    logger.info(f"Processing event for custom post-processor invocation")

    try:
        input_decompressed = False
        output_decompressed = False

        # Decompression rewrites exactly two values in place — the `detail.input`
        # and `detail.output` JSON STRINGS — so remembering those two is enough to
        # restore the original (compressed) event for the too-large fallback
        # below. Deliberately not a deepcopy of the whole event: that would double
        # peak memory for a payload we usually never send.
        original_detail_strings = {
            k: event.get('detail', {}).get(k)
            for k in ('input', 'output')
            if isinstance(event.get('detail', {}), dict)
        }

        # Decompress input document if present and compressed
        if event.get('detail', {}).get('input'):
            input_data = json.loads(event['detail']['input'])
            
            # Extract document from input
            input_doc_data = input_data.get('document')
            if input_doc_data and isinstance(input_doc_data, dict) and input_doc_data.get('compressed', False):
                logger.info(f"Input document is compressed, decompressing from S3 URI: {input_doc_data.get('s3_uri', 'N/A')}")
                
                # Decompress document using idp_common
                processed_doc = Document.load_document(input_doc_data, WORKING_BUCKET, logger)
                
                logger.info(f"Decompressed input document: {processed_doc.num_pages} pages")
                
                # Update input_data with decompressed document
                input_data['document'] = processed_doc.to_dict()
                event['detail']['input'] = json.dumps(input_data)
                input_decompressed = True
        
        # Decompress output document if present and compressed
        output_data = None
        if event.get('detail', {}).get('output'):
            output_data = json.loads(event['detail']['output'])
        
        if not output_data:
            logger.error("No output data found in event")
            raise ValueError("Missing output data in event")
        
        # Extract document data - handle both Pattern 1 and Pattern 2/3 structures
        document_data = None
        if 'document' in output_data:
            # Pattern 2/3 structure: at root level
            document_data = output_data['document']
            logger.info("Found document in output_data['document']")
        elif 'Result' in output_data and 'document' in output_data.get('Result', {}):
            # Pattern 1 structure: wrapped in Result
            document_data = output_data['Result']['document']
            logger.info("Found document in output_data['Result']['document']")
        else:
            logger.warning("Document not found in expected locations, using entire output")
            document_data = output_data
        
        # A preprocessing hook (e.g. PII anonymization in "redact copy and stop"
        # mode) halts the execution for the ORIGINAL document after spawning a
        # redacted copy. That execution still ends as SUCCEEDED, so the
        # EventBridge rule matches — but handing the un-redacted original to a
        # customer's post-processor is exactly what the redaction was meant to
        # prevent, and the workflow tracker is about to delete it. The status
        # lives inside detail.output (invisible to an EventBridge pattern), so
        # the check has to happen here. The redacted copy is processed as its own
        # document and fires this hook normally. The same applies to phase 1 of
        # a review-queue document (HITL_IN_PROGRESS: the finalize run fires the
        # hook) and to a rejected document (REJECTED: no usable result).
        suppressed = {s.value for s in POST_PROCESSING_SUPPRESSED_STATUSES}
        wrapper_status = document_data.get('status') if isinstance(document_data, dict) else None
        if wrapper_status in suppressed:
            logger.info(
                "Skipping custom post-processor: document status is %s",
                wrapper_status,
            )
            message = (
                'Skipped custom post-processor for superseded document'
                if wrapper_status == Status.REDACTED_SUPERSEDED.value
                else f'Skipped custom post-processor for {wrapper_status} document'
            )
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': message,
                    'customProcessorArn': CUSTOM_POST_PROCESSOR_ARN,
                    'skipped': True,
                    'reason': wrapper_status,
                })
            }

        # Check if document is compressed
        is_compressed = isinstance(document_data, dict) and document_data.get('compressed', False)

        if is_compressed:
            logger.info(f"Output document is compressed, decompressing from S3 URI: {document_data.get('s3_uri', 'N/A')}")
            
            # Decompress document using idp_common
            processed_doc = Document.load_document(document_data, WORKING_BUCKET, logger)
            
            logger.info(f"Decompressed output document: {processed_doc.num_pages} pages, "
                       f"{len(processed_doc.sections)} sections")
            
            # Reconstruct output_data with decompressed document
            if 'document' in output_data:
                output_data['document'] = processed_doc.to_dict()
            elif 'Result' in output_data and 'document' in output_data.get('Result', {}):
                output_data['Result']['document'] = processed_doc.to_dict()
            else:
                output_data = processed_doc.to_dict()
            
            # Update event with decompressed payload
            event['detail']['output'] = json.dumps(output_data)
            output_decompressed = True
            
            logger.info("Output document decompressed successfully")
        else:
            logger.info("Output document is not compressed, passing through as-is")
        
        # Invoke custom post-processor lambda with decompressed payload.
        #
        # An async (Event) invoke caps the payload at 256KB, and a decompressed
        # document can exceed that. Rather than fail — which cost the invocation
        # entirely after EventBridge's retries — fall back to the original
        # compressed event, which is always small. The post-processor then sees
        # the same `{compressed: true, s3_uri, ...}` reference it would have
        # received before this decompressor existed, and can resolve it via
        # idp_common (or a plain S3 GET + gunzip).
        payload = json.dumps(event)
        payload_bytes = len(payload.encode('utf-8'))
        sent_compressed_fallback = False

        if payload_bytes > MAX_ASYNC_PAYLOAD_BYTES:
            logger.warning(
                "Decompressed payload is %d bytes, over the %d-byte async invoke "
                "limit; invoking the custom post-processor with the ORIGINAL "
                "compressed event instead. The post-processor must resolve the "
                "compressed document reference itself for this document.",
                payload_bytes,
                MAX_ASYNC_PAYLOAD_BYTES,
            )
            # Restore the two strings decompression replaced, then re-serialize.
            for key, original in original_detail_strings.items():
                if original is None:
                    event['detail'].pop(key, None)
                else:
                    event['detail'][key] = original
            payload = json.dumps(event)
            sent_compressed_fallback = True
            input_decompressed = False
            output_decompressed = False

        logger.info(f"Invoking custom post-processor: {CUSTOM_POST_PROCESSOR_ARN}")

        response = lambda_client.invoke(
            FunctionName=CUSTOM_POST_PROCESSOR_ARN,
            InvocationType='Event',  # Async invocation
            Payload=payload
        )

        logger.info(f"Custom post-processor invoked successfully. StatusCode: {response['StatusCode']}")

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Successfully invoked custom post-processor',
                'customProcessorArn': CUSTOM_POST_PROCESSOR_ARN,
                'inputDecompressed': input_decompressed,
                'outputDecompressed': output_decompressed,
                'sentCompressedFallback': sent_compressed_fallback
            })
        }

    except Exception as e:
        logger.error(f"Error processing event: {str(e)}", exc_info=True)
        raise
