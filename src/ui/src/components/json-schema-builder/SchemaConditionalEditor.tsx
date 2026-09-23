import React from 'react';
import {
  Box,
  SpaceBetween,
  Header,
  FormField,
  Input,
  Select,
  Button,
  Container,
  ExpandableSection,
  Alert,
} from '@cloudscape-design/components';
import { refNode } from './utils/schemaHelpers';

interface SchemaAttribute {
  if?: Record<string, unknown>;
  then?: Record<string, unknown>;
  else?: Record<string, unknown>;
  [key: string]: unknown;
}

interface AvailableClass {
  name: string;
  id?: string;
}

interface SchemaConditionalEditorProps {
  selectedAttribute?: SchemaAttribute | null;
  availableClasses?: AvailableClass[];
  onUpdate: (updates: SchemaAttribute) => void;
}

const SchemaConditionalEditor = ({
  selectedAttribute = null,
  availableClasses = [],
  onUpdate,
}: SchemaConditionalEditorProps): React.JSX.Element | null => {
  const hasConditional = selectedAttribute && selectedAttribute.if;

  const schemaTypeOptions = [
    { label: 'String', value: 'string' },
    { label: 'Number', value: 'number' },
    { label: 'Integer', value: 'integer' },
    { label: 'Boolean', value: 'boolean' },
    { label: 'Object', value: 'object' },
    { label: 'Array', value: 'array' },
    { label: 'Null', value: 'null' },
    ...(availableClasses || []).map((cls) => ({
      label: `Ref: ${cls.name}`,
      value: `#/$defs/${cls.name}`,
    })),
  ];

  const handleAddConditional = (): void => {
    if (!selectedAttribute) return;
    const updates = {
      ...selectedAttribute,
      if: { type: 'string' },
      then: { type: 'string' },
    };
    onUpdate(updates);
  };

  const handleRemoveConditional = (): void => {
    if (!selectedAttribute) return;
    const updates = { ...selectedAttribute };
    delete updates.if;
    delete updates.then;
    delete updates.else;
    onUpdate(updates);
  };

  const handleAddElse = (): void => {
    if (!selectedAttribute) return;
    const updates = {
      ...selectedAttribute,
      else: { type: 'string' },
    };
    onUpdate(updates);
  };

  const handleRemoveElse = (): void => {
    if (!selectedAttribute) return;
    const updates = { ...selectedAttribute };
    delete updates.else;
    onUpdate(updates);
  };

  const handleUpdateSchema = (key: string, field: string, value: string): void => {
    if (!selectedAttribute) return;
    const updates = { ...selectedAttribute } as Record<string, unknown>;

    if (!updates[key]) {
      updates[key] = {};
    }

    if (field === 'type') {
      // `type` and `$ref` are alternatives, so switching between them replaces the branch.
      updates[key] = value.startsWith('#/$defs/') ? refNode(value) : { type: value };
    } else if (field === 'const') {
      try {
        (updates[key] as Record<string, unknown>).const = JSON.parse(value);
      } catch {
        (updates[key] as Record<string, unknown>).const = value;
      }
    } else {
      (updates[key] as Record<string, unknown>)[field] = value;
    }

    onUpdate(updates as SchemaAttribute);
  };

  const renderSchemaEditor = (key: string, label: string, description: string): React.JSX.Element => {
    const schema = (selectedAttribute?.[key] as Record<string, unknown>) || {};

    return (
      <Container>
        <SpaceBetween size="m">
          <Header {...({ variant: 'h4' } as Record<string, unknown>)}>{label}</Header>
          <Alert type="info">{description}</Alert>

          <FormField label="Schema Type">
            <Select
              selectedOption={
                schemaTypeOptions.find((opt) => schema.$ref === opt.value || schema.type === opt.value) || schemaTypeOptions[0]
              }
              onChange={({ detail }) => handleUpdateSchema(key, 'type', detail.selectedOption.value ?? '')}
              options={schemaTypeOptions}
            />
          </FormField>

          {key === 'if' && (
            <FormField label="Const Value (for if condition)" description="Exact value to match against">
              <Input
                value={schema.const !== undefined ? JSON.stringify(schema.const) : ''}
                onChange={({ detail }) => handleUpdateSchema(key, 'const', detail.value)}
                placeholder='e.g., "USA", 42'
              />
            </FormField>
          )}
        </SpaceBetween>
      </Container>
    );
  };

  if (!selectedAttribute) return null;

  return (
    <ExpandableSection headerText="Conditional Schema (if/then/else)" variant="container">
      <SpaceBetween size="m">
        <Alert type="info">
          Conditional schemas allow validation to change based on the value of the data. Use this for scenarios like: different validation
          rules per country, required fields based on a status, or format validation based on type.
        </Alert>

        {!hasConditional ? (
          <Container>
            <SpaceBetween size="m">
              <p>Add conditional validation to this attribute</p>
              <Button onClick={handleAddConditional} variant="primary">
                Add if/then Condition
              </Button>
            </SpaceBetween>
          </Container>
        ) : (
          <SpaceBetween size="m">
            <Box float="right">
              <Button onClick={handleRemoveConditional} variant="normal">
                Remove Conditional
              </Button>
            </Box>

            {renderSchemaEditor('if', 'If (Condition)', 'When the data matches this schema...')}
            {renderSchemaEditor('then', 'Then (When true)', 'Apply this schema if the condition is true')}

            {!selectedAttribute.else ? (
              <Button onClick={handleAddElse} variant="normal">
                Add Else Branch
              </Button>
            ) : (
              <>
                {renderSchemaEditor('else', 'Else (When false)', 'Apply this schema if the condition is false')}
                <Button onClick={handleRemoveElse} variant="link">
                  Remove Else Branch
                </Button>
              </>
            )}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </ExpandableSection>
  );
};

export default SchemaConditionalEditor;
