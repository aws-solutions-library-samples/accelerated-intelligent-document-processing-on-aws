import React, { useState, useEffect } from 'react';
import { Header, FormField, Input, TokenGroup, Button, SpaceBetween } from '@cloudscape-design/components';
import { ClassLike, formatValueForInput, parseInputValue, resolveAttributeType } from '../utils/schemaHelpers';

interface SchemaAttribute {
  type?: string;
  const?: unknown;
  enum?: unknown[];
  items?: {
    type?: string;
    $ref?: string;
    const?: unknown;
    enum?: unknown[];
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

interface ValueConstraintsProps {
  attribute: SchemaAttribute;
  onUpdate: (updates: Partial<SchemaAttribute>) => void;
  /** Used only to resolve a `$ref`'d attribute's real type when parsing Const. */
  availableClasses?: ClassLike[];
}

const ValueConstraints = ({ attribute, onUpdate, availableClasses }: ValueConstraintsProps): React.JSX.Element => {
  // Local state for buffering user input without immediate parsing
  const [constInput, setConstInput] = useState('');
  const [enumInput, setEnumInput] = useState('');

  // A property written as a bare `{"$ref": "#/$defs/Address"}` has no `type` of
  // its own, so reading `.type` here yields undefined and `parseInputValue` below
  // stored a JSON Const as a raw string. This panel is rendered for EVERY
  // non-rule attribute and its Const/Enum inputs are not gated by type, unlike
  // every sibling constraints panel, so the ref has to be resolved (GitHub #906).
  const resolvedType = resolveAttributeType(attribute, availableClasses);

  // For arrays with simple item types (not $ref), enum/const should be on items, not the array itself
  const isSimpleArray = resolvedType === 'array' && attribute.items && !attribute.items.$ref;
  const effectiveType = isSimpleArray ? attribute.items?.type : resolvedType;

  // Get enum value from the correct location (items for simple arrays, attribute otherwise)
  const currentEnum = isSimpleArray ? attribute.items?.enum : attribute.enum;
  const currentConst = isSimpleArray ? attribute.items?.const : attribute.const;

  // Initialize local state from attribute values
  useEffect(() => {
    setConstInput(formatValueForInput(currentConst));
  }, [currentConst]);

  // Initialize enum input as empty (it's only shown when no enum exists yet)
  useEffect(() => {
    if (!currentEnum || currentEnum.length === 0) {
      setEnumInput('');
    }
  }, [currentEnum]);

  // Helper to update enum/const at the correct level (items for simple arrays)
  const updateValueConstraint = (updates: Record<string, unknown>): void => {
    if (isSimpleArray) {
      // Place enum/const inside items for simple arrays
      // Need to handle undefined values by explicitly removing keys
      const newItems = { ...attribute.items } as Record<string, unknown>;
      Object.keys(updates).forEach((key) => {
        if (updates[key] === undefined) {
          delete newItems[key];
        } else {
          newItems[key] = updates[key];
        }
      });
      onUpdate({ items: newItems as SchemaAttribute['items'] });
    } else {
      onUpdate(updates);
    }
  };

  // Handle Const field blur - parse and update parent state
  const handleConstBlur = (): void => {
    if (!constInput) {
      updateValueConstraint({ const: undefined });
      return;
    }
    const parsed = parseInputValue(constInput, effectiveType);
    updateValueConstraint({ const: parsed });
  };

  // Handle Enum field blur - parse and update parent state
  const handleEnumBlur = (): void => {
    const value = enumInput.trim();
    if (value) {
      try {
        const parsed = JSON.parse(`[${value}]`) as unknown[];
        updateValueConstraint({ enum: parsed });
      } catch {
        const enumValues = value
          .split(',')
          .map((v) => v.trim())
          .filter((v) => v);
        updateValueConstraint({ enum: enumValues.length > 0 ? enumValues : undefined });
      }
      // Clear the input after successful processing
      setEnumInput('');
    }
  };

  // Get placeholder examples based on effective type
  const getEnumPlaceholder = (): string => {
    switch (effectiveType) {
      case 'number':
      case 'integer':
        return 'e.g., 1, 2, 3';
      case 'boolean':
        return 'e.g., true, false';
      default:
        return 'e.g., active, pending, completed';
    }
  };

  const getConstPlaceholder = (): string => {
    switch (effectiveType) {
      case 'number':
      case 'integer':
        return 'e.g., 42';
      case 'boolean':
        return 'e.g., true';
      default:
        return 'e.g., active';
    }
  };

  // Build description with JSON Schema context
  const enumDescription = isSimpleArray
    ? 'Allowed values for each item in the array (JSON Schema enum). Comma-separated list.'
    : 'Allowed values for this field (JSON Schema enum). Comma-separated list.';

  const constDescription = isSimpleArray
    ? 'Each item in the array must be exactly this value (JSON Schema const).'
    : 'Field must be exactly this value (JSON Schema const).';

  return (
    <>
      <Header {...({ variant: 'h4' } as Record<string, unknown>)}>Value Constraints (JSON Schema)</Header>

      <FormField label="Const (Single Constant Value)" description={constDescription} constraintText={`Example: ${getConstPlaceholder()}`}>
        <Input
          value={constInput}
          onChange={({ detail }) => setConstInput(detail.value)}
          onBlur={handleConstBlur}
          placeholder={getConstPlaceholder()}
          disabled={!!currentEnum && currentEnum.length > 0}
        />
      </FormField>

      <FormField
        label="Enum (Allowed Values)"
        description={enumDescription}
        constraintText={`Example: ${getEnumPlaceholder()} - Values are comma-separated`}
      >
        {currentEnum && currentEnum.length > 0 ? (
          <SpaceBetween size="xs">
            <TokenGroup
              items={currentEnum.map((val) => ({
                label: typeof val === 'object' ? JSON.stringify(val) : String(val),
                dismissLabel: `Remove ${val}`,
              }))}
              onDismiss={({ detail: { itemIndex } }) => {
                const newEnum = [...(currentEnum || [])];
                newEnum.splice(itemIndex, 1);
                updateValueConstraint({ enum: newEnum.length > 0 ? newEnum : undefined });
              }}
            />
            <Button
              variant="link"
              onClick={() => {
                updateValueConstraint({ enum: undefined });
              }}
            >
              Clear all enum values
            </Button>
          </SpaceBetween>
        ) : (
          <Input
            placeholder={getEnumPlaceholder()}
            value={enumInput}
            onChange={({ detail }) => setEnumInput(detail.value)}
            onBlur={handleEnumBlur}
            disabled={currentConst !== undefined}
          />
        )}
      </FormField>
    </>
  );
};

export default ValueConstraints;
