import React, { useState, useEffect } from 'react';
import { FormField, Select, Input, RadioGroup, SpaceBetween, Textarea, ExpandableSection } from '@cloudscape-design/components';
import { refNode } from '../utils/schemaHelpers';

interface ContainsSchema {
  $ref?: string;
  type?: string;
  properties?: Record<string, Record<string, unknown>>;
  required?: string[];
  [key: string]: unknown;
}

interface AvailableClass {
  name: string;
  id?: string;
  description?: string;
}

interface ConditionOption {
  label: string;
  value: string;
  description: string;
}

interface ContainsSchemaBuilderProps {
  containsSchema?: ContainsSchema | null;
  onChange: (schema: ContainsSchema | null) => void;
  availableClasses?: AvailableClass[];
}

const CONDITION_OPTIONS: ConditionOption[] = [
  { label: 'Equals (const)', value: 'const', description: 'Exact value match' },
  { label: 'Minimum', value: 'minimum', description: 'Minimum value (for numbers)' },
  { label: 'Maximum', value: 'maximum', description: 'Maximum value (for numbers)' },
  { label: 'Pattern (regex)', value: 'pattern', description: 'Regular expression match' },
  { label: 'One of (enum)', value: 'enum', description: 'Comma-separated list of values' },
];

const buildConditionSchema = (property: string, condition: string, value: string): ContainsSchema | null => {
  if (!property || !value) return null;

  const baseSchema: ContainsSchema = {
    type: 'object',
    properties: {},
    required: [property],
  };

  switch (condition) {
    case 'const':
      baseSchema.properties![property] = { const: value };
      break;
    case 'minimum':
      baseSchema.properties![property] = { type: 'number', minimum: parseFloat(value) };
      break;
    case 'maximum':
      baseSchema.properties![property] = { type: 'number', maximum: parseFloat(value) };
      break;
    case 'pattern':
      baseSchema.properties![property] = { type: 'string', pattern: value };
      break;
    case 'enum':
      baseSchema.properties![property] = {
        enum: value
          .split(',')
          .map((v) => v.trim())
          .filter((v) => v),
      };
      break;
    default:
      baseSchema.properties![property] = { const: value };
  }

  return baseSchema;
};

const detectMode = (containsSchema: ContainsSchema | null | undefined): string => {
  if (!containsSchema) return 'property';
  if (containsSchema.$ref) return 'class';

  // Check if it's a simple property match
  if (containsSchema.type === 'object' && containsSchema.properties && Object.keys(containsSchema.properties).length === 1) {
    return 'property';
  }

  return 'custom';
};

