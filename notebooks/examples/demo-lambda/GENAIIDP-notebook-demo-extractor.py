# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Demo Lambda function for GenAI IDP Accelerator notebook examples.

This is a simplified Lambda function designed specifically for notebook demonstrations.
It shows how custom business logic can modify extraction prompts based on document analysis.

Key Features Demonstrated:
1. Document type detection and custom prompt selection
2. Content-based analysis for specialized processing
3. Clear logging for educational purposes
4. Simplified business logic for easy understanding
"""

import json
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Notebook demo: Custom prompt generator with clear business logic examples.
    
    This function demonstrates how to customize extraction prompts based on:
    1. Document type (Bank Statement, Invoice, etc.)
    2. Document content analysis (business vs personal accounts)
    3. Conditional logic for specialized processing
    """
    
    try:
        logger.info("=== DEMO LAMBDA INVOKED ===")
        logger.info(f"Complete input event: {json.dumps(event, indent=2)}")
        
        # Extract key information from the payload
        config = event.get("config", {})
        placeholders = event.get("prompt_placeholders", {})
        default_content = event.get("default_task_prompt_content", [])
        document = event.get("serialized_document", {})
               
        document_class = placeholders.get("DOCUMENT_CLASS", "")
        document_text = placeholders.get("DOCUMENT_TEXT", "")
        document_image_uris = placeholders.get("DOCUMENT_IMAGE", [])
        document_id = document.get("id", "unknown")
        
        # Log extraction config details
        extraction_config = config.get("extraction", {})
        logger.info("=== EXTRACTION CONFIG ===")
        logger.info(f"Model: {extraction_config.get('model', 'Not specified')}")
        logger.info(f"Temperature: {extraction_config.get('temperature', 'Not specified')}")
        logger.info(f"Max tokens: {extraction_config.get('max_tokens', 'Not specified')}")
        logger.info(f"Custom Lambda ARN: {extraction_config.get('custom_prompt_lambda_arn', 'Not specified')}")
        
        # Default system prompt from config
        default_system_prompt = config.get("extraction", {}).get("system_prompt", "")
        logger.info(f"Default system prompt length: {len(default_system_prompt)} characters")
        
        logger.info("=== BUSINESS LOGIC DECISION TREE ===")
        
        # Demo Logic: Customize based on document type and content
        if "bank statement" in document_class.lower():
            logger.info("🏦 DECISION: Processing as bank statement")
            result = _handle_bank_statement(placeholders, default_system_prompt, document_text)
        elif "invoice" in document_class.lower():
            logger.info("🧾 DECISION: Processing as invoice") 
            result = _handle_invoice(placeholders, default_system_prompt, document_text)
        else:
            logger.info("📄 DECISION: Processing as generic document")
            result = _handle_generic_document(placeholders, default_system_prompt, default_content)
        
        # Log complete output structure
        logger.info("=== OUTPUT ANALYSIS ===")
        logger.info(f"Output keys: {list(result.keys())}")
        logger.info(f"System prompt length: {len(result.get('system_prompt', ''))}")
        logger.info(f"System prompt (first 200 chars): {result.get('system_prompt', '')[:200]}...")
        
        task_content = result.get('task_prompt_content', [])
        logger.info(f"Task prompt content items: {len(task_content)}")
        for i, item in enumerate(task_content[:3]):  # Log first 3 items
            logger.info(f"Content item {i}: keys={list(item.keys())}")
            if 'text' in item:
                logger.info(f"  Text length: {len(item['text'])} characters")
                logger.info(f"  Text sample (first 150 chars): {item['text'][:150]}...")
            if 'image_uri' in item:
                logger.info(f"  Image URI: {item['image_uri']}")
        
        if len(task_content) > 3:
            logger.info(f"  ... and {len(task_content) - 3} more content items")
            
        logger.info("=== DEMO LAMBDA RESPONSE READY ===")
        return result
            
    except Exception as e:
        logger.error("=== DEMO LAMBDA ERROR ===")
        logger.error(f"Error type: {type(e).__name__}")
        logger.error(f"Error message: {str(e)}")
        logger.error(f"Input event keys: {list(event.keys()) if 'event' in locals() else 'Unknown'}")
        # In demo, we'll fail gracefully with detailed error info
        raise Exception(f"Demo Lambda failed: {str(e)}")


