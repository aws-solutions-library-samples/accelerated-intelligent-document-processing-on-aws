Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Healthcare Multisection Package Configuration

This directory contains a specialized configuration for processing healthcare document packages using the GenAI IDP Accelerator. This configuration demonstrates comprehensive multi-document type support for healthcare workflows including claims processing, patient records, and insurance documentation.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false) — Uses Amazon Textract for OCR, then Bedrock LLM (Nova/Claude) for classification, extraction, assessment, and summarization. Set use_bda: true for BDA mode.

## Validation Level

**Level**: 2 - Minimal Testing

- **Testing Evidence**: This configuration has been tested with the provided sample document `samples/healthcare-multisection-package.pdf`. It demonstrates accurate classification and extraction across multiple healthcare document types including insurance claims, discharge summaries, and provider notes.
- **Usage**: This configuration serves as a reference implementation for healthcare document processing workflows (AWS For Health workshop demo configuration)
- **Known Limitations**: Will require adjustments for organization-specific form layouts and regional healthcare documentation standards

## Key Components

### Document Classes

The configuration defines **10 document classes** with comprehensive nested attributes:

#### 1. Health-Insurance-Claim-Form
Health insurance claim form for processing medical claims.
- **Group Attributes**: PatientInformation, MedicalInformation, PaymentInformation, InsuredInformation, OtherInsuredInformation, EmploymentInformation, InsurancePlanInformation, PhysicianInformation
- **List Attributes**: ServiceInformation (Date-of-Service, Procedures, Charges, Diagnosis-Code, etc.)

#### 2. DischargeSummary
Summary of patient's hospital discharge including assessments and recommendations.
- **Group Attributes**: PatientInformation (Patient, Provider, Gender), VisitDetails (Admitted, Discharged, Discharged-to), Header (HospitalName, DocumentTitle), Discharge, Assessment

#### 3. Provider-Notes
Printed or handwritten notes from attending provider regarding patient's condition.
- **Group Attributes**: Patient-History, Additional-Information (Social-Hx, NKDA, FHx), Review-of-Systems, Medications, Physical-Exam, Patient-Information, Provider-Information

#### 4. Driver-License
United States Driver License document for identity verification.
- **Group Attributes**: LicenseDetails (Country, DocumentType, Class, Endorsements, ExpirationDate, LicenseNumber), PersonalInformation (Name, DOB, Address, Physical characteristics)

#### 5. HealthPlanClaim
Health plan claim document detailing services, costs, and patient information.
- **Group Attributes**: HeaderInformation (HealthPlanName, GroupInformation, CheckNumber), ClaimDetails (ClaimNumber, ServiceDates, BilledAmount, PaidAmount, etc.), PaymentDetails, AdditionalInformation

#### 6. Insurance-Card
Health insurance card for member identification.
- **Group Attributes**: InsuranceDetails (Member-ID, Group-Number, Rx-Bin, Rx-PCN, Rx-Grp, PCP-Name, PCP-Phone, Payer-ID, Health-Plan)

#### 7. Medical-Examination-Report
Report detailing a medical examination for insurance purposes.
- **Group Attributes**: ExamineeDetails (ClaimNumber, ExamineeName, DateOfExamination, DateOfInjury), ChiefComplaints, Header, History, ExaminationDetails

#### 8. Medical-Insurance-Invoice
Hospital billing document from medical facilities.
- **Group Attributes**: InvoiceDetails (Date, InvoiceNumber), BillTo (Name, Address), HospitalInformation, Summary (Subtotal, Tax, Discount, Total), PaymentInstructions
- **List Attributes**: Charges (Description, Amount per line item)

#### 9. Surgical-Pathology-Report
Surgical pathology report for patients with various conditions.
- **Group Attributes**: Report-Details (Accession-Number, Patient, MRN, DOB, Procedure, Attending), Header, Specimen-Details, Gross-Description, Clinical-History, Diagnosis, Immunostains, Microscopic-Description, Comment

#### 10. Prescription
Prescription form for medication documentation.
- **Group Attributes**: PrescriptionDetails (FacilityName, RxNumber, MedicationName, MedicationDosage, Quantity, DaysSupply, Refills, FillDate, RefillDate)

### Attribute Types Demonstration

This configuration demonstrates **Group Attributes** and **List Attributes** at the document class level. All top-level properties in each document class use `$ref` to reference nested object definitions.

#### 1. Group Attributes
All document classes use group attributes (nested object structures) for their top-level properties:
- **PatientInformation**: Patient demographics and contact details
- **PaymentInformation**: Financial details including charges and payments
- **InsuranceDetails**: Insurance plan and member information
- **PhysicianInformation**: Provider details and signatures
- **HeaderInformation**: Document header and metadata
- **ClaimDetails**: Claim-specific information

