import React, { useState, useEffect } from 'react';
import { FormField, Textarea, Input } from '@cloudscape-design/components';
import { ClassLike, formatValueForInput, parseInputValue, resolveAttributeType } from '../utils/schemaHelpers';

interface SchemaAttribute {
  type?: string;
  description?: string;
  default?: unknown;
  examples?: unknown[];
  [key: string]: unknown;
}

interface MetadataFieldsProps {
  attribute: SchemaAttribute;
  onUpdate: (updates: Partial<SchemaAttribute>) => void;
  /** Used only to resolve a `$ref`'d attribute's real type when parsing Default Value. */
  availableClasses?: ClassLike[];
}

const MetadataFields = ({ attribute, onUpdate, availableClasses }: MetadataFieldsProps): React.JSX.Element => {
  // Local state for buffering user input without immediate parsing
  const [examplesInput, setExamplesInput] = useState('');
  const [defaultValueInput, setDefaultValueInput] = useState('');

  // Initialize local state from attribute values
  useEffect(() => {
    if (!attribute.examples) {
      setExamplesInput('');
    } else if (Array.isArray(attribute.examples)) {
      setExamplesInput(attribute.examples.map((ex) => (typeof ex === 'object' ? JSON.stringify(ex) : ex)).join(', '));
    }
  }, [attribute.examples]);

  useEffect(() => {
    setDefaultValueInput(formatValueForInput(attribute.default));
  }, [attribute.default]);

  // Handle Examples field blur - parse and update parent state
  const handleExamplesBlur = (): void => {
    if (!examplesInput.trim()) {
      const updates = { ...attribute };
      delete updates.examples;
      onUpdate(updates);
      return;
    }
    try {
      const parsed = JSON.parse(`[${examplesInput}]`) as unknown[];
      onUpdate({ examples: parsed });
    } catch {
      const examples = examplesInput
        .split(',')
        .map((v) => v.trim())
        .filter((v) => v);
      onUpdate({ examples: examples.length > 0 ? examples : undefined });
    }
  };

  // Handle Default Value field blur - parse and update parent state
  const handleDefaultValueBlur = (): void => {
    if (!defaultValueInput) {
      const updates = { ...attribute };
      delete updates.default;
      onUpdate(updates);
      return;
    }
    // Resolve a bare `$ref` to its target type first: read straight off the node
    // it is `undefined`, so a JSON default typed into an object field was stored
    // as the raw string rather than parsed (GitHub #906).
    const parsed = parseInputValue(defaultValueInput, resolveAttributeType(attribute, availableClasses));
    onUpdate({ default: parsed });
  };

  return (
    <>
      <FormField
        label="Description"
        description="Describe what information to extract and provide specific instructions for the LLM. Be clear about format, units, and any special handling needed."
      >
        <Textarea
          value={attribute.description || ''}
          onChange={({ detail }) => onUpdate({ description: detail.value || undefined })}
          rows={3}
          placeholder="e.g., The total amount due including tax, formatted as a decimal number"
        />
      </FormField>

      <FormField
        label="Examples"
        description="Provide example values to guide extraction. This helps the LLM understand the expected format and content. Enter comma-separated values or a JSON array."
      >
        <Textarea
          value={examplesInput}
          onChange={({ detail }) => setExamplesInput(detail.value)}
          onBlur={handleExamplesBlur}
          rows={2}
          placeholder='e.g., "INV-2024-001", "PO-12345" or ["John Doe", "Jane Smith"]'
        />
      </FormField>

      <FormField
        label="Default Value"
        description="Fallback value to use if this field is not found or cannot be extracted from the document."
      >
        <Input
          value={defaultValueInput}
          onChange={({ detail }) => setDefaultValueInput(detail.value)}
          onBlur={handleDefaultValueBlur}
          placeholder="e.g., 0, N/A, or a JSON value"
        />
      </FormField>
    </>
  );
};

export default MetadataFields;
