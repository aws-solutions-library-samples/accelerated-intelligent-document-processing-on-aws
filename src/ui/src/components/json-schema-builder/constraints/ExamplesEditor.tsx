import React, { useState } from 'react';
import {
  Box,
  SpaceBetween,
  Header,
  FormField,
  Input,
  Textarea,
  Button,
  ExpandableSection,
  Alert,
  Container,
  ColumnLayout,
} from '@cloudscape-design/components';
import { X_AWS_IDP_ATTRIBUTES_PROMPT, X_AWS_IDP_CLASS_PROMPT, X_AWS_IDP_IMAGE_PATH } from '../../../constants/schemaConstants';

/**
 * A few-shot example entry inside a class's `x-aws-idp-examples` array.
 *
 * Fields are keyed by their canonical `x-aws-idp-*` names. The legacy camelCase
 * names (`classPrompt` / `attributesPrompt` / `imagePath`) are still read, since
 * older configs — and every config written by earlier versions of this editor —
 * use them.
 */
type Example = Record<string, unknown>;

/** Canonical key for each editable field, with its legacy alias. */
const FIELD_KEYS = {
  classPrompt: { canonical: X_AWS_IDP_CLASS_PROMPT, legacy: 'classPrompt' },
  attributesPrompt: { canonical: X_AWS_IDP_ATTRIBUTES_PROMPT, legacy: 'attributesPrompt' },
  imagePath: { canonical: X_AWS_IDP_IMAGE_PATH, legacy: 'imagePath' },
} as const;

type FieldName = keyof typeof FIELD_KEYS;

/** Read a field, preferring the canonical key and falling back to the legacy one. */
const readField = (example: Example, field: FieldName): string => {
  const { canonical, legacy } = FIELD_KEYS[field];
  return (example[canonical] ?? example[legacy] ?? '') as string;
};

/**
 * Set a field using the canonical key, dropping the legacy alias so an edited
 * example does not end up carrying both spellings.
 */
const writeField = (example: Example, field: FieldName, value: string): Example => {
  const { canonical, legacy } = FIELD_KEYS[field];
  const updated: Example = { ...example, [canonical]: value };
  delete updated[legacy];
  return updated;
};

interface ExamplesEditorProps {
  examples?: Example[];
  onChange: (examples: Example[]) => void;
}

/**
 * ExamplesEditor Component
 *
 * Manages few-shot examples for classification and extraction.
 * Examples are stored in the x-aws-idp-examples array with:
 * - name: Example identifier
 * - x-aws-idp-class-prompt: Classification prompt (used by classification service)
 * - x-aws-idp-attributes-prompt: Extraction prompt (used by extraction service)
 * - x-aws-idp-image-path: S3 path or local path to example image(s)
 *
 * Legacy camelCase keys are read for backward compatibility; edits are written
 * back using the canonical `x-aws-idp-*` keys.
 */
