Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# IDP Bedrock Data Automation (BDA) Module

This module provides functionality for interacting with Amazon Bedrock Data Automation (BDA) for document processing and information extraction.

## Overview

The BDA module enables seamless integration with Amazon Bedrock Data Automation services, allowing you to:

- Invoke BDA jobs asynchronously
- Monitor job status and retrieve results
- Process extracted data from BDA outputs
- Work with BDA projects and blueprints

## Components

- **BdaService**: Main service class for interacting with BDA
- **BdaInvocation**: Data class for handling BDA job results
- **BdaBlueprintService**: Blueprint lifecycle management, schema conversion, and project synchronization
- **BDABlueprintCreator**: Blueprint CRUD operations (create, update, version, delete)
- **BlueprintOptimizer**: Orchestrates BDA blueprint optimization using ground truth data to improve extraction accuracy
- **CloudFormation Templates**: Templates for creating BDA projects and blueprints

## Usage

### Basic BDA Job Invocation

```python
from idp_common.bda.bda_service import BdaService

# Initialize the service with output location
bda_service = BdaService(
    output_s3_uri="s3://your-bucket/output-path"
)

# Invoke BDA and wait for completion
result = bda_service.invoke_data_automation(
    input_s3_uri="s3://your-bucket/input-path/document.pdf",
    blueprintArn="arn:aws:bedrock:region:account:blueprint/blueprint-id"
)

# Check the result
if result['status'] == 'success':
    output_location = result['output_location']
    print(f"Processing completed. Results at: {output_location}")
else:
    print(f"Processing failed: {result['error_message']}")
```

### Asynchronous Invocation

For more control over the process, you can use the async methods:

```python
# Start the job asynchronously
response = bda_service.invoke_data_automation_async(
    input_s3_uri="s3://your-bucket/input-path/document.pdf",
    blueprintArn="arn:aws:bedrock:region:account:blueprint/blueprint-id"
)

# Get the invocation ARN
invocation_arn = response['invocationArn']

# Later, check the status
bda_service.wait_data_automation_invocation(invocationArn=invocation_arn)
result = bda_service.get_data_automation_invocation(invocationArn=invocation_arn)
```

### Processing BDA Results

The `BdaInvocation` class simplifies working with BDA output:

```python
from idp_common.bda.bda_invocation import BdaInvocation

# Create from S3 output location
bda_invocation = BdaInvocation.from_s3(s3_url=result["output_location"])

# Get the custom output (extracted data)
custom_output = bda_invocation.get_custom_output()

# Access specific fields
if "PatientName" in custom_output:
    patient_name = custom_output["PatientName"]
    print(f"Patient Name: {patient_name}")
```

## Configuration

### BdaService Configuration

The BdaService can be configured with:

- `output_s3_uri`: S3 URI where BDA job results will be stored
- `dataAutomationProjectArn`: Optional ARN of a BDA project
- `dataAutomationProfileArn`: Optional ARN of a BDA profile (defaults to standard profile)

```python
bda_service = BdaService(
    output_s3_uri="s3://your-bucket/output-path",
    dataAutomationProjectArn="arn:aws:bedrock:region:account:data-automation-project/project-id"
)
```

## CloudFormation Templates

The module includes CloudFormation templates for creating BDA resources:

### Creating a BDA Project and Blueprint

Use the provided CloudFormation template in `notebooks/bda/cfn/bda-project.yml`:

```bash
# Deploy the CloudFormation stack
aws cloudformation deploy \
  --template-file bda-project.yml \
  --stack-name my-bda-project \
  --parameter-overrides \
    ProjectName=MyProject \
    ProjectDescription="My BDA Project" \
    BlueprintName=MyBlueprint
```

### Managing BDA Resources

The module includes a Makefile with helpful commands:

```bash
# List all BDA projects
make list-projects

# List all blueprints
make list-blueprints

# Get details of a specific project
make get-project BDA_PROJECT_ARN=arn:aws:bedrock:region:account:data-automation-project/project-id

# Get details of a specific blueprint
make get-blueprint BDA_BLUEPRINT_ARN=arn:aws:bedrock:region:account:blueprint/blueprint-id
```

## Error Handling

The BDA service includes comprehensive error handling:

1. If a BDA job fails, the error details are captured in the result
2. The service optionally automatically polls for job completion.
3. All errors are logged for debugging

## Performance Optimization

For optimal performance with BDA:

1. Use asynchronous invocation for large batches of documents
2. Monitor job status with appropriate polling intervals
3. Consider using BDA projects for consistent processing across multiple documents

## Thread Safety

The BDA service is designed to be thread-safe, supporting concurrent processing of multiple documents in parallel workloads.

## Blueprint Synchronization (`BdaBlueprintService`)

`create_blueprints_from_custom_configuration` aligns a config version's IDP document
classes with the blueprints its BDA project associates. A blueprint *is* the extraction
contract in BDA mode — the project's `customOutputConfiguration.blueprints` list decides
which document types are recognised and which fields come back — so the error contracts
below are what stop a sync from quietly changing what the next document extracts.

