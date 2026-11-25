---
title: "Customer Managed KMS Key"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Customer Managed KMS Key

By default a deployment creates one AWS KMS key, `CustomerManagedEncryptionKey`,
and encrypts its data at rest with it: the S3 buckets, the DynamoDB tables, the SQS
queues, the SNS topics, the CloudWatch log groups, the Secrets Manager secrets and
the nested stacks' resources. If your organisation requires that all data at rest
uses a centrally managed key, you can supply your own key instead with the
`CustomerManagedKmsKeyArn` parameter when you create the stack. The stack then
creates no key, and every resource that would have used the stack's key uses
yours.

## Supplying a key

Set `CustomerManagedKmsKeyArn` to the key's ARN when you create the stack, for
example with the IDP CLI:

```bash
idp-cli deploy \
    --stack-name my-idp-stack \
    --admin-email your.email@example.com \
    --parameters "CustomerManagedKmsKeyArn=arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab" \
    --wait
```

In the CloudFormation console the parameter is in the **Advanced - Encryption (new
stacks only)** group. The value must be a key ARN (`arn:<partition>:kms:<region>:<account>:key/<key-id>`),
not an alias, and a multi-Region key (`mrk-…`) is accepted. Leave it empty to have
the stack create and manage its own key, which is the default.

The stack checks the key before anything uses it, and refuses to create if the key:

- is not in the stack's account and Region;
- is an AWS managed key rather than a customer managed key;
- is not a symmetric (`SYMMETRIC_DEFAULT`) encrypt-and-decrypt key;
- is not enabled (for example disabled, or pending deletion);
- cannot be described by the stack, which usually means its key policy does not let
  the account's IAM principals use it.

## The key policy the key needs

The stack's roles are granted the KMS actions they need on your key through their
IAM policies, scoped to the key's ARN, exactly as they are on the key the stack
would have created. For those IAM grants to take effect, and for the AWS services
that use the key on the stack's behalf, the key policy must contain the statements
below. They are the statements of the key policy the stack gives its own key.
Replace `111122223333` with your account ID; in AWS GovCloud (US) the partition is
`aws-us-gov`, and in the China Regions the service principals end in
`amazonaws.com.cn`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Enable IAM User Permissions",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::111122223333:root" },
      "Action": "kms:*",
      "Resource": "*"
    },
    {
      "Sid": "Allow DynamoDB to use the key",
      "Effect": "Allow",
      "Principal": { "Service": "dynamodb.amazonaws.com" },
      "Action": ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"],
      "Resource": "*"
    },
    {
      "Sid": "Allow CloudWatch Logs to use the key",
      "Effect": "Allow",
      "Principal": { "Service": "logs.amazonaws.com" },
      "Action": ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"],
      "Resource": "*"
    },
    {
      "Sid": "Allow CloudWatch Alarms to use the key for SNS",
      "Effect": "Allow",
      "Principal": { "Service": "cloudwatch.amazonaws.com" },
      "Action": ["kms:Decrypt", "kms:GenerateDataKey*"],
      "Resource": "*"
    },
    {
      "Sid": "Allow S3 Vectors indexing service to use the key",
      "Effect": "Allow",
      "Principal": { "Service": "indexing.s3vectors.amazonaws.com" },
      "Action": ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"],
      "Resource": "*"
    }
  ]
}
```

The S3 Vectors statement is needed whenever the document knowledge base is
created with `KnowledgeBaseVectorStore` set to `S3_VECTORS`, which is the default.
Without it the knowledge base's vector index cannot be created and the stack
rolls back, with an `AccessDeniedException` from `CreateIndex` naming
`indexing.s3vectors.amazonaws.com` on the `S3VectorBucketAndIndex` resource of the
knowledge base's nested stack. You can leave it out only when the knowledge base
is disabled or uses OpenSearch Serverless.

The first statement is the default key policy AWS KMS gives a new key. It delegates
access to IAM, so an IAM policy in the account can grant use of the key; without
it the roles' grants do nothing. If your key policy instead grants key use to named
principals only, the deployment fails with access-denied errors on its first write.
The principal that deploys the stack, or the CloudFormation service role it
deploys with, uses the key too while it creates the encrypted resources; with the
first statement in place an IAM grant of the KMS actions on the key is enough, and
the [example service role](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/iam-roles/cloudformation-management/README.md)
already has one.
The CloudWatch Alarms statement is what lets an alarm publish to the stack's
encrypted alerts topic: without it every alarm still changes state, but no
notification is delivered.

## Changing the key after the stack is created

The key is chosen when the stack is created, and an update cannot change it. Data
already written stays encrypted with the key it was written with, so pointing the
stack at another key would leave that data unreadable to the application, whose
roles would lose their grant on the old key. A custom resource in the template,
`CustomerManagedKmsKeyCheck`, refuses each of these updates, and CloudFormation
rolls the update back:

| Update | What happens |
|---|---|
| Setting `CustomerManagedKmsKeyArn` on an existing stack that has its own key, including a stack created before the parameter existed | Refused. No resource is re-encrypted and the stack's own key is kept. |
| Clearing `CustomerManagedKmsKeyArn` on a stack created with a supplied key | Refused before the stack creates a key of its own. |
| Changing it from one key to another | Refused. |
| Updating the stack with the same value, or leaving it out with `idp-cli`, which keeps each parameter's previous value | Nothing changes. |

Every resource that uses the key waits for the check, so a refused update fails
before anything is re-encrypted. Note that a CloudFormation update made outside
`idp-cli` that does not pass the parameter takes its default, the empty value,
rather than the previous one; on a stack created with a supplied key that is the
"clearing" case above, and it is refused. Pass the parameter with
`UsePreviousValue`, or use `idp-cli`, which does.

`idp-cli deploy` refuses a changed value itself, before calling CloudFormation or
Terraform, and says why.

To move a deployment to a different key, deploy a new stack with the new key.

## What happens when the stack is deleted

A supplied key belongs to you: the stack did not create it and never deletes or
schedules it for deletion. The retained buckets' data stays encrypted with it, so
keep the key while any of that data is needed. `idp-cli delete` names the key and
says it is yours; it does not print the steps for scheduling a key's deletion that
it prints for a key the stack created (see [The retained KMS key](./idp-cli.md#the-retained-kms-key)).

## Terraform

The Terraform module carries the same variable, `customer_managed_kms_key_arn`, with
the same pattern, and creates no key when it is set. `idp-cli deploy --terraform`
refuses a changed value on an existing deployment at plan time, and setting it on
an existing deployment would plan the deletion of the deployment's key, which the
plan guard refuses. Terraform has no rollback, so apply a change to this variable
only through `idp-cli`. Under Terraform the key check runs alongside the resources
that use the key rather than before them, so on a new Terraform deployment a key
the check would refuse fails those resources too, and the apply stops part-way;
correct the key and apply again. See [Terraform](./terraform.md).

## Limits

- The key must be in the stack's account and Region. A key in another account is
  refused by the check.
- One key encrypts everything. A key per service (logs, queues, tables, buckets) is
  not supported.
- The keys a deployment uses for other purposes are unchanged: the optional bastion
  host's EBS key, and the keys given by `ArtifactsBucketKmsKeyArn` and
  `ReportingBucketKmsKeyArn` for buckets you bring.
- A few resources do not use the deployment's key whichever key that is, and
  supplying one does not change them: the access-log bucket and the web UI bucket
  use S3-managed encryption (`AES256`), the Feature Platform's installed-features
  table uses the AWS managed DynamoDB key, and the log groups of the few Lambda
  functions that run only during a stack operation, this parameter's own check
  among them, are not encrypted with a customer managed key.