#### 2. List Attributes
Arrays of repeating records for line-item data:
- **ServiceInformation**: Individual service line items on claim forms (Health-Insurance-Claim-Form)
- **Charges**: Line-item charges on invoices (Medical-Insurance-Invoice)

#### 3. Simple Attributes (Within Group Definitions)
Simple attributes exist **within** the group definitions (`$defs`), not at the document class level:
- **Patient-Name**: Full name of the patient (FUZZY evaluation)
- **Insured-ID-Number**: Unique identifiers (EXACT evaluation)
- **Patient-Birth-Date**: Date fields (EXACT evaluation with date format)
- **Total-Charge**: Payment amounts (EXACT evaluation)

### Evaluation Methods

The configuration uses multiple evaluation methods tailored to data types:

| Method | Usage | Examples |
|--------|-------|----------|
| EXACT | Identifiers, codes, precise values | Account numbers, IDs, ZIP codes |
| FUZZY | Names, addresses, variable text | Patient names, addresses, hospital names |
| LLM | Complex descriptions, clinical text | Medical history, diagnoses, symptoms |

### Classification Settings

This preset carries no `classification:` block, so every classification setting
comes from the system defaults
(`lib/idp_common_pkg/idp_common/config/system_defaults/base-classification.yaml`):

- **Strategy / granularity**: combined, page granularity — one call per page, with
  a `start`/`continue` boundary verdict per page that is folded into sections
- **Modality**: text and images (the default task prompt carries both
  `{DOCUMENT_TEXT}` and `{DOCUMENT_IMAGE}`)
- **Model**: Amazon Nova 2 Lite
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5

The classification component segments each package from the per-page boundary
signal and classifies each section across all 10 document types. To have the model
see the whole package in one call instead, set
`classification.combined.granularity: packet`. A packet longer than
`classification.combined.packet.window_pages` (20 by default) is then split into
overlapping windows and merged, which keeps a request under the 20-image-block
threshold above which Bedrock downscales every image to 2,000 px. Keep
`window_pages` at or below 20 while images are on, and no smaller than your
longest single section.

### Extraction Settings

This preset carries no `extraction:` block either, so extraction uses the system
defaults (`base-extraction.yaml`):

- **Model**: Claude Sonnet 5
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5
- **Document Image Support**: Uses `{DOCUMENT_IMAGE}` placeholder for multimodal extraction

The extraction component processes each document section to extract structured data including nested group information and list attributes.

### Assessment Settings

- **Model**: Amazon Nova Lite (the system default `extraction.confidence.model`)
- **Default Confidence Threshold**: 0.8
- **Temperature**: 0 (deterministic outputs)

The assessment component evaluates extraction confidence for each attribute, including nested structures and array items.

### Evaluation Settings

- **Model**: Claude 3 Haiku (for LLM evaluations)
- **Evaluation Methods**:
  - EXACT: For IDs, account numbers, codes, dates
  - FUZZY: For names, addresses, descriptions
  - LLM: For clinical narratives and complex medical text

## Sample Documents

This configuration works with the following sample document:

- `samples/healthcare-multisection-package.pdf`: A multi-section healthcare document package demonstrating various document types including claim forms, discharge summaries, insurance cards, and more

## Performance Metrics

Based on testing with the provided sample document:

| Metric | Value | Notes |
|--------|-------|-------|
| Doc Split Classification Accuracy | 100% | Accurate identification across 10 document types |
| Attribute Accuracy | 99% (F1 score) | Measured over all group and list attributes |

## Usage Instructions

To use this healthcare configuration:

1. **Deploy with Sample**: Upload `samples/healthcare-multisection-package.pdf` to test the configuration
2. **Review Results**: Examine the classified sections and extracted nested data structure in the UI
3. **Evaluate Performance**: Use the evaluation framework to compare against baseline results
4. **Customize**: Modify attribute definitions for your specific healthcare document formats

## Customization Guidance

### Adding New Document Types

To add a new healthcare document class:
```yaml
- $schema: https://json-schema.org/draft/2020-12/schema
  $id: New-Document-Type
  type: object
  x-aws-idp-document-type: New-Document-Type
  description: Description of the document
  $defs:
    SectionDef:
      type: object
      description: Section description
      properties:
        FieldName:
          type: string
          description: Field description
          x-aws-idp-evaluation-method: EXACT
  properties:
    Section:
      description: Section description
      $ref: '#/$defs/SectionDef'
      x-aws-idp-evaluation-method: LLM
```

### Adding Fields to Existing Documents

To extend an existing definition:
```yaml
properties:
  NewField:
    type: string
    description: Description of the new field
    x-aws-idp-evaluation-method: FUZZY
```

## Contributors

- GenAI IDP Accelerator Team