const ContainsSchemaBuilder = ({
  containsSchema = null,
  onChange,
  availableClasses = [],
}: ContainsSchemaBuilderProps): React.JSX.Element => {
  const existingContains = containsSchema;
  const initialMode = detectMode(existingContains);

  const [mode, setMode] = useState(initialMode);
  const [selectedClass, setSelectedClass] = useState<string | null>(null);
  const [propertyName, setPropertyName] = useState('');
  const [condition, setCondition] = useState<ConditionOption>(CONDITION_OPTIONS[0]);
  const [propertyValue, setPropertyValue] = useState('');
  const [customSchema, setCustomSchema] = useState(
    existingContains && initialMode === 'custom' ? JSON.stringify(existingContains, null, 2) : '',
  );

  // Initialize from existing schema
  useEffect(() => {
    if (existingContains) {
      if (existingContains.$ref) {
        const className = existingContains.$ref.replace('#/$defs/', '');
        setSelectedClass(className);
      } else if (
        existingContains.type === 'object' &&
        existingContains.properties &&
        Object.keys(existingContains.properties).length === 1
      ) {
        const propName = Object.keys(existingContains.properties)[0];
        const propSchema = existingContains.properties[propName] as Record<string, unknown>;
        setPropertyName(propName);

        if (propSchema.const !== undefined) {
          setCondition(CONDITION_OPTIONS[0]);
          setPropertyValue(String(propSchema.const));
        } else if (propSchema.minimum !== undefined) {
          setCondition(CONDITION_OPTIONS[1]);
          setPropertyValue(String(propSchema.minimum));
        } else if (propSchema.maximum !== undefined) {
          setCondition(CONDITION_OPTIONS[2]);
          setPropertyValue(String(propSchema.maximum));
        } else if (propSchema.pattern) {
          setCondition(CONDITION_OPTIONS[3]);
          setPropertyValue(propSchema.pattern as string);
        } else if (propSchema.enum) {
          setCondition(CONDITION_OPTIONS[4]);
          setPropertyValue((propSchema.enum as unknown[]).join(', '));
        }
      }
    }
  }, [existingContains]);

  const handleModeChange = ({ detail }: { detail: { value: string } }): void => {
    setMode(detail.value);
    // Clear contains when switching modes
    onChange(null);
  };

  const handleClassSelect = ({ detail }: { detail: { selectedOption: { value?: string | null } } }): void => {
    const className = detail.selectedOption.value || '';
    setSelectedClass(className);
    // Guarded rather than interpolated: an empty selection produced a pointer to
    // `#/$defs/undefined`, which resolves to nothing.
    onChange(className ? refNode(className) : null);
  };

  const handlePropertyUpdate = (): void => {
    const schema = buildConditionSchema(propertyName, condition.value, propertyValue);
    if (schema) {
      onChange(schema);
    }
  };

  const handleCustomSchemaChange = (value: string): void => {
    setCustomSchema(value);
    if (!value.trim()) {
      onChange(null);
      return;
    }

    try {
      const parsed = JSON.parse(value) as ContainsSchema;
      onChange(parsed);
    } catch {
      // Invalid JSON, don't update
    }
  };

  const classOptions =
    availableClasses?.map((cls) => ({
      label: cls.name,
      value: cls.name,
      description: cls.description || `Reference ${cls.name} class`,
    })) || [];

  return (
    <SpaceBetween size="m">
      <FormField label="Contains Validation Mode">
        <RadioGroup
          value={mode}
          onChange={handleModeChange}
          items={[
            {
              value: 'class',
              label: 'Match existing class',
              description: 'Array must contain items that match a defined class',
            },
            {
              value: 'property',
              label: 'Match property values',
              description: 'Array must contain items with specific property values',
            },
            {
              value: 'custom',
              label: 'Custom JSON Schema',
              description: 'Advanced: Define custom validation schema',
            },
          ]}
        />
      </FormField>

      {mode === 'class' && (
        <FormField label="Select Class" description="Choose a class definition that array items must match">
          <Select
            selectedOption={selectedClass ? classOptions.find((opt) => opt.value === selectedClass) || null : null}
            onChange={handleClassSelect}
            options={classOptions}
            placeholder="Select a class..."
            empty="No classes available"
          />
        </FormField>
      )}

      {mode === 'property' && (
        <SpaceBetween size="m">
          <FormField label="Property Name" description="Name of the property to check in array items">
            <Input
              value={propertyName}
              onChange={({ detail }) => setPropertyName(detail.value)}
              onBlur={handlePropertyUpdate}
              placeholder="e.g., Status, Type, Category"
            />
          </FormField>

          <FormField label="Condition" description="Type of validation to apply">
            <Select
              selectedOption={condition}
              onChange={({ detail }) => {
                setCondition(detail.selectedOption as ConditionOption);
                handlePropertyUpdate();
              }}
              options={CONDITION_OPTIONS}
            />
          </FormField>

          <FormField
            label="Value"
            description={condition.value === 'enum' ? 'Comma-separated list of allowed values' : 'Value to match against'}
          >
            <Input
              value={propertyValue}
              onChange={({ detail }) => setPropertyValue(detail.value)}
              onBlur={handlePropertyUpdate}
              placeholder={condition.value === 'enum' ? 'e.g., approved, pending, rejected' : 'e.g., approved'}
            />
          </FormField>
        </SpaceBetween>
      )}

      {mode === 'custom' && (
        <FormField
          label="Custom Contains Schema"
          description="JSON Schema that array items must match. Changes applied when valid JSON is entered."
        >
          <Textarea
            value={customSchema}
            onChange={({ detail }) => handleCustomSchemaChange(detail.value)}
            rows={8}
            placeholder='{"type": "object", "properties": {"Status": {"const": "approved"}}}'
          />
        </FormField>
      )}

      {containsSchema && (
        <ExpandableSection headerText="Preview Generated Schema" variant="footer">
          <Textarea value={JSON.stringify(containsSchema, null, 2)} readOnly rows={6} />
        </ExpandableSection>
      )}
    </SpaceBetween>
  );
};

export default ContainsSchemaBuilder;