const ExamplesEditor = ({ examples = [], onChange }: ExamplesEditorProps): React.JSX.Element => {
  const [expandedSections, setExpandedSections] = useState<Record<number, boolean>>({});

  const handleAddExample = (): void => {
    const newExample: Example = {
      id: crypto.randomUUID(),
      name: `Example ${examples.length + 1}`,
      [X_AWS_IDP_CLASS_PROMPT]: '',
      [X_AWS_IDP_ATTRIBUTES_PROMPT]: '',
      [X_AWS_IDP_IMAGE_PATH]: '',
    };
    onChange([...examples, newExample]);
    // Auto-expand the new example
    setExpandedSections({
      ...expandedSections,
      [examples.length]: true,
    });
  };

  const handleUpdateName = (index: number, value: string): void => {
    const updated = [...examples];
    updated[index] = { ...updated[index], name: value };
    onChange(updated);
  };

  const handleUpdateExample = (index: number, field: FieldName, value: string): void => {
    const updated = [...examples];
    updated[index] = writeField(updated[index], field, value);
    onChange(updated);
  };

  const handleDeleteExample = (index: number): void => {
    const updated = examples.filter((_, i) => i !== index);
    onChange(updated);
    // Clean up expanded state
    const newExpanded = { ...expandedSections };
    delete newExpanded[index];
    setExpandedSections(newExpanded);
  };

  const toggleSection = (index: number): void => {
    setExpandedSections({
      ...expandedSections,
      [index]: !expandedSections[index],
    });
  };

  return (
    <SpaceBetween size="m">
      <Box>
        <SpaceBetween size="xs">
          <Header
            {...({ variant: 'h4' } as Record<string, unknown>)}
            description="Add few-shot examples to improve classification and extraction accuracy"
            actions={
              <Button iconName="add-plus" onClick={handleAddExample}>
                Add Example
              </Button>
            }
          >
            Few-Shot Examples ({examples.length})
          </Header>

          {examples.length === 0 && (
            <Alert type="info" header="No examples defined">
              Add examples to provide the model with sample inputs and expected outputs. Examples help improve accuracy for both
              classification and extraction tasks.
            </Alert>
          )}
        </SpaceBetween>
      </Box>

      {examples.map((example, index) => {
        // Use stable ID as key to prevent focus loss on content changes
        const stableKey = (example.id as string) || `example-${index}`;
        return (
          <ExpandableSection
            key={stableKey}
            headerText={(example.name as string) || `Example ${index + 1}`}
            expanded={expandedSections[index] || false}
            onChange={() => toggleSection(index)}
            headerActions={
              <Button
                iconName="remove"
                variant="icon"
                onClick={(e) => {
                  e.stopPropagation();
                  handleDeleteExample(index);
                }}
              />
            }
          >
            <Container>
              <SpaceBetween size="m">
                <FormField label="Example Name" description="Unique identifier for this example">
                  <Input
                    value={(example.name as string) || ''}
                    onChange={({ detail }) => handleUpdateName(index, detail.value)}
                    placeholder="e.g., Invoice Example 1"
                  />
                </FormField>

                <FormField
                  label="Classification Prompt (x-aws-idp-class-prompt)"
                  description="Used by classification service to identify document type. Describe what makes this example match this class. Left empty, this example is skipped for classification."
                  stretch
                >
                  <Textarea
                    value={readField(example, 'classPrompt')}
                    onChange={({ detail }) => handleUpdateExample(index, 'classPrompt', detail.value)}
                    placeholder="This is an example of the class 'Invoice'. Key characteristics: Has invoice number, date, line items, and total amount."
                    rows={4}
                  />
                </FormField>

                <FormField
                  label="Extraction Prompt (x-aws-idp-attributes-prompt)"
                  description="Used by extraction service to extract field values. Show expected output format and values. Left empty, this example is skipped for extraction."
                  stretch
                >
                  <Textarea
                    value={readField(example, 'attributesPrompt')}
                    onChange={({ detail }) => handleUpdateExample(index, 'attributesPrompt', detail.value)}
                    placeholder={`Expected attributes are:\n{\n  "invoiceNumber": "INV-2024-001",\n  "date": "2024-01-15",\n  "total": 1250.00\n}`}
                    rows={8}
                  />
                </FormField>

                <FormField
                  label="Image Path (x-aws-idp-image-path)"
                  description="S3 URI (s3://bucket/path) or local path to example image. Supports directories for multiple images. If the path cannot be read, the example is still sent as text."
                  stretch
                >
                  <Input
                    value={readField(example, 'imagePath')}
                    onChange={({ detail }) => handleUpdateExample(index, 'imagePath', detail.value)}
                    placeholder="s3://my-bucket/examples/invoice-1.png or config_library/examples/"
                  />
                </FormField>

                <Alert type="info">
                  <ColumnLayout columns={2} variant="text-grid">
                    <div>
                      <Box variant="strong">Classification Service</Box>
                      <Box variant="p">
                        Uses <code>x-aws-idp-class-prompt</code> and <code>x-aws-idp-image-path</code>
                      </Box>
                    </div>
                    <div>
                      <Box variant="strong">Extraction Service</Box>
                      <Box variant="p">
                        Uses <code>x-aws-idp-attributes-prompt</code> and <code>x-aws-idp-image-path</code>
                      </Box>
                    </div>
                  </ColumnLayout>
                  <Box variant="p" margin={{ top: 'xs' }}>
                    Each service only sends examples that have its own prompt filled in, and only when the corresponding task prompt
                    contains the <code>{'{FEW_SHOT_EXAMPLES}'}</code> placeholder (present in the shipped prompts).
                  </Box>
                </Alert>
              </SpaceBetween>
            </Container>
          </ExpandableSection>
        );
      })}
    </SpaceBetween>
  );
};

export default ExamplesEditor;