### Reading the project is allowed to fail; it is not allowed to look empty

`_retrieve_all_blueprints` **raises** when the project cannot be read, and when it is
given no project ARN. It returns an empty list only when the project genuinely
associates no blueprints, because that is what the empty list means to its callers:

- in replace mode, BDA → IDP reads it as "BDA is empty" and **clears every IDP class**;
- in phase 2, it hides every existing blueprint from `_blueprint_lookup`, so the sync
  creates a **second blueprint for every class**.

A project configured for standard output only has no `customOutputConfiguration` at all,
which the API reports by omitting the field. That is an empty list, not a failure.

### A recorded project is replaced only when it is known to be gone

`get_or_create_project_for_version` verifies the ARN recorded for a version and creates
a replacement **only** for `ResourceNotFoundException`. A throttle, an `AccessDenied` or
a server error on `GetDataAutomationProject` raises: it says nothing about whether the
project still exists, and creating one anyway overwrites the tracking row and orphans
the first project's blueprints while the version's config names the new, empty one.

### Every dropped property is a warning, not just a log line

BDA supports neither objects inside objects nor arrays whose items nest further, so the
transform drops those properties. Each drop is recorded in `_skipped_properties`, which
`_process_single_class` returns as per-class `warnings` and the sync resolver surfaces in
its response. The recorded `type` values are `nested_object`, `nested_array` and
`invalid_property_schema` (a property whose value is not a JSON Schema object — `null`,
or a bare string — which cannot be described to BDA at all and used to fail the whole
class with a raw `TypeError`).

### Failing to associate a blueprint fails its class

Creating a blueprint and then failing to write it into the project leaves it existing in
the account but absent from the extraction contract, so that document type is not
recognised. `_process_classes_parallel` and `_convert_aws_standard_blueprints_parallel`
therefore downgrade the affected classes to `status: failed` with the reason, rather
than reporting a clean sync.

### The project ARN is read through one accessor

`dataAutomationProjectArn` is `Optional[str]`, because the service is also constructed
to create the project (`get_or_create_project_for_version`) and to run the schema
transforms, neither of which needs one. Every method that *does* need it reads
`self._project_arn`, which raises a `RuntimeError` naming the class and the missing ARN
rather than letting a `None` become a botocore `ParamValidationError` several frames
away. Assigning the attribute after construction is supported and is what the callers
that create the project do.

`bda_blueprint_service.py` and `schema_converter.py` carry
`# pyright: reportArgumentType=error`, which is off repo-wide. Both files are at zero
findings under it, so a new site that reads the attribute directly fails `make
typecheck`.

### Deletes are per-blueprint and their failures are returned

`_synchronize_deletes` removes the blueprints to be deleted from the project first —
BDA refuses to delete an associated blueprint — and then deletes them one at a time,
returning the ARNs it could not delete. Those are orphans that the project-scoped
retrieval can no longer see; `cleanup_orphaned_blueprints` lists account-wide and will
still find them. Note that `BDABlueprintCreator.delete_blueprint` reports failure by
returning `False` rather than raising, so its return value is the signal to read.

## Blueprint Optimization

The `BlueprintOptimizer` class uses the BDA `InvokeBlueprintOptimizationAsync` API to improve extraction accuracy by comparing results against ground truth data.

### Usage

```python
from idp_common.bda.blueprint_optimizer import BlueprintOptimizer, OptimizationStatus
from idp_common.bda.bda_blueprint_service import BdaBlueprintService
from idp_common.bda.bda_blueprint_creator import BDABlueprintCreator
from idp_common.config.configuration_manager import ConfigurationManager

optimizer = BlueprintOptimizer(
    blueprint_service=BdaBlueprintService(),
    blueprint_creator=BDABlueprintCreator(),
    config_manager=ConfigurationManager(),
)

result = optimizer.optimize(
    class_schema=class_schema,       # IDP JSON Schema dict
    document_key="path/to/doc.pdf",  # S3 key for sample document
    ground_truth_key="path/to/gt.json",  # S3 key for ground truth
    bucket="my-bucket",
    version="default",
    status_callback=lambda msg: print(msg),  # Optional progress callback
)

if result.status == OptimizationStatus.IMPROVED:
    print(f"Accuracy improved: {result.before_metrics.exact_match:.2f} → {result.after_metrics.exact_match:.2f}")
elif result.status == OptimizationStatus.NO_IMPROVEMENT:
    print("No improvement detected, original schema kept")
elif result.status == OptimizationStatus.FAILED:
    print(f"Optimization failed: {result.error_message}")
```

### Key Behaviors

- **Blueprint Reuse**: Looks up existing blueprints in the BDA project before creating new ones
- **Standard Naming**: New blueprints follow `{StackName}-{ClassName}-{hash}` convention
- **LIVE Stage**: Blueprints are created and referenced in `LIVE` stage
- **S3 Retry**: Results fetched with retry logic (10 attempts, 2s delay) for S3 eventual consistency
- **Polling**: Exponential backoff from 5s to 30s, 15-minute timeout