def _handle_bank_statement(placeholders, default_system_prompt, document_text):
    """Handle bank statement documents with specialized prompts."""
    
    logger.info("DEMO LOGIC: Applying bank statement customization")
    
    # Analyze document content for business vs personal account
    business_indicators = ["llc", "inc", "corp", "business", "company", "dba"]
    is_business = any(indicator in document_text.lower() for indicator in business_indicators)
    
    logger.info(f"DEMO ANALYSIS: Business account detected: {is_business}")
    
    # Create specialized system prompt
    if is_business:
        custom_system_prompt = """You are a specialized business banking document processor. 
        You understand business financial terminology, corporate account structures, and commercial banking products. 
        Focus on business-specific transaction patterns and corporate financial data."""
        
        context_note = "BUSINESS BANKING CONTEXT"
        business_instructions = """
        - Focus on business transaction types (payroll, vendor payments, operating expenses)
        - Pay attention to business entity information (LLC, Corp, etc.)
        - Note any business banking products or commercial services
        - Consider multi-account relationships and business financial patterns
        """
    else:
        custom_system_prompt = """You are a specialized personal banking document processor.
        You understand consumer banking products, personal financial patterns, and individual account management.
        Focus on personal transaction patterns and consumer banking services."""
        
        context_note = "PERSONAL BANKING CONTEXT"  
        business_instructions = """
        - Focus on personal spending categories and consumer transactions
        - Pay attention to individual account holder information
        - Note personal banking products and services
        - Consider personal financial management patterns
        """
    
    # Create custom task prompt with business context
    custom_task_prompt = f"""
    <demo-customization>
    This is a DEMONSTRATION of custom prompt generation.
    
    {context_note}: This appears to be a {'business' if is_business else 'personal'} bank statement.
    
    Special Processing Instructions:
    {business_instructions}
    </demo-customization>
    
    <document-class>{placeholders.get("DOCUMENT_CLASS")}</document-class>
    
    <attributes-to-extract>
    {placeholders.get("ATTRIBUTE_NAMES_AND_DESCRIPTIONS")}
    </attributes-to-extract>
    
    <document-content>
    {placeholders.get("DOCUMENT_TEXT")}
    </document-content>
    
    Apply the special processing instructions above when extracting the requested attributes.
    Return valid JSON with all requested fields.
    """
    
    logger.info("DEMO RESULT: Generated specialized bank statement prompts")
    
    return {
        "system_prompt": custom_system_prompt,
        "task_prompt_content": [{"text": custom_task_prompt}]
    }


def _handle_invoice(placeholders, default_system_prompt, document_text):
    """Handle invoice documents with specialized prompts."""
    
    logger.info("DEMO LOGIC: Applying invoice customization")
    
    # Check for international invoice indicators
    international_indicators = ["vat", "gst", "euro", "€", "£", "currency", "exchange rate"]
    is_international = any(indicator in document_text.lower() for indicator in international_indicators)
    
    logger.info(f"DEMO ANALYSIS: International invoice detected: {is_international}")
    
    custom_system_prompt = """You are a specialized invoice processing system.
    You understand accounting terminology, tax implications, and invoice structures.
    Focus on accurate financial data extraction and regulatory compliance."""
    
    if is_international:
        context_instructions = """
        - Pay special attention to currency symbols and exchange rates
        - Note VAT/GST tax structures and international tax codes
        - Consider multi-currency transactions and conversions
        - Preserve exact monetary amounts without currency conversion
        """
        context_note = "INTERNATIONAL INVOICE CONTEXT"
    else:
        context_instructions = """
        - Focus on domestic tax structures and local currency
        - Pay attention to standard sales tax and local tax codes
        - Note standard domestic invoice formatting and terms
        - Consider standard domestic payment terms and methods
        """
        context_note = "DOMESTIC INVOICE CONTEXT"
    
    custom_task_prompt = f"""
    <demo-customization>
    This is a DEMONSTRATION of custom prompt generation.
    
    {context_note}: This appears to be an {'international' if is_international else 'domestic'} invoice.
    
    Special Processing Instructions:
    {context_instructions}
    </demo-customization>
    
    <document-class>{placeholders.get("DOCUMENT_CLASS")}</document-class>
    
    <attributes-to-extract>
    {placeholders.get("ATTRIBUTE_NAMES_AND_DESCRIPTIONS")}
    </attributes-to-extract>
    
    <document-content>
    {placeholders.get("DOCUMENT_TEXT")}
    </document-content>
    
    Apply the special processing instructions above when extracting invoice data.
    Return valid JSON with all requested fields.
    """
    
    logger.info("DEMO RESULT: Generated specialized invoice prompts")
    
    return {
        "system_prompt": custom_system_prompt,
        "task_prompt_content": [{"text": custom_task_prompt}]
    }


def _handle_generic_document(placeholders, default_system_prompt, default_content):
    """Handle generic documents with minimal customization."""
    
    logger.info("DEMO LOGIC: Using enhanced generic prompts")
    
    # Add a simple enhancement to show Lambda was called
    enhanced_system_prompt = f"{default_system_prompt}\n\nNote: This extraction is powered by a custom Lambda function for demonstration purposes."
    
    # Add demo context to the beginning of default content
    demo_context = {
        "text": f"""
        <demo-customization>
        This is a DEMONSTRATION of custom prompt generation.
        
        GENERIC DOCUMENT PROCESSING: Document class '{placeholders.get("DOCUMENT_CLASS")}' is being processed with enhanced generic prompts.
        
        The Lambda function detected this as a generic document type and applied standard enhancements.
        </demo-customization>
        """
    }
    
    # Handle both URI-based images and text content
    custom_content = [demo_context]
    
    # Process default content and preserve image URIs
    for item in default_content:
        if "image_uri" in item:
            # Preserve image URI references (they'll be converted back to bytes by ExtractionService)
            custom_content.append({"image_uri": item["image_uri"]})
        elif "text" in item:
            # Keep text content as-is
            custom_content.append(item)
        elif "cachePoint" in item:
            # Keep cache points as-is
            custom_content.append(item)
        else:
            # Keep any other content types
            custom_content.append(item)
    
    logger.info("DEMO RESULT: Generated enhanced generic prompts")
    
    return {
        "system_prompt": enhanced_system_prompt,
        "task_prompt_content": custom_content
    }